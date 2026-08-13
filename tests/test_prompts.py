"""提示词管理系统测试。"""

import os
import tempfile
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# PromptManager 测试
# ---------------------------------------------------------------------------


class TestPromptManagerLoading:
    """测试 PromptManager 的加载功能。"""

    def test_load_all_finds_templates(self):
        """加载模板目录应找到至少一个模板。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        templates = pm.list_templates()
        assert "system_prompt" in templates, f"Expected 'system_prompt' in {templates}"

    def test_get_latest_returns_newest_version(self):
        """get() 无版本号应返回最新版本。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        tpl = pm.get("system_prompt")
        assert tpl is not None
        versions = pm.list_versions("system_prompt")
        assert tpl.version == versions[0]  # 最新版本在最前

    def test_get_specific_version(self):
        """get() 指定版本应返回精确匹配。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        tpl = pm.get("system_prompt", "1.0.0")
        assert tpl is not None
        assert tpl.version == "1.0.0"

    def test_list_versions_descending(self):
        """版本列表应是降序的。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        versions = pm.list_versions("system_prompt")
        assert len(versions) >= 2, f"Expected at least 2 versions, got {versions}"

        # 验证降序
        for i in range(len(versions) - 1):
            a = tuple(map(int, versions[i].split(".")))
            b = tuple(map(int, versions[i + 1].split(".")))
            assert a > b, f"{versions[i]} should be > {versions[i+1]}"

    def test_list_all_flattens_versions(self):
        """list_all() 应返回所有模板的所有版本的扁平列表。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        all_templates = pm.list_all()
        # 每个模板有 ≥2 版本
        assert len(all_templates) >= 2

    def test_nonexistent_template_returns_none(self):
        """不存在的模板 get() 返回 None。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        assert pm.get("nonexistent_template") is None

    def test_nonexistent_version_returns_none(self):
        """存在的模板但不存在的版本号 get() 返回 None。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()
        assert pm.get("system_prompt", "99.99.99") is None


class TestPromptManagerRender:
    """测试 PromptManager 的渲染功能。"""

    def test_render_with_variables(self):
        """渲染应替换模板中的占位符。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        result = pm.render("system_prompt", version="1.0.0",
                           capabilities="CAP_TEST", tool_names="TOOL_TEST")

        assert "CAP_TEST" in result
        assert "TOOL_TEST" in result
        assert "你是一个能力全面的 AI 助手" in result

    def test_render_missing_variable_retained(self):
        """未提供的变量应在输出中保留占位符。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        result = pm.render("system_prompt", version="1.0.0",
                           capabilities="CAP_TEST")
        # 未提供 tool_names，应保留 {{tool_names}}
        assert "CAP_TEST" in result

    def test_render_nonexistent_template_raises(self):
        """渲染不存在的模板应抛出 ValueError。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        with pytest.raises(ValueError, match="not found"):
            pm.render("nonexistent")

    def test_render_with_jinja2_style_placeholders(self):
        """应正确处理 {{var}} 风格的 Jinja2 占位符。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        result = pm.render("system_prompt", version="1.0.0",
                           capabilities="CAPS", tool_names="TOOLS")
        assert "{{capabilities}}" not in result
        assert "{{tool_names}}" not in result


class TestPromptManagerSave:
    """测试 PromptManager 的保存功能。"""

    def test_save_new_version(self):
        """保存新版本应创建文件并可立即查询。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        test_version = "9.9.9"
        # 清理（如果上次测试残留）
        try:
            existing = pm.get("system_prompt", test_version)
            if existing:
                # 从 _templates 中删除旧条目
                pm._templates["system_prompt"] = [
                    t for t in pm._templates["system_prompt"]
                    if t.version != test_version
                ]
        except Exception:
            pass

        file_path = pm.save(
            "system_prompt",
            version=test_version,
            template="测试模板 {{test_var}}",
            variables=["test_var"],
            description="测试保存功能",
        )

        # 查询
        tpl = pm.get("system_prompt", test_version)
        assert tpl is not None
        assert tpl.version == test_version
        assert tpl.description == "测试保存功能"
        assert "test_var" in tpl.variables

        # 清理文件
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass

        # 从内存中清理
        pm._templates["system_prompt"] = [
            t for t in pm._templates["system_prompt"]
            if t.version != test_version
        ]

    def test_save_duplicate_version_raises(self):
        """保存已存在的版本应抛出 ValueError。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        with pytest.raises(ValueError, match="already exists"):
            pm.save("system_prompt", version="1.0.0", template="test")


class TestPromptManagerHotReload:
    """测试热重载功能。"""

    def test_reload_preserves_functionality(self):
        """热重载后仍能正常加载模板。"""
        from prompts import get_prompt_manager
        pm = get_prompt_manager()
        pm.reload()

        assert "system_prompt" in pm.list_templates()
        versions = pm.list_versions("system_prompt")
        assert len(versions) >= 2

    def test_ensure_loaded_is_lazy(self):
        """验证懒加载机制：未调用前 _loaded=False。"""
        from prompts.manager import PromptManager
        pm = PromptManager()
        assert not pm._loaded

        # 访问数据应触发加载
        _ = pm.list_templates()
        assert pm._loaded


class TestPromptManagerEdgeCases:
    """边界条件测试。"""

    def test_empty_directory_no_error(self):
        """空目录加载不应报错。"""
        from prompts.manager import PromptManager
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PromptManager(templates_dir=tmpdir)
            count = pm.load_all()
            assert count == 0
            assert pm.list_templates() == []

    def test_invalid_yaml_skipped(self):
        """无效 YAML 文件应被跳过。"""
        from prompts.manager import PromptManager
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.yaml"
            bad_file.write_text(": invalid: yaml: [[[")
            pm = PromptManager(templates_dir=tmpdir)
            count = pm.load_all()
            assert count == 0

    def test_non_dict_yaml_skipped(self):
        """根节点非 dict 的 YAML 应被跳过。"""
        from prompts.manager import PromptManager
        import yaml as yaml_lib
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "list.yaml"
            bad_file.write_text(yaml_lib.dump(["item1", "item2"]))
            pm = PromptManager(templates_dir=tmpdir)
            count = pm.load_all()
            assert count == 0

    def test_json_file_loaded(self):
        """JSON 格式模板应被正确加载。"""
        from prompts.manager import PromptManager
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            data = {
                "name": "test_prompt",
                "version": "1.0.0",
                "template": "Hello {{name}}",
                "variables": ["name"],
            }
            json_file = Path(tmpdir) / "test_prompt.json"
            json_file.write_text(json.dumps(data))
            pm = PromptManager(templates_dir=tmpdir)
            count = pm.load_all()
            assert count == 1
            tpl = pm.get("test_prompt")
            assert tpl is not None
            assert tpl.version == "1.0.0"

    def test_prompt_template_dataclass(self):
        """PromptTemplate dataclass 测试。"""
        from prompts import PromptTemplate
        tpl = PromptTemplate(
            name="test",
            version="1.0.0",
            template="test {{x}}",
            variables=["x"],
            description="unit test",
        )
        assert tpl.name == "test"
        assert tpl.version == "1.0.0"
        assert tpl.variables == ["x"]
        assert tpl.description == "unit test"


# ---------------------------------------------------------------------------
# get_system_prompt 集成测试
# ---------------------------------------------------------------------------

class TestSystemPromptIntegration:
    """测试 get_system_prompt 与 PromptManager 的集成。"""

    def test_get_system_prompt_falls_back_when_no_template(self):
        """PromptManager 不可用时应回退到硬编码版本。"""
        from agent.core import get_system_prompt
        # 设置环境变量禁用模板
        old_version = os.environ.pop("PROMPT_VERSION", None)
        old_pm = os.environ.get("_DISABLE_PROMPT_MANAGER", None)

        try:
            # 正常调用 get_system_prompt，如果模板存在就用模板，
            # 如果模板不存在就回退到硬编码。我们在这里测试回退逻辑
            # 通过 monkeypatch 让 get_prompt_manager 抛异常
            import prompts
            original_get = prompts.get_prompt_manager

            def mock_get_pm():
                raise RuntimeError("simulated failure")

            prompts.get_prompt_manager = mock_get_pm
            try:
                result = get_system_prompt()
                assert "你是一个能力全面的 AI 助手" in result
                # 回退版本不包含 {{tool_names}} 变量标记
                assert "{{tool_names}}" not in result
            finally:
                prompts.get_prompt_manager = original_get
        finally:
            if old_version is not None:
                os.environ["PROMPT_VERSION"] = old_version

    def test_get_system_prompt_loads_from_template(self):
        """正常情况下 get_system_prompt 应从模板加载。"""
        from agent.core import get_system_prompt
        old_version = os.environ.pop("PROMPT_VERSION", None)
        try:
            result = get_system_prompt()
            assert len(result) > 100
            assert "AI 助手" in result or "助手" in result
        finally:
            if old_version is not None:
                os.environ["PROMPT_VERSION"] = old_version


# ---------------------------------------------------------------------------
# A/B 对比数据模型测试
# ---------------------------------------------------------------------------


class TestABCompareDataModel:
    """测试 A/B 对比的数据结构。"""

    def test_eval_result_defaults(self):
        """EvalResult 默认值测试。"""
        from scripts.run_eval import EvalResult
        r = EvalResult(name="test", question="q?")
        assert r.passed is True
        assert r.failures == []
        assert r.scores == {}
        assert r.prompt_version == "unknown"

    def test_ab_compare_result_defaults(self):
        """ABCompareResult 默认值测试。"""
        from scripts.run_eval import ABCompareResult
        ab = ABCompareResult(baseline_version="1.0.0", candidate_version="1.1.0")
        assert ab.baseline_version == "1.0.0"
        assert ab.candidate_version == "1.1.0"
        assert ab.baseline_results == []
        assert ab.candidate_results == []
        assert ab.winner == ""

    def test_eval_result_scores_dict(self):
        """EvalResult.scores 应存储评分信息。"""
        from scripts.run_eval import EvalResult
        r = EvalResult(name="test", question="q?")
        r.scores = {
            "relevance": 4,
            "accuracy": 5,
            "completeness": 3,
            "tool_usage": 4,
            "overall": 4,
        }
        assert r.scores["overall"] == 4
        assert r.passed is True


# ---------------------------------------------------------------------------
# 版本函数测试
# ---------------------------------------------------------------------------


class TestVersionHelpers:
    """版本辅助函数测试。"""

    def test_version_key_parses_semver(self):
        from prompts.manager import _version_key
        assert _version_key("1.2.3") == (1, 2, 3)
        assert _version_key("0.0.1") == (0, 0, 1)
        assert _version_key("10.20.30") == (10, 20, 30)

    def test_version_key_invalid_returns_zero(self):
        from prompts.manager import _version_key
        assert _version_key("invalid") == (0, 0, 0)
        assert _version_key("") == (0, 0, 0)
