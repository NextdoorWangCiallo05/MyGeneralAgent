"""代码执行工具：在受限沙箱子进程中运行 Python 片段。

隔离细节见 `app.tools.sandbox`：独立工作目录、最小环境变量、无 shell、
超时与内存上限、输出截断。仅标准库可用（解释器以 `-I -S` 启动）。
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.tools.base import Tool, ToolResult
from app.tools.context import require_tool_context
from app.tools.sandbox import SandboxError, run_python

logger = logging.getLogger("app.tools.python_exec")


async def _python_exec(code: str, timeout: float | None = None) -> ToolResult:
    settings = get_settings()
    payload = (code or "").strip()
    if not payload:
        return ToolResult(status="error", summary="代码为空", content="错误：code 不能为空")

    # 模型可请求更短超时，但不得超过后台配置的上限
    limit = settings.sandbox_exec_timeout
    if timeout is not None:
        try:
            limit = min(max(float(timeout), 1.0), settings.sandbox_exec_timeout)
        except (TypeError, ValueError):
            pass

    try:
        context = require_tool_context()
        result = await run_python(payload, user_id=context.user_id, timeout=limit)
    except RuntimeError as exc:
        return ToolResult(status="error", summary="执行上下文缺失", content=f"错误：{exc}")
    except SandboxError as exc:
        return ToolResult(
            status="error",
            summary=f"沙箱执行失败：{exc}",
            content=f"错误：{exc}",
            data={"code": payload},
        )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.timed_out:
        return ToolResult(
            status="timeout",
            summary=f"执行超时（{limit:g} 秒）",
            content=f"错误：代码执行超过 {limit:g} 秒已被强制终止。请优化算法或缩小数据规模。",
            data={"code": payload, "stdout": stdout, "stderr": stderr},
        )

    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    body = "\n".join(parts) or "（无输出）"

    return ToolResult(
        status="success" if result.exit_code == 0 else "error",
        summary=(
            f"执行完成（退出码 {result.exit_code}）"
            if result.exit_code == 0
            else f"执行出错（退出码 {result.exit_code}）"
        ),
        content=body,
        data={
            "code": payload,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
        },
    )


python_exec_tool = Tool(
    name="python_exec",
    description=(
        "在受限沙箱中执行一段 Python 代码并返回标准输出，适合数据处理、格式转换、算法验证。"
        "只能使用标准库，无法访问网络与宿主机文件；有执行超时与内存上限。"
        "该操作会运行代码，必须经用户确认后才会执行。需要会话已开启沙箱。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的完整 Python 代码（仅标准库）"},
            "timeout": {
                "type": "number",
                "description": "可选的期望超时秒数，实际不会超过后台配置的上限",
            },
        },
        "required": ["code"],
    },
    handler=_python_exec,
    requires_confirmation=True,
    readonly=False,
    sandbox_required=True,
    timeout=0.0,
)


__all__ = ["python_exec_tool"]
