"""OpenAI 兼容的 function-calling 客户端。

只用 ``requests``，不引入 SDK —— 这样"模型能不能用"这件事只取决于一个 URL 和一个
Key，不取决于某个包的版本。

**这里没有重试逻辑，是刻意的。** 模型调用失败时，正确的行为是把失败原因如实告诉
操作员（"模型不可用，无法继续排查"），而不是默默重试三次让人对着转圈等 90 秒。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import DEFAULT_LLM_BASE_URL, DEFAULT_LLM_MODEL, llm_credentials


class LLMError(RuntimeError):
    """模型调用失败。调用方应把它变成一句给人看的话，而不是堆栈。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str = ""


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
    ):
        # 凭据解析只有一个来源（config.llm_credentials），避免这里和 CLI
        # 各自维护一份默认值、然后慢慢漂移。
        env_base, env_key, env_model = llm_credentials()
        self.base_url = (base_url or env_base or DEFAULT_LLM_BASE_URL).rstrip("/")
        self.api_key = api_key or env_key or ""
        self.model = model or env_model or DEFAULT_LLM_MODEL
        self.timeout = timeout
        self.calls = 0
        self.total_tokens = 0

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> LLMReply:
        if not self.available:
            raise LLMError("未配置模型 API Key（DEEPSEEK_API_KEY / OMAGENT_LLM_API_KEY）")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise LLMError(f"模型调用超时（{self.timeout}s）") from exc
        except requests.RequestException as exc:
            raise LLMError(f"模型调用失败：{exc}") from exc

        if resp.status_code != 200:
            body = resp.text[:400]
            raise LLMError(f"模型返回 {resp.status_code}：{body}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"模型返回的不是 JSON：{resp.text[:200]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"模型没有返回任何选择：{str(data)[:200]}")

        message = choices[0].get("message") or {}
        self.calls += 1
        usage = data.get("usage") or {}
        self.total_tokens += int(usage.get("total_tokens") or 0)

        return LLMReply(
            content=message.get("content") or "",
            tool_calls=[_parse_tool_call(tc) for tc in (message.get("tool_calls") or [])],
            finish_reason=choices[0].get("finish_reason") or "",
            usage=usage,
        )


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    """解析一次工具调用。

    参数 JSON 解析失败**不抛异常**——模型偶尔会吐出半个 JSON。把它变成一个带
    ``parse_error`` 的 ToolCall，让上层把"你的参数不是合法 JSON"喂回给模型，
    它下一轮通常就能改对。直接崩掉会让一次可恢复的小失误变成一次失败的排查。
    """
    fn = raw.get("function") or {}
    name = fn.get("name") or ""
    raw_args = fn.get("arguments") or "{}"
    call = ToolCall(id=raw.get("id") or f"call_{name}", name=name)
    if isinstance(raw_args, dict):
        call.arguments = raw_args
        return call
    try:
        parsed = json.loads(raw_args or "{}")
        call.arguments = parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError as exc:
        call.parse_error = f"参数不是合法 JSON（{exc}）：{raw_args[:200]}"
    return call
