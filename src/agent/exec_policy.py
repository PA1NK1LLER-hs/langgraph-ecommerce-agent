"""命令级审批策略引擎 — 借鉴 Codex harness 的 execpolicy 模块。

Codex 的 ``execpolicy/src/``（``decision.rs`` / ``rule.rs`` / ``policy.rs``）
用「前缀规则」判定一条命令是否可执行：把命令按空格切分成 argv，
用 ``PrefixPattern``（首词 + 后续词，支持多选）做最长前缀匹配，
对匹配到的规则取最严格决策（Decision: allow < prompt < forbidden）。

本模块提供同构的纯 Python 实现，作为 shell 类工具（``execute_code``）
在工具名粒度审批之外的**第二道命令级闸**：

- ``Decision`` 偏序：allow < prompt < forbidden；
- ``PrefixPattern``：首词必匹配 + 后续词逐段匹配（支持多选）；
- ``PrefixRule``：pattern + decision；
- ``Policy``：多条规则，``evaluate(argv)`` 取所有匹配规则的最严格决策；
- ``check_command`` / ``check_multiple``：便捷入口。

所有类为纯数据/纯函数，无副作用，便于单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable


class Decision(IntEnum):
    """决策偏序：数值越大越严格。"""
    ALLOW = 0
    PROMPT = 1
    FORBIDDEN = 2


@dataclass
class PrefixPattern:
    """前缀匹配模式。

    ``first`` 必须精确匹配 argv[0]；``rest`` 逐段匹配 argv[1:]，
    每段可给单个字符串或字符串元组（多选，命中其一即匹配）。
    ``rest`` 为空表示只匹配首词（任何以该首词开头的命令都命中）。
    """
    first: str
    rest: tuple[str | tuple[str, ...], ...] = ()


def _matches_one(pattern: str | tuple[str, ...], word: str) -> bool:
    if isinstance(pattern, tuple):
        return word in pattern
    return word == pattern


@dataclass
class PrefixRule:
    """一条前缀规则：命令命中 ``pattern`` 时返回 ``decision``。"""
    pattern: PrefixPattern
    decision: Decision


@dataclass
class Policy:
    """多条规则组成的策略。"""
    rules: list[PrefixRule] = field(default_factory=list)

    def evaluate(self, argv: list[str]) -> Decision:
        """对单条命令求决策：取所有匹配规则的最严格决策，无匹配返回 ALLOW。

        与 Codex 一致：未命中任何规则默认放行（allow），
        因为禁止项应由显式 forbidden 规则覆盖。
        """
        result = Decision.ALLOW
        for rule in self.rules:
            if self._matches(rule.pattern, argv):
                if rule.decision > result:
                    result = rule.decision
        return result

    @staticmethod
    def _matches(pattern: PrefixPattern, argv: list[str]) -> bool:
        if not argv or argv[0] != pattern.first:
            return False
        for i, expected in enumerate(pattern.rest):
            word = argv[i + 1] if i + 1 < len(argv) else None
            if word is None:
                return False
            if not _matches_one(expected, word):
                return False
        return True


def tokenize(command: str) -> list[str]:
    """把命令字符串切成 argv（简单空白切分，够前缀匹配用）。"""
    return command.split()


def check_command(command: str, policy: Policy | None = None) -> Decision:
    """对单条命令字符串求决策。"""
    policy = policy or DEFAULT_POLICY
    return policy.evaluate(tokenize(command))


def check_multiple(commands: Iterable[str], policy: Policy | None = None) -> Decision:
    """对多条命令求决策：取各命令的最严格决策。"""
    policy = policy or DEFAULT_POLICY
    result = Decision.ALLOW
    for c in commands:
        d = check_command(c, policy)
        if d > result:
            result = d
    return result


# ---------------------------------------------------------------------------
# 默认策略（借鉴 Codex 默认 policy：危险命令 forbidden，无害命令 allow）
# ---------------------------------------------------------------------------

DEFAULT_POLICY = Policy(rules=[
    # ── 危险操作：forbidden ──
    PrefixRule(PrefixPattern("rm", (("-r", "-f", "-rf", "-fr"),)), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("rm", (("-rf", "-fr"),)), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("mkfs", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("dd", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("shutdown", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("reboot", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("halt", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("poweroff", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern(":(){", ()), Decision.FORBIDDEN),  # fork bomb
    PrefixRule(PrefixPattern("chmod", (("-R", "777"),)), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("chown", (("-R",),)), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("sudo", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("su", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("format", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("mkfs.ext4", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("git", ("push",)), Decision.PROMPT),
    PrefixRule(PrefixPattern("git", ("reset", ("--hard",))), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("git", ("clean", ("-fd", "-df", "-fdx"))), Decision.PROMPT),
    PrefixRule(PrefixPattern("pip", ("uninstall",)), Decision.PROMPT),
    PrefixRule(PrefixPattern("docker", ("rm",)), Decision.PROMPT),
    PrefixRule(PrefixPattern("docker", ("rmi",)), Decision.PROMPT),
    PrefixRule(PrefixPattern("kubectl", ("delete",)), Decision.PROMPT),
    PrefixRule(PrefixPattern("dropdb", ()), Decision.FORBIDDEN),
    PrefixRule(PrefixPattern("kill", (("-9",),)), Decision.PROMPT),

    # ── 无害操作：allow（显式放行，供 strict 模式参考）──
    PrefixRule(PrefixPattern("ls", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("pwd", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("cat", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("head", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("tail", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("echo", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("python", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("python3", ()), Decision.ALLOW),
    PrefixRule(PrefixPattern("pip", ("list", "show", "freeze")), Decision.ALLOW),
    PrefixRule(PrefixPattern("git", ("status", "diff", "log", "branch", "fetch", "pull")), Decision.ALLOW),
])
