"""命令级审批策略引擎测试（借鉴 Codex execpolicy）。"""

import pytest

from agent.exec_policy import (
    Decision,
    PrefixPattern,
    PrefixRule,
    Policy,
    tokenize,
    check_command,
    check_multiple,
    DEFAULT_POLICY,
)


class TestTokenize:
    def test_simple_split(self):
        assert tokenize("rm -rf /tmp") == ["rm", "-rf", "/tmp"]

    def test_empty(self):
        assert tokenize("") == []


class TestPrefixPattern:
    def test_first_only_matches_any_args(self):
        p = PrefixPattern("ls")
        policy = Policy([PrefixRule(p, Decision.ALLOW)])
        assert policy.evaluate(["ls", "-la"]) == Decision.ALLOW

    def test_rest_single(self):
        p = PrefixPattern("git", ("push",))
        assert Policy._matches(p, ["git", "push", "origin", "main"]) is True
        assert Policy._matches(p, ["git", "pull"]) is False

    def test_rest_alts(self):
        p = PrefixPattern("rm", (("-r", "-f", "-rf", "-fr"),))
        assert Policy._matches(p, ["rm", "-rf", "x"]) is True
        assert Policy._matches(p, ["rm", "-f", "x"]) is True
        assert Policy._matches(p, ["rm", "x"]) is False

    def test_first_mismatch(self):
        p = PrefixPattern("rm", (("-r",),))
        assert Policy._matches(p, ["cp", "-r", "x"]) is False


class TestPolicyEvaluate:
    def test_max_decision_wins(self):
        # 一条 allow + 一条 forbidden 同时命中 → forbidden
        policy = Policy([
            PrefixRule(PrefixPattern("rm"), Decision.ALLOW),
            PrefixRule(PrefixPattern("rm", (("-rf",),)), Decision.FORBIDDEN),
        ])
        assert policy.evaluate(["rm", "-rf", "x"]) == Decision.FORBIDDEN
        assert policy.evaluate(["rm", "x"]) == Decision.ALLOW

    def test_no_match_defaults_allow(self):
        assert DEFAULT_POLICY.evaluate(["echo", "hi"]) == Decision.ALLOW


class TestCheckCommand:
    def test_rm_rf_forbidden(self):
        assert check_command("rm -rf /tmp") == Decision.FORBIDDEN

    def test_ls_allowed(self):
        assert check_command("ls -la") == Decision.ALLOW

    def test_git_push_prompt(self):
        assert check_command("git push origin main") == Decision.PROMPT

    def test_git_status_allowed(self):
        assert check_command("git status") == Decision.ALLOW

    def test_sudo_forbidden(self):
        assert check_command("sudo rm -rf /") == Decision.FORBIDDEN

    def test_git_reset_hard_forbidden(self):
        assert check_command("git reset --hard HEAD~1") == Decision.FORBIDDEN

    def test_python_allowed(self):
        assert check_command("python script.py") == Decision.ALLOW


class TestCheckMultiple:
    def test_strictest_across_commands(self):
        cmds = ["ls -la", "git status", "rm -rf /tmp"]
        assert check_multiple(cmds) == Decision.FORBIDDEN

    def test_all_allowed(self):
        assert check_multiple(["ls", "pwd", "cat x.txt"]) == Decision.ALLOW
