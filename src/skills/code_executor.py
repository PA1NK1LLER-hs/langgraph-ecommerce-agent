"""代码编写与执行技能 — Agent 可直接调用的 @tool。

安全策略：
- Python / Shell：优先在 Docker 沙箱中执行（无网络、内存 256MB、只读文件系统）
- Docker 不可用时，回退到 subprocess 并记录安全警告
- PowerShell / Bat：仅限 Windows，subprocess 执行并记录警告
"""

import logging
import shutil
import subprocess
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from langchain_core.tools import tool

from config import DATA_DIR

warnings.filterwarnings("ignore", category=DeprecationWarning)

logger = logging.getLogger(__name__)

CODE_LOG_DIR = Path(DATA_DIR) / "code_logs"
CODE_LOG_DIR.mkdir(parents=True, exist_ok=True)
PROJECT_ROOT = Path(DATA_DIR).parent.parent

LANG_CONFIG = {
    "python":     {"ext": ".py", "cmd": ["python"]},
    "shell":      {"ext": ".sh", "cmd": ["bash"]},
    "powershell": {"ext": ".ps1", "cmd": ["powershell", "-ExecutionPolicy", "Bypass", "-File"]},
    "bat":        {"ext": ".bat", "cmd": ["cmd", "/c"]},
}

# Docker 沙箱可以安全执行的语言
_DOCKER_SANDBOX_LANGS = {"python", "shell"}

# Docker 沙箱配置
_SANDBOX_IMAGE = "python:3.11-slim"
_SANDBOX_MEMORY = "256m"
_SANDBOX_CPUS = "1"
_SANDBOX_TIMEOUT = 60


class ExecuteCodeArgs(BaseModel):
    code: str = Field(description="要编写/运行的代码文本")
    language: str = Field(default="python", description="语言类型 — python / shell / powershell / bat")
    filename: str = Field(default="", description="自定义文件名（不含扩展名，可选）")
    run: bool = Field(default=True, description="是否自动运行（默认 true）")
    cwd: str = Field(default="", description="工作目录（默认为项目根目录）")


@tool(args_schema=ExecuteCodeArgs)
def execute_code(
    code: str,
    language: str = "python",
    filename: str = "",
    run: bool = True,
    cwd: str = "",
) -> dict[str, Any]:
    """编写并运行代码。当用户要求写代码、运行脚本、自动生成程序、操作文件（复制/移动/删除/搜索）、
批量处理、数据分析时使用此工具。注意: 只能写命令行/终端程序，不要写 GUI/图形界面代码（会超时）。"""
    lang = language.lower()
    cfg = LANG_CONFIG.get(lang)
    if cfg is None:
        return {
            "status": "error",
            "message": f"不支持的语言 '{language}'。可用: {list(LANG_CONFIG.keys())}",
        }

    ext = cfg["ext"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = filename or f"code_{timestamp}"
    safe_name = Path(name).stem
    filepath = CODE_LOG_DIR / f"{safe_name}_{timestamp}{ext}"

    log_lines = [
        "# Code Execution Log",
        f"# Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Language: {lang}",
        f"# Run: {run}",
        "",
        code,
    ]

    try:
        filepath.write_text("\n".join(log_lines), encoding="utf-8")
    except Exception as exc:
        return {"status": "error", "message": f"写入文件失败: {exc}"}

    result: dict[str, Any] = {
        "status": "success",
        "file": str(filepath),
        "language": lang,
        "code_length": len(code),
    }

    if not run:
        result["output"] = "(未运行)"
        return result

    working_dir = Path(cwd) if cwd else PROJECT_ROOT
    working_dir.mkdir(parents=True, exist_ok=True)

    # ── 优先尝试 Docker 沙箱 ──
    if lang in _DOCKER_SANDBOX_LANGS and _docker_available():
        sandbox_result = _run_in_docker(lang, filepath, working_dir)
        if sandbox_result is not None:
            _append_output(filepath, sandbox_result.get("exit_code", -1),
                           sandbox_result.get("output", ""))
            result.update(sandbox_result)
            return result
        logger.warning("Docker 沙箱不可用，回退到 subprocess（无隔离）")

    # ── 回退：subprocess（无沙箱隔离）──
    if lang not in _DOCKER_SANDBOX_LANGS:
        logger.warning("语言 '%s' 不支持 Docker 沙箱，直接在宿主机执行", lang)

    try:
        if lang == "python":
            proc = subprocess.run(
                ["python", str(filepath)],
                capture_output=True, text=True, timeout=_SANDBOX_TIMEOUT,
                cwd=str(working_dir), encoding="utf-8", errors="replace",
            )
        elif lang == "shell":
            proc = subprocess.run(
                ["bash", str(filepath)],
                capture_output=True, text=True, timeout=_SANDBOX_TIMEOUT,
                cwd=str(working_dir), encoding="utf-8", errors="replace",
            )
        elif lang == "powershell":
            # ⚠️ 安全：不再允许宿主机 PowerShell 执行（防止容器逃逸）
            # 如需 PowerShell，请使用 Docker 沙箱模式
            return {
                "status": "error",
                "message": "PowerShell 执行需要 Docker 沙箱环境。宿主机直接执行已被禁用。",
            }
        elif lang == "bat":
            proc = subprocess.run(
                ["cmd", "/c", str(filepath)],
                capture_output=True, text=True, timeout=_SANDBOX_TIMEOUT,
                cwd=str(working_dir), encoding="utf-8", errors="replace",
            )
        else:
            return {"status": "error", "message": f"不支持的语言 '{language}'"}

        stdout = proc.stdout.strip() if proc.stdout else ""
        stderr = proc.stderr.strip() if proc.stderr else ""
        exit_code = proc.returncode

        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}"
        if not output:
            output = f"(退出码 {exit_code})"

        result["output"] = output
        result["exit_code"] = exit_code
        _append_output(filepath, exit_code, output)

    except subprocess.TimeoutExpired:
        result["status"] = "error"
        result["message"] = f"代码执行超时 ({_SANDBOX_TIMEOUT}s)"
        result["output"] = "[超时]"
    except FileNotFoundError as exc:
        result["status"] = "error"
        result["message"] = f"找不到执行器: {exc}"
        result["output"] = f"[错误] {exc}"
    except Exception as exc:
        result["status"] = "error"
        result["message"] = f"执行异常: {exc}"
        result["output"] = f"[异常] {exc}"

    return result


# ---------------------------------------------------------------------------
# Docker 沙箱
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """检查 Docker 是否可用。"""
    return shutil.which("docker") is not None


def _run_in_docker(lang: str, filepath: Path, working_dir: Path) -> dict | None:
    """在 Docker 沙箱中执行代码文件。

    安全限制：
    - --network=none     禁止网络访问
    - --memory=256m       内存上限
    - --cpus=1            CPU 上限
    - --read-only         只读根文件系统
    - --tmpfs /tmp        /tmp 为临时内存文件系统
    - --rm                容器退出后自动删除
    """
    container_code_path = f"/code/{filepath.name}"
    abs_filepath = filepath.resolve()

    cmd: list[str]
    if lang == "python":
        cmd = ["python", container_code_path]
    elif lang == "shell":
        cmd = ["bash", container_code_path]
    else:
        return None

    docker_args = [
        "docker", "run",
        "--rm",
        "--network=none",
        f"--memory={_SANDBOX_MEMORY}",
        f"--cpus={_SANDBOX_CPUS}",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
        "-v", f"{abs_filepath}:{container_code_path}:ro",
        "-w", "/code",
        _SANDBOX_IMAGE,
        *cmd,
    ]

    try:
        proc = subprocess.run(
            docker_args,
            capture_output=True, text=True, timeout=_SANDBOX_TIMEOUT + 10,
            encoding="utf-8", errors="replace",
        )
        stdout = proc.stdout.strip() if proc.stdout else ""
        stderr = proc.stderr.strip() if proc.stderr else ""
        exit_code = proc.returncode

        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}"
        if not output:
            output = f"(退出码 {exit_code})"

        return {"output": output, "exit_code": exit_code, "sandbox": "docker"}

    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "message": f"沙箱执行超时 ({_SANDBOX_TIMEOUT}s)",
            "output": "[超时]",
        }
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Docker 沙箱执行异常: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _append_output(filepath: Path, exit_code: int, output: str) -> None:
    """将执行输出追加写入日志文件。"""
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write("\n\n# --- Execution Output ---\n")
            f.write(f"# Exit Code: {exit_code}\n")
            f.write(output)
    except Exception:
        pass


def list_code_logs(count: int = 20) -> list[dict]:
    """列出最近的代码日志文件。"""
    files = sorted(CODE_LOG_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files[:count]:
        result.append({
            "name": f.name,
            "size": f.stat().st_size,
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return result
