"""提示词管理器 — YAML/JSON 模板版本化 + 热重载 + 变量渲染。

用法:
    from prompts import get_prompt_manager

    pm = get_prompt_manager()
    prompt = pm.render("system_prompt", capabilities=cap_str, tool_names=names_str)
    versions = pm.list_versions("system_prompt")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class PromptTemplate:
    """单条提示词模板。"""

    name: str
    version: str
    template: str
    variables: list[str] = field(default_factory=list)
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""  # 源文件路径，用于热重载定位


# ---------------------------------------------------------------------------
# PromptManager
# ---------------------------------------------------------------------------


class PromptManager:
    """从 YAML/JSON 文件加载提示词模板，支持语义版本号和热重载。

    模板文件存放在 TEMPLATES_DIR（默认 src/prompts/templates/）。
    文件名约定: {template_name}.yaml 或 {template_name}.yml。

    每个 YAML 文件的根节点必须包含:
        version: "1.0.0"
        template: "提示词文本，支持 {{variable}} 占位符"
        variables: [optional, list of variable names]
        description: "optional description"

    一个文件 = 一个模板的一个版本。多版本通过不同文件名或文件内的 versions 列表管理。
    """

    def __init__(self, templates_dir: Path | str | None = None) -> None:
        self.templates_dir = Path(templates_dir) if templates_dir else TEMPLATES_DIR
        # name -> list[PromptTemplate]，按版本降序排列
        self._templates: dict[str, list[PromptTemplate]] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load_all(self) -> int:
        """加载模板目录下所有 YAML/JSON 文件。返回加载的模板数量。"""
        self._templates.clear()
        count = 0

        if not self.templates_dir.exists():
            logger.warning("Templates directory not found: %s", self.templates_dir)
            self._loaded = True
            return 0

        for entry in sorted(self.templates_dir.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() in (".yaml", ".yml"):
                loaded = self._load_yaml_file(entry)
                count += loaded
            elif entry.suffix.lower() == ".json":
                loaded = self._load_json_file(entry)
                count += loaded

        self._loaded = True
        logger.info("Loaded %d prompt templates from %s", count, self.templates_dir)
        return count

    def reload(self) -> int:
        """热重载所有模板（例如通过 API 触发）。"""
        self._templates.clear()
        self._loaded = False
        return self.load_all()

    def _load_yaml_file(self, path: Path) -> int:
        """加载单个 YAML 文件，支持单模板和多版本两种格式。

        单模板格式:
            name: system_prompt
            version: "1.0.0"
            template: |
              你是一个 AI 助手...
            variables: [capabilities, tool_names]

        多版本格式:
            name: system_prompt
            versions:
              - version: "1.1.0"
                template: |
                  新版本内容...
                variables: [...]
              - version: "1.0.0"
                template: |
                  旧版本内容...
                variables: [...]
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            logger.error("Failed to parse YAML: %s — %s", path, exc)
            return 0
        except Exception as exc:
            logger.error("Failed to read: %s — %s", path, exc)
            return 0

        if not isinstance(data, dict):
            return 0

        count = 0
        name = data.get("name", path.stem)
        file_path = str(path)

        # 多版本格式
        if "versions" in data and isinstance(data["versions"], list):
            for ver_entry in data["versions"]:
                if self._add_template_entry(name, ver_entry, file_path):
                    count += 1
        else:
            # 单模板格式
            if self._add_template_entry(name, data, file_path):
                count = 1

        return count

    def _load_json_file(self, path: Path) -> int:
        """加载单个 JSON 文件（格式与 YAML 一致）。"""
        import json

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse JSON: %s — %s", path, exc)
            return 0
        except Exception as exc:
            logger.error("Failed to read: %s — %s", path, exc)
            return 0

        if not isinstance(data, dict):
            return 0

        name = data.get("name", path.stem)
        file_path = str(path)
        count = 0

        if "versions" in data and isinstance(data["versions"], list):
            for ver_entry in data["versions"]:
                if self._add_template_entry(name, ver_entry, file_path):
                    count += 1
        else:
            if self._add_template_entry(name, data, file_path):
                count = 1

        return count

    def _add_template_entry(self, name: str, entry: dict, file_path: str) -> bool:
        """从 dict 构造 PromptTemplate 并加入索引。"""
        version = str(entry.get("version", "0.0.0"))
        template_text = entry.get("template", "")
        if not template_text:
            logger.warning("Empty template for '%s' v%s in %s", name, version, file_path)
            return False

        tpl = PromptTemplate(
            name=name,
            version=version,
            template=template_text,
            variables=list(entry.get("variables", [])),
            description=str(entry.get("description", "")),
            metadata=dict(entry.get("metadata", {})),
            file_path=file_path,
        )

        if name not in self._templates:
            self._templates[name] = []
        self._templates[name].append(tpl)
        # 按版本降序排列（使用 semver 比较）
        self._templates[name].sort(key=lambda t: tuple(map(int, t.version.split("."))), reverse=True)
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, name: str, version: str | None = None) -> PromptTemplate | None:
        """获取指定名称和版本的模板。

        Args:
            name: 模板名称。
            version: 版本号，None 表示最新版本。

        Returns:
            PromptTemplate 或 None。
        """
        self._ensure_loaded()
        entries = self._templates.get(name, [])
        if not entries:
            return None

        if version is None or version == "latest":
            # 已按版本降序排列，第一个即最新
            return entries[0]

        for tpl in entries:
            if tpl.version == version:
                return tpl

        return None

    def list_versions(self, name: str) -> list[str]:
        """列出某个模板的所有版本（降序）。"""
        self._ensure_loaded()
        entries = self._templates.get(name, [])
        return [t.version for t in entries]

    def list_templates(self) -> list[str]:
        """列出所有模板名称。"""
        self._ensure_loaded()
        return sorted(self._templates.keys())

    def list_all(self) -> list[PromptTemplate]:
        """返回所有模板（扁平列表）。"""
        self._ensure_loaded()
        result: list[PromptTemplate] = []
        for entries in self._templates.values():
            result.extend(entries)
        return sorted(result, key=lambda t: (t.name, _version_key(t.version)))

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, name: str, version: str | None = None, **variables) -> str:
        """加载模板并用给定变量渲染。

        Args:
            name: 模板名称。
            version: 版本号，默认最新。
            **variables: 模板变量的值。

        Returns:
            渲染后的字符串。缺失变量会原样保留占位符并记录警告。

        Raises:
            ValueError: 模板不存在。
        """
        tpl = self.get(name, version=version)
        if tpl is None:
            raise ValueError(
                f"Template '{name}' not found"
                + (f" v{version}" if version else "")
            )

        result = tpl.template
        # 检查未提供的变量
        for var in tpl.variables:
            if var not in variables:
                logger.debug("Variable '%s' not provided for template '%s' v%s", var, name, tpl.version)

        # 执行替换（兼容 Jinja2 {{var}} 和 {var} 风格）
        for var_name, var_value in variables.items():
            # Jinja2 风格
            result = result.replace("{{" + var_name + "}}", str(var_value))
            # 简单风格
            result = result.replace("{" + var_name + "}", str(var_value))

        # 清理未填充的占位符（可选：保留原样便于调试）
        return result

    # ------------------------------------------------------------------
    # 保存 / 导出
    # ------------------------------------------------------------------

    def save(self, name: str, version: str, template: str,
             variables: list[str] | None = None,
             description: str = "",
             metadata: dict[str, Any] | None = None) -> Path:
        """保存模板为新版本 YAML 文件。

        Returns:
            写入的文件路径。

        Raises:
            ValueError: 版本号已存在。
        """
        self._ensure_loaded()

        # 检查版本唯一性
        existing = self.get(name, version=version)
        if existing is not None:
            raise ValueError(f"Template '{name}' v{version} already exists")

        data: dict[str, Any] = {
            "name": name,
            "version": version,
            "template": template,
            "variables": variables or [],
            "description": description,
            "metadata": metadata or {},
        }

        file_name = f"{name}__v{version.replace('.', '_')}.yaml"
        file_path = self.templates_dir / file_name
        self.templates_dir.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False,
                           sort_keys=False, width=120)

        # 更新内存
        tpl = PromptTemplate(
            name=name, version=version, template=template,
            variables=list(variables or []), description=description,
            metadata=dict(metadata or {}), file_path=str(file_path),
        )
        if name not in self._templates:
            self._templates[name] = []
        self._templates[name].append(tpl)
        self._templates[name].sort(key=lambda t: tuple(map(int, t.version.split("."))), reverse=True)

        logger.info("Saved template '%s' v%s → %s", name, version, file_path)
        return file_path

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """懒加载：首次访问时自动加载所有模板。"""
        if not self._loaded:
            self.load_all()

    def __repr__(self) -> str:
        return f"<PromptManager templates={len(self._templates)} dir={self.templates_dir}>"


# ---------------------------------------------------------------------------
# 模块单例
# ---------------------------------------------------------------------------


_prompt_manager: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    """获取模块级单例 PromptManager。"""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _version_key(v: str) -> tuple[int, ...]:
    """将语义版本号转为可比较的元组。"""
    try:
        return tuple(map(int, v.split(".")))
    except (ValueError, TypeError):
        return (0, 0, 0)
