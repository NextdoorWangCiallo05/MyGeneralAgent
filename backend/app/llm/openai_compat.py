"""OpenAI 兼容协议的流式聊天客户端。

「接入其他家的模型」在本项目里落到这一个实现上：只要对方提供 OpenAI 兼容端点
（DeepSeek / OpenAI / Moonshot / 通义千问 / 智谱 / Ollama / 自建代理都一样），
协议层无需改动；差异（服务地址、默认模型、是否需要密钥、是否接受 stream_options）
由 `app.llm.providers` 的厂商预设描述。

- `POST {base_url}/chat/completions`，`stream=true`
- 逐行解析 `data:` 帧，`[DONE]` 结束
- 工具调用分片按 `index` 聚合后一次性下发
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from app.llm.client import ChatChunk, ChunkKind, LLMNotConfiguredError, LLMRequestError

logger = logging.getLogger("app.llm")


class OpenAICompatClient:
    """任意 OpenAI 兼容端点的流式客户端。

    所有参数都由调用方（`app.llm.providers.build_llm_client`）显式传入，
    客户端自身不读环境变量，避免配置来源分散在多层。
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        timeout: float = 60.0,
        label: str = "模型服务",
        requires_api_key: bool = True,
        stream_usage: bool = True,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")
        self._model = model
        self._timeout = timeout
        #: 厂商展示名，仅用于日志与错误文案
        self._label = label
        self._requires_api_key = requires_api_key
        #: 少数端点（如 Ollama、部分网关）不接受 stream_options，按预设关闭
        self._stream_usage = stream_usage

    @property
    def default_model(self) -> str:
        return self._model

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        if self._requires_api_key and not self._api_key:
            raise LLMNotConfiguredError(f"{self._label} 的 API-Key 未配置")
        if not self._base_url:
            raise LLMNotConfiguredError(f"{self._label} 的服务地址（base_url）未配置")

        payload: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if self._stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        url = f"{self._base_url}/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        # 本地部署（如 Ollama）通常不校验密钥，此时不带 Authorization 头
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # index -> 聚合中的 tool_call
        buffer: dict[int, dict[str, Any]] = {}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise LLMRequestError(
                            f"{self._label} 返回 {response.status_code}: {body[:300]}"
                        )

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if not data or data == "[DONE]":
                            continue

                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            logger.debug("忽略无法解析的 SSE 帧: %s", data[:200])
                            continue

                        usage = chunk.get("usage")
                        if usage:
                            yield ChatChunk(kind=ChunkKind.USAGE.value, usage=usage)

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        # DeepSeek 等推理模型会单独返回思考内容，其余厂商没有该字段
                        if delta.get("reasoning_content"):
                            yield ChatChunk(
                                kind=ChunkKind.REASONING.value, text=delta["reasoning_content"]
                            )
                        if delta.get("content"):
                            yield ChatChunk(kind=ChunkKind.CONTENT.value, text=delta["content"])

                        for call in delta.get("tool_calls") or []:
                            index = call.get("index", 0)
                            slot = buffer.setdefault(
                                index,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["function"]["name"] = function["name"]
                            if function.get("arguments"):
                                slot["function"]["arguments"] += function["arguments"]
        except LLMRequestError:
            raise
        except httpx.HTTPError as exc:
            raise LLMRequestError(f"模型请求失败: {exc}") from exc

        if buffer:
            calls = [buffer[i] for i in sorted(buffer) if buffer[i]["function"]["name"]]
            if calls:
                yield ChatChunk(kind=ChunkKind.TOOL_CALLS.value, tool_calls=calls)

        yield ChatChunk(kind=ChunkKind.DONE.value)
