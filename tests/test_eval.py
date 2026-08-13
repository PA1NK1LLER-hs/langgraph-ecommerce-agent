"""Phase 5 测试 — 可观测性与评估框架。

测试 eval/judge.py 的评分模型、eval/fixtures.py 的用例格式、
observability 模块的 Langfuse handler。
"""

import pytest
from pydantic import ValidationError


# ═══════════════════════════════════════════════════
# EvalScores 模型验证
# ═══════════════════════════════════════════════════

class TestEvalScores:
    """测试 LLM-as-Judge 评分模型。"""

    def test_valid_scores(self):
        from eval.judge import EvalScores
        scores = EvalScores(
            relevance=5, accuracy=4, completeness=3,
            tool_usage=4, overall=4, notes="Good response",
        )
        assert scores.relevance == 5
        assert scores.accuracy == 4
        assert scores.completeness == 3
        assert scores.tool_usage == 4
        assert scores.overall == 4

    def test_all_max(self):
        from eval.judge import EvalScores
        scores = EvalScores(
            relevance=5, accuracy=5, completeness=5,
            tool_usage=5, overall=5,
        )
        assert scores.overall == 5

    def test_all_min(self):
        from eval.judge import EvalScores
        scores = EvalScores(
            relevance=1, accuracy=1, completeness=1,
            tool_usage=1, overall=1,
        )
        assert scores.overall == 1

    def test_score_out_of_range_low(self):
        from eval.judge import EvalScores
        with pytest.raises(ValidationError):
            EvalScores(relevance=0, accuracy=5, completeness=5, tool_usage=5, overall=5)

    def test_score_out_of_range_high(self):
        from eval.judge import EvalScores
        with pytest.raises(ValidationError):
            EvalScores(relevance=6, accuracy=5, completeness=5, tool_usage=5, overall=5)

    def test_notes_default(self):
        from eval.judge import EvalScores
        scores = EvalScores(relevance=3, accuracy=3, completeness=3, tool_usage=3, overall=3)
        assert scores.notes == ""

    def test_serialization(self):
        from eval.judge import EvalScores
        scores = EvalScores(relevance=4, accuracy=4, completeness=4, tool_usage=4, overall=4, notes="ok")
        d = scores.model_dump()
        assert d == {
            "relevance": 4, "accuracy": 4, "completeness": 4,
            "tool_usage": 4, "overall": 4, "notes": "ok",
        }


# ═══════════════════════════════════════════════════
# Judge Prompt 格式
# ═══════════════════════════════════════════════════

class TestJudgePrompt:
    """测试 JUDGE_PROMPT 模板格式正确。"""

    def test_prompt_contains_all_dimensions(self):
        from eval.judge import JUDGE_PROMPT
        assert "relevance" in JUDGE_PROMPT
        assert "accuracy" in JUDGE_PROMPT
        assert "completeness" in JUDGE_PROMPT
        assert "tool_usage" in JUDGE_PROMPT
        assert "overall" in JUDGE_PROMPT

    def test_prompt_is_formattable(self):
        from eval.judge import JUDGE_PROMPT
        formatted = JUDGE_PROMPT.format(
            question="test question",
            answer="test answer",
            tool_calls="[]",
        )
        assert "test question" in formatted
        assert "test answer" in formatted
        assert "[]" in formatted


# ═══════════════════════════════════════════════════
# Fixtures 格式验证
# ═══════════════════════════════════════════════════

class TestEvalFixtures:
    """测试回归测试用例格式。"""

    def test_all_cases_have_name(self):
        from eval.fixtures import REGRESSION_TESTS
        for case in REGRESSION_TESTS:
            assert "name" in case, f"Missing name in case: {case}"
            assert "question" in case, f"Missing question in case: {case.get('name')}"

    def test_all_cases_have_at_least_one_min(self):
        from eval.fixtures import REGRESSION_TESTS
        for case in REGRESSION_TESTS:
            has_min = any(
                case.get(k) is not None
                for k in ("min_relevance", "min_accuracy", "min_completeness",
                          "min_tool_usage", "min_overall")
            )
            assert has_min, f"Case '{case['name']}' has no minimum thresholds"

    def test_names_are_unique(self):
        from eval.fixtures import REGRESSION_TESTS
        names = [c["name"] for c in REGRESSION_TESTS]
        assert len(names) == len(set(names)), f"Duplicate names: {names}"

    def test_min_scores_in_range(self):
        from eval.fixtures import REGRESSION_TESTS
        for case in REGRESSION_TESTS:
            for k in ("min_relevance", "min_accuracy", "min_completeness",
                      "min_tool_usage", "min_overall"):
                val = case.get(k)
                if val is not None:
                    assert 1 <= val <= 5, f"Case '{case['name']}' {k}={val} out of range"


# ═══════════════════════════════════════════════════
# Langfuse Observability
# ═══════════════════════════════════════════════════

class TestObservability:
    """测试 Langfuse 可观测性模块。"""

    def test_get_handler_returns_none_without_env(self, monkeypatch):
        """未设置环境变量时返回 None（不抛出异常）。"""
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        # 重新导入以清除缓存
        import observability
        observability._langfuse_handler = None  # type: ignore[attr-defined]

        handler = observability.get_langfuse_handler()
        assert handler is None

    def test_get_handler_returns_none_with_partial_env(self, monkeypatch):
        """只有 PUBLIC_KEY 无 SECRET_KEY 时返回 None。"""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        import observability
        observability._langfuse_handler = None  # type: ignore[attr-defined]

        handler = observability.get_langfuse_handler()
        assert handler is None

    def test_get_callbacks_returns_none_without_env(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

        import observability
        observability._langfuse_handler = None  # type: ignore[attr-defined]

        callbacks = observability.get_callbacks()
        assert callbacks is None
