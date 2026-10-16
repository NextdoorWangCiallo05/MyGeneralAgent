"""沙箱隔离层：目录白名单 + 路径逃逸防护 + 受限子进程执行。

对应《技术设计文档》4.8 节，本期采用**受限子进程**方案：

- 每个用户一个独立沙箱根目录（`SANDBOX_ROOT/<user_id>`），互不可见；
- 所有路径先规范化再校验，拒绝绝对路径、`..` 与符号链接逃逸；
- 子进程不使用 shell、以列表方式传参，杜绝命令注入；
- 清空环境变量（只保留最小集合），避免宿主凭据泄漏给被执行的代码；
- 限制执行超时、单路输出大小与虚拟内存（`resource` 仅在 Unix 生效，其余平台自动跳过）；
- 用 `-I -S` 启动解释器：忽略环境变量与用户 site-packages，缩小可用攻击面。

后续可平滑升级为容器 / 微虚拟机隔离（限制 CPU、网络），上层工具无需改动。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("app.sandbox")

#: 传给子进程的环境变量白名单（其余一律不继承）
_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "LANG",
    "LC_ALL",
)

#: 子进程启动前注入的自限代码：统一输出编码，并在可用时限制虚拟内存。
_PREAMBLE = """\
import sys as _sys

try:
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import resource as _resource

    _limit = {memory_limit}
    if _limit > 0:
        _resource.setrlimit(_resource.RLIMIT_AS, (_limit, _limit))
except Exception:
    pass
"""


class SandboxError(Exception):
    """沙箱内的非法操作或执行失败（路径逃逸、超时、启动失败等）。"""


@dataclass(slots=True)
class ExecResult:
    """受限子进程的执行结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


# ---------------------------------------------------------------- 路径
def sandbox_root() -> Path:
    """沙箱总根目录（不存在则创建）。"""
    root = Path(get_settings().sandbox_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def user_root(user_id: str) -> Path:
    """用户级沙箱根目录（不存在则创建）。"""
    root = sandbox_root() / _safe_segment(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_path(user_id: str, relative: str) -> Path:
    """把相对路径解析到用户沙箱目录内；越界一律抛 `SandboxError`。

    这是所有文件类工具的唯一入口，防止 `../`、绝对路径与符号链接逃逸。
    """
    text = (relative or "").strip()
    if not text:
        raise SandboxError("路径不能为空")
    if "\x00" in text:
        raise SandboxError("路径包含非法字符")

    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        raise SandboxError("只允许沙箱内的相对路径")

    root = user_root(user_id)
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise SandboxError("路径超出沙箱范围")
    return resolved


def display_path(user_id: str, path: Path) -> str:
    """把绝对路径还原为沙箱内相对路径，便于回显给模型。"""
    try:
        return path.relative_to(user_root(user_id)).as_posix()
    except ValueError:  # pragma: no cover - 理论上不会发生
        return path.name


def _safe_segment(value: str) -> str:
    """把用户 ID 之类的外部输入压成安全的单层目录名。"""
    cleaned = "".join(ch for ch in (value or "") if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise SandboxError("非法的沙箱目录标识")
    return cleaned[:64]


# ---------------------------------------------------------------- 子进程
async def run_python(
    code: str,
    *,
    user_id: str,
    timeout: float | None = None,
    memory_mb: int | None = None,
    max_output_bytes: int | None = None,
) -> ExecResult:
    """在受限子进程中执行一段 Python 代码。

    每次执行使用独立临时工作目录，结束后清理；超时会强制 kill。
    """
    settings = get_settings()
    timeout = settings.sandbox_exec_timeout if timeout is None else timeout
    memory_mb = settings.sandbox_memory_mb if memory_mb is None else memory_mb
    max_output = settings.sandbox_max_output_bytes if max_output_bytes is None else max_output_bytes

    if not (code or "").strip():
        raise SandboxError("代码不能为空")

    workdir = user_root(user_id) / "_exec" / uuid.uuid4().hex
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / "main.py"
    preamble = _PREAMBLE.format(memory_limit=max(memory_mb, 0) * 1024 * 1024)
    script.write_text(preamble + code, encoding="utf-8")

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            str(script),
            cwd=str(workdir),
            env=_child_env(workdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        _cleanup(workdir)
        raise SandboxError(f"无法启动沙箱进程：{exc}") from exc

    timed_out = False
    try:
        try:
            async with asyncio.timeout(timeout):
                raw_out, raw_err = await process.communicate()
        except TimeoutError:
            timed_out = True
            await _kill(process)
            raw_out, raw_err = b"", b""
        except BaseException:
            # 上层取消（用户停止生成 / 注册表超时）：必须回收子进程，避免残留
            await _kill(process)
            raise

        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=_truncate(raw_out.decode("utf-8", "replace"), max_output),
            stderr=_truncate(raw_err.decode("utf-8", "replace"), max_output),
            timed_out=timed_out,
        )
    finally:
        _cleanup(workdir)


def _child_env(workdir: Path) -> dict[str, str]:
    """最小化子进程环境变量，避免泄漏宿主密钥。"""
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
    env.update(
        {
            "HOME": str(workdir),
            "TEMP": str(workdir),
            "TMP": str(workdir),
            "TMPDIR": str(workdir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "*",
        }
    )
    return env


def _creation_flags() -> int:
    """Windows 下避免弹出控制台窗口。"""
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - 竞态
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, asyncio.TimeoutError):  # pragma: no cover - 极少发生
        logger.warning("沙箱进程未能及时退出: pid=%s", process.pid)


def _truncate(text: str, limit: int) -> str:
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    kept = data[:limit].decode("utf-8", "ignore")
    return f"{kept}\n…（输出超过 {limit} 字节，已截断）"


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "ExecResult",
    "SandboxError",
    "display_path",
    "resolve_path",
    "run_python",
    "sandbox_root",
    "user_root",
]
