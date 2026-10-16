"""人工确认：让 Agent 在副作用工具执行前挂起，等待用户在前端决定。

工作方式：
1. Agent 遇到 `requires_confirmation=True` 的工具时，先下发 `tool_confirm` 事件；
2. 随后 `await` 本注册表，SSE 连接保持打开（不占用额外连接）；
3. 前端弹出确认后调用 `POST /chat/confirm`，由该接口 `resolve` 唤醒；
4. 超时、用户停止生成或连接中断，一律按**拒绝**处理，绝不默认放行。

与「停止生成」一样，待确认表保存在进程内；多实例部署需改为 Redis（见 README 里程碑）。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("app.confirm")


class ConfirmationRegistry:
    """待确认请求表（键为工具调用 ID）。"""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[bool]] = {}

    @property
    def pending(self) -> list[str]:
        return [key for key, fut in self._pending.items() if not fut.done()]

    def create(self, key: str) -> asyncio.Future[bool]:
        """预先登记待确认请求。

        **必须在 `tool_confirm` 事件下发之前调用**：否则用户在事件到达后
        立刻点确认，而 Agent 尚未开始等待，`resolve` 就会找不到目标。
        """
        self._discard(key)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        return future

    async def wait(self, key: str, timeout: float) -> bool:
        """登记并等待用户决定（便捷写法，等价于 `create` + `wait_future`）。"""
        return await self.wait_future(key, self.create(key), timeout)

    async def wait_future(
        self, key: str, future: asyncio.Future[bool], timeout: float
    ) -> bool:
        """等待已登记的请求；超时返回 False（拒绝）。

        `asyncio.CancelledError` 不在此处吞掉：用户点「停止生成」时应让取消
        正常向上传播，而不是退化成一个「已拒绝」的工具结果。
        """
        try:
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            logger.info("工具确认超时，按拒绝处理: %s", key)
            return False
        finally:
            self._discard(key)

    def resolve(self, key: str, approved: bool) -> bool:
        """由 API 层调用：用户已做出决定。返回 False 表示该请求不存在或已结束。"""
        future = self._pending.get(key)
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True

    def cancel_all(self) -> None:
        """清空全部待确认请求（用于停止生成或测试隔离）。"""
        for key in list(self._pending):
            self._discard(key)

    def _discard(self, key: str) -> None:
        future = self._pending.pop(key, None)
        if future is not None and not future.done():
            future.cancel()


_registry: ConfirmationRegistry | None = None


def get_confirmations() -> ConfirmationRegistry:
    """进程内单例。"""
    global _registry
    if _registry is None:
        _registry = ConfirmationRegistry()
    return _registry


def reset_confirmations() -> None:
    """清空单例（测试或切换运行环境时使用）。"""
    global _registry
    _registry = None


__all__ = ["ConfirmationRegistry", "get_confirmations", "reset_confirmations"]
