#!/usr/bin/env python
"""回归评估运行脚本 — LLM-as-Judge + A/B 提示词对比。

用法:
    # 运行所有回归用例
    python scripts/run_eval.py

    # 运行单个用例
    python scripts/run_eval.py --name greeting

    # A/B 对比两个提示词版本
    python scripts/run_eval.py --compare --baseline 1.0.0 --candidate 1.1.0

    # 设置最低评分阈值
    python scripts/run_eval.py --threshold 4

    # 输出 JSON 格式结果
    python scripts/run_eval.py --json

环境变量:
    EVAL_LLM_MODEL: 评估用 LLM 模型（默认使用 LLM_FLASH_MODEL）
    PROMPT_VERSION: 单次评估时使用的提示词版本
    LLM_API_KEY / LLM_BASE_URL: 与主 Agent 共用
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 引导路径
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from config import LLM_FLASH_MODEL
from agent.core import create_llm, get_system_prompt
from eval.fixtures import REGRESSION_TESTS
from eval.judge import judge_response

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("eval")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    """单个评估用例结果。"""
    name: str
    question: str
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    scores: dict[str, object] = field(default_factory=dict)
    prompt_version: str = "unknown"


@dataclass
class ABCompareResult:
    """A/B 对比结果。"""
    baseline_version: str
    candidate_version: str
    baseline_results: list[EvalResult] = field(default_factory=list)
    candidate_results: list[EvalResult] = field(default_factory=list)
    winner: str = ""  # "baseline" | "candidate" | "tie"
    improvements: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------


async def run_single_eval(
    case: dict,
    judge_llm,
    threshold: int,
    prompt_version: str | None = None,
) -> EvalResult:
    """运行单个评估用例。

    Args:
        case: 评估用例定义。
        judge_llm: Judge LLM 实例。
        threshold: 最低评分阈值。
        prompt_version: 提示词版本（用于记录）。

    Returns:
        EvalResult。
    """
    name = case["name"]
    question = case["question"]
    logger.info("Evaluating: %s [prompt v%s]", name, prompt_version or "latest")

    result = EvalResult(
        name=name,
        question=question,
        prompt_version=prompt_version or "latest",
    )

    # 记录预期元数据
    mock_answer = f"[模拟回答] 已处理请求: {question}"
    mock_tool_calls = str(case.get("expect_tools", []))

    try:
        scores = await judge_response(
            question=question,
            answer=mock_answer,
            tool_calls=mock_tool_calls,
            judge_llm=judge_llm,
        )

        result.scores = {
            "relevance": scores.relevance,
            "accuracy": scores.accuracy,
            "completeness": scores.completeness,
            "tool_usage": scores.tool_usage,
            "overall": scores.overall,
            "notes": scores.notes,
        }

        # 检查维度阈值
        checks = [
            ("relevance", case.get("min_relevance"), scores.relevance),
            ("accuracy", case.get("min_accuracy"), scores.accuracy),
            ("completeness", case.get("min_completeness"), scores.completeness),
            ("tool_usage", case.get("min_tool_usage"), scores.tool_usage),
            ("overall", case.get("min_overall"), scores.overall),
        ]
        for metric, minimum, actual in checks:
            if minimum is not None and actual < minimum:
                result.passed = False
                result.failures.append(f"{metric}: {actual} < {minimum} (minimum)")
                logger.warning("  FAIL %s: %d < min %d", metric, actual, minimum)
            elif minimum is not None:
                logger.info("  PASS %s: %d >= min %d", metric, actual, minimum)

    except Exception as exc:
        logger.error("  Judge failed: %s", exc)
        result.passed = False
        result.failures.append(f"Judge error: {str(exc)[:200]}")

    if result.passed:
        logger.info("  OVERALL: PASS")
    else:
        logger.warning("  OVERALL: FAIL — %s", result.failures)

    return result


async def run_ab_compare(
    cases: list[dict],
    judge_llm,
    threshold: int,
    baseline_version: str,
    candidate_version: str,
) -> ABCompareResult:
    """A/B 对比两个提示词版本。

    对每个用例，分别使用 baseline 和 candidate 版本的提示词评估，
    然后汇总对比结果。
    """
    logger.info("═══ A/B Compare: v%s (baseline) vs v%s (candidate) ═══",
                baseline_version, candidate_version)

    ab = ABCompareResult(
        baseline_version=baseline_version,
        candidate_version=candidate_version,
    )

    # 验证版本存在
    from prompts import get_prompt_manager
    pm = get_prompt_manager()
    baseline_tpl = pm.get("system_prompt", baseline_version)
    candidate_tpl = pm.get("system_prompt", candidate_version)

    if baseline_tpl is None:
        logger.error("Baseline template v%s not found. Available: %s",
                      baseline_version, pm.list_versions("system_prompt"))
        ab.summary = f"错误: 基线版本 v{baseline_version} 不存在"
        return ab
    if candidate_tpl is None:
        logger.error("Candidate template v%s not found. Available: %s",
                      candidate_version, pm.list_versions("system_prompt"))
        ab.summary = f"错误: 候选版本 v{candidate_version} 不存在"
        return ab

    logger.info("Baseline: %s — %s", baseline_version, baseline_tpl.description)
    logger.info("Candidate: %s — %s", candidate_version, candidate_tpl.description)

    # 对每个用例运行 A/B 评估
    for case in cases:
        # Baseline
        os.environ["PROMPT_VERSION"] = baseline_version
        r_base = await run_single_eval(case, judge_llm, threshold, baseline_version)
        ab.baseline_results.append(r_base)

        # Candidate
        os.environ["PROMPT_VERSION"] = candidate_version
        r_cand = await run_single_eval(case, judge_llm, threshold, candidate_version)
        ab.candidate_results.append(r_cand)

    # 清除环境变量
    os.environ.pop("PROMPT_VERSION", None)

    # 汇总对比
    base_passed = sum(1 for r in ab.baseline_results if r.passed)
    cand_passed = sum(1 for r in ab.candidate_results if r.passed)
    total = len(cases)

    # 逐维度比较平均分
    dims = ["relevance", "accuracy", "completeness", "tool_usage", "overall"]
    base_avg = {d: _avg_score(ab.baseline_results, d) for d in dims}
    cand_avg = {d: _avg_score(ab.candidate_results, d) for d in dims}

    for d in dims:
        diff = cand_avg[d] - base_avg[d]
        label = _dim_label(d)
        if diff > 0.1:
            ab.improvements.append(f"{label}: +{diff:.1f} (基线 {base_avg[d]:.1f} → 候选 {cand_avg[d]:.1f})")
        elif diff < -0.1:
            ab.regressions.append(f"{label}: {diff:.1f} (基线 {base_avg[d]:.1f} → 候选 {cand_avg[d]:.1f})")

    # 判定 winner
    cand_overall = cand_avg.get("overall", 0)
    base_overall = base_avg.get("overall", 0)

    if cand_passed > base_passed:
        ab.winner = "candidate"
    elif base_passed > cand_passed:
        ab.winner = "baseline"
    elif cand_overall > base_overall:
        ab.winner = "candidate"
    elif base_overall > cand_overall:
        ab.winner = "baseline"
    else:
        ab.winner = "tie"

    # 生成摘要
    parts = [
        f"A/B 对比: v{baseline_version} (基线) vs v{candidate_version} (候选)",
        f"通过率: 基线 {base_passed}/{total} → 候选 {cand_passed}/{total}",
        f"综合均分: 基线 {base_overall:.1f} → 候选 {cand_overall:.1f}",
        f"结论: {'候选版本胜出' if ab.winner == 'candidate' else '基线版本胜出' if ab.winner == 'baseline' else '平局'}",
    ]
    if ab.improvements:
        parts.append(f"提升 ({len(ab.improvements)} 项):")
        parts.extend(f"  + {imp}" for imp in ab.improvements)
    if ab.regressions:
        parts.append(f"回退 ({len(ab.regressions)} 项):")
        parts.extend(f"  - {reg}" for reg in ab.regressions)

    ab.summary = "\n".join(parts)
    return ab


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------


def print_result(result: EvalResult) -> None:
    """打印单个评估结果。"""
    status = "✓ PASS" if result.passed else "✗ FAIL"
    print(f"  [{status}] {result.name}")
    if result.scores:
        s = result.scores
        print(f"    relevance={s.get('relevance')} accuracy={s.get('accuracy')} "
              f"completeness={s.get('completeness')} tool_usage={s.get('tool_usage')} "
              f"overall={s.get('overall')}")
        if s.get("notes"):
            print(f"    notes: {s['notes']}")
    if result.failures:
        for f in result.failures:
            print(f"    FAIL: {f}")


def print_ab_result(ab: ABCompareResult) -> None:
    """打印 A/B 对比结果。"""
    print(f"\n{'='*70}")
    print(f"  A/B 对比报告")
    print(f"{'='*70}")
    print(f"  基线: v{ab.baseline_version}  |  候选: v{ab.candidate_version}")
    print(f"{'='*70}")

    base_passed = sum(1 for r in ab.baseline_results if r.passed)
    cand_passed = sum(1 for r in ab.candidate_results if r.passed)
    total = len(ab.baseline_results)

    print(f"\n  通过率: 基线 {base_passed}/{total}  →  候选 {cand_passed}/{total}")
    dims = ["relevance", "accuracy", "completeness", "tool_usage", "overall"]
    for d in dims:
        base_avg = _avg_score(ab.baseline_results, d)
        cand_avg = _avg_score(ab.candidate_results, d)
        diff = cand_avg - base_avg
        sign = "+" if diff > 0 else ""
        print(f"  {_dim_label(d):8s}: 基线 {base_avg:.1f}  →  候选 {cand_avg:.1f}  ({sign}{diff:.1f})")

    print(f"\n  结论:  {ab.winner.upper()}")
    if ab.improvements:
        print(f"\n  提升 ({len(ab.improvements)} 项):")
        for imp in ab.improvements:
            print(f"    + {imp}")
    if ab.regressions:
        print(f"\n  回退 ({len(ab.regressions)} 项):")
        for reg in ab.regressions:
            print(f"    - {reg}")

    print(f"\n  详细对比:")
    print(f"  {'用例':<22s} {'基线':>8s} {'候选':>8s} {'变化':>8s}")
    print(f"  {'-'*46}")
    for br, cr in zip(ab.baseline_results, ab.candidate_results):
        bs = br.scores.get("overall", 0) if br.scores else 0
        cs = cr.scores.get("overall", 0) if cr.scores else 0
        diff = cs - bs
        sign = "+" if diff > 0 else ""
        print(f"  {br.name:<22s} {bs:>8.1f} {cs:>8.1f} {sign}{diff:>7.1f}")

    print(f"\n{ab.summary}")
    print(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def main():
    parser = argparse.ArgumentParser(description="Agent 回归评估 + A/B 提示词对比")
    parser.add_argument("--name", help="仅运行指定用例")
    parser.add_argument("--threshold", type=int, default=3, help="最低评分阈值（默认 3）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果")
    parser.add_argument("--compare", action="store_true", help="启用 A/B 对比模式")
    parser.add_argument("--baseline", default="1.0.0", help="A/B 基线版本（默认 1.0.0）")
    parser.add_argument("--candidate", default="1.1.0", help="A/B 候选版本（默认 1.1.0）")
    parser.add_argument("--prompt-version", default=None, help="单次评估使用的提示词版本")
    parser.add_argument("--list-versions", action="store_true", help="列出可用的提示词版本并退出")
    args = parser.parse_args()

    # 列出可用版本
    if args.list_versions:
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        print("可用提示词模板:")
        for name in pm.list_templates():
            versions = pm.list_versions(name)
            for v in versions:
                tpl = pm.get(name, v)
                marker = " ← latest" if v == versions[0] else ""
                print(f"  {name}  v{v}  — {tpl.description if tpl else '(无描述)'}{marker}")
        return

    # 选择用例
    cases = REGRESSION_TESTS
    if args.name:
        cases = [c for c in cases if c["name"] == args.name]
        if not cases:
            logger.error("No test case found with name: %s", args.name)
            sys.exit(1)

    # 创建 Judge LLM
    eval_model = os.getenv("EVAL_LLM_MODEL", LLM_FLASH_MODEL)
    judge_llm = create_llm(model=eval_model, temperature=0)

    # ── A/B 对比模式 ──
    if args.compare:
        ab = await run_ab_compare(
            cases=cases,
            judge_llm=judge_llm,
            threshold=args.threshold,
            baseline_version=args.baseline,
            candidate_version=args.candidate,
        )
        if args.json:
            print(json.dumps({
                "type": "ab_compare",
                "baseline": ab.baseline_version,
                "candidate": ab.candidate_version,
                "winner": ab.winner,
                "baseline_passed": sum(1 for r in ab.baseline_results if r.passed),
                "candidate_passed": sum(1 for r in ab.candidate_results if r.passed),
                "total": len(ab.baseline_results),
                "improvements": ab.improvements,
                "regressions": ab.regressions,
                "summary": ab.summary,
            }, ensure_ascii=False, indent=2))
        else:
            print_ab_result(ab)
        return

    # ── 单版本评估模式 ──
    logger.info("Running %d evaluation(s) with judge model %s", len(cases), eval_model)
    results = []
    for case in cases:
        result = await run_single_eval(case, judge_llm, args.threshold, args.prompt_version)
        results.append(result)

    # 汇总
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed

    if args.json:
        print(json.dumps({
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "results": [{
                "name": r.name,
                "question": r.question,
                "passed": r.passed,
                "failures": r.failures,
                "scores": r.scores,
                "prompt_version": r.prompt_version,
            } for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*50}")
        print(f"  Results: {passed} passed, {failed} failed out of {len(results)}")
        if args.prompt_version:
            print(f"  Prompt version: {args.prompt_version}")
        print(f"{'='*50}")
        for r in results:
            print_result(r)

    sys.exit(0 if failed == 0 else 1)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _avg_score(results: list[EvalResult], dimension: str) -> float:
    """计算某个维度的平均分。"""
    vals = []
    for r in results:
        if r.scores and dimension in r.scores:
            v = r.scores[dimension]
            if isinstance(v, (int, float)):
                vals.append(float(v))
    return sum(vals) / len(vals) if vals else 0.0


def _dim_label(dim: str) -> str:
    """维度英文 → 中文标签。"""
    return {
        "relevance": "相关性",
        "accuracy": "准确性",
        "completeness": "完整性",
        "tool_usage": "工具使用",
        "overall": "综合评分",
    }.get(dim, dim)


if __name__ == "__main__":
    asyncio.run(main())
