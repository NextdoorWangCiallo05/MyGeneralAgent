"""工具注册表：注册、schema 导出、参数校验、超时执行与错误包装。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable

from app.config import get_settings
from app.tools.base import Tool, ToolResult

logger = logging.getLogger("app.tools")


class ToolRegistry:
    """工具注册与执行中心。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ---------------------------------------------------------------- 注册
    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具已存在: {tool.name}")
        self._tools[tool.name] = tool

    def register_many(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    # ---------------------------------------------------------------- schema
    def schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        """导出 OpenAI 兼容的 tools 数组；`only` 用于按会话裁剪可用工具。"""
        tools = self._tools.values()
        if only is not None:
            allow = set(only)
            tools = [t for t in tools if t.name in allow]
        return [t.to_openai_schema() for t in tools]

    def available_names(
        self,
        *,
        sandbox_enabled: bool,
        knowledge_enabled: bool = True,
        attachments_enabled: bool = True,
    ) -> list[str]:
        """当前环境下可用的工具名。

        - 沙箱关闭时剔除沙箱类工具；
        - 知识库不可检索时剔除知识库类工具（避免模型反复调用必然失败的工具）；
        - 会话里没有任何附件时剔除附件类工具。
        """
        return [
            tool.name
            for tool in self._tools.values()
            if (sandbox_enabled or not tool.sandbox_required)
            and (knowledge_enabled or not tool.knowledge_required)
            and (attachments_enabled or not tool.attachment_required)
        ]

    def schemas_for(
        self,
        *,
        sandbox_enabled: bool,
        knowledge_enabled: bool = True,
        attachments_enabled: bool = True,
    ) -> list[dict[str, Any]]:
        """按运行环境导出可用工具 schema。"""
        return self.schemas(
            self.available_names(
                sandbox_enabled=sandbox_enabled,
                knowledge_enabled=knowledge_enabled,
                attachments_enabled=attachments_enabled,
            )
        )

    def is_sandbox_required(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.sandbox_required)

    def is_knowledge_required(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.knowledge_required)

    def is_attachment_required(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.attachment_required)

    # ---------------------------------------------------------------- 执行
    async def execute(
        self, name: str, args: dict[str, Any], *, approved: bool = False
    ) -> ToolResult:
        """执行工具。

        `approved=True` 表示调用方已完成人工确认流程（见 `core.confirmations`）；
        未确认时，副作用类工具不会被真正执行。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(status="error", summary=f"未知工具: {name}", content=f"错误：工具 `{name}` 不存在")

        if tool.requires_confirmation and not approved:
            return ToolResult(
                status="needs_confirmation",
                summary=f"工具 {name} 需要人工确认",
                content=f"错误：工具 `{name}` 需要用户确认后才能执行",
            )

        timeout = tool.timeout or get_settings().tool_timeout
        try:
            async with asyncio.timeout(timeout):
                return await tool.handler(**(args or {}))
        except asyncio.TimeoutError:
            logger.warning("工具超时: %s", name)
            return ToolResult(status="timeout", summary=f"{name} 执行超时", content=f"错误：工具 `{name}` 执行超时")
        except TypeError as exc:
            logger.warning("工具参数错误: %s -> %s", name, exc)
            return ToolResult(
                status="error",
                summary=f"{name} 参数错误",
                content=f"错误：调用工具 `{name}` 的参数不合法（{exc}）",
            )
        except Exception as exc:  # noqa: BLE001 - 工具异常需包装后回喂给模型
            logger.exception("工具执行异常: %s", name)
            return ToolResult(status="error", summary=f"{name} 执行失败", content=f"错误：工具 `{name}` 执行失败（{exc}）")


def build_default_registry() -> ToolRegistry:
    """构建默认工具集（8 个工具）。

    - 只读类：calculator、web_search、fetch_url、read_file、knowledge_search、read_attachment
    - 副作用类（需人工确认）：write_file、python_exec
    - 沙箱类（需会话开启沙箱）：read_file、write_file、python_exec
    - 知识库类（需已启用向量化且存在已索引片段）：knowledge_search
    - 附件类（需会话里已有附件）：read_attachment
    """
    from app.tools.attachments import attachment_read_tool
    from app.tools.calculator import calculator_tool
    from app.tools.fetch_url import fetch_url_tool
    from app.tools.files import read_file_tool, write_file_tool
    from app.tools.knowledge import knowledge_search_tool
    from app.tools.python_exec import python_exec_tool
    from app.tools.web_search import web_search_tool

    registry = ToolRegistry()
    registry.register_many(
        [
            calculator_tool,
            web_search_tool,
            fetch_url_tool,
            read_file_tool,
            write_file_tool,
            python_exec_tool,
            knowledge_search_tool,
            attachment_read_tool,
        ]
    )
    return registry
