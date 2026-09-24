"""把集群工具集暴露成标准 MCP server（stdio 传输）。

## 为什么只暴露只读工具

MCP 让任何客户端（Claude Desktop、Cursor、自研 agent）直接调用我们的工具。
诱惑是"把 15 个只读 + 10 个写全挂上去"，但那样等于**绕过整个审批架构**：
写操作的执行权会从"人点卡片"变成"MCP 客户端自己决定"。

所以这里沿用项目一贯的做法——**够不着的口子不开**，和 `tools.FORBIDDEN`
是同一个思路：

- `tools/list` 只列只读工具。写工具**根本不出现在清单里**，
  客户端看不见也就不会调（不是"调了再拒绝"，是压根没有）。
- `tools/call` 对任何写工具或未登记的工具一律拒绝，fail closed。
- 需要改动集群？去监控台，那里有人、有 dry-run、有审计、有命名空间白名单。

代价是 MCP 这一侧不能改集群。这是**故意的**：MCP 的信任边界是"客户端自己的
模型"，而我们不接受那个模型作为审批人。

## 协议

JSON-RPC 2.0 over stdio，按行分隔（MCP 的 stdio 传输就是 NDJSON，不是
Content-Length 分帧）。手写而不是拉 SDK——和 `llm.py` 一样，标准库够用就不引依赖，
也顺便让协议层可测：`handle()` 是纯函数，不碰 stdin。
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any, IO

from . import safety
from .tools import FORBIDDEN, READONLY_TOOLS, TOOLS, is_mutating

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "omagent"
SERVER_VERSION = "1.0.0"

# JSON-RPC 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolRefused(RuntimeError):
    """调用被安全边界拒绝（不是工具本身报错）。"""


class McpServer:
    """一个 MCP server 实例 = 一个命名空间视角的只读工具网关。"""

    def __init__(self, agent: Any, namespace: str = "demo"):
        self.agent = agent
        self.namespace = namespace

    # ------------------------------------------------------------ 工具清单

    def list_tools(self) -> list[dict[str, Any]]:
        """只列只读工具。写工具不在清单里——看不见就调不到。"""
        out = []
        for name in READONLY_TOOLS:
            spec = TOOLS[name]
            # 必须深拷贝。schema 是**嵌套** dict，浅拷贝会让下面塞默认值的
            # 那一步直接改到 TOOLS 里的全局 schema 上——先连 staging 再连 demo，
            # 第二个连接就会拿到第一个连接的命名空间。测试里表现为顺序相关的失败。
            schema = copy.deepcopy(spec.schema) or {"type": "object", "properties": {}}
            # 客户端不一定知道该填哪个命名空间，给个默认值省一轮往返
            props = schema.setdefault("properties", {})
            if "namespace" in props and "default" not in props["namespace"]:
                props["namespace"] = {**props["namespace"], "default": self.namespace}
            out.append({
                "name": spec.name,
                "description": spec.summary,
                "inputSchema": schema,
            })
        return out

    # ------------------------------------------------------------ 工具调用

    def call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        """执行一个只读工具，返回 MCP 的 content 结构。

        写工具、禁止动作、未登记的名字都在这里被拦下。注意 ``is_mutating``
        对未登记工具默认返回 True，所以这里的判断是 fail closed 的。
        """
        if name in FORBIDDEN:
            raise ToolRefused(
                f"{name} 属于禁止动作：{FORBIDDEN[name]}"
            )
        if is_mutating(name):
            if name in TOOLS:
                raise ToolRefused(
                    f"{name} 是写操作，MCP 这一侧不提供执行权。"
                    f"请在 O&M 监控台发起，由运维人员确认后执行。"
                )
            raise ToolRefused(f"工具 {name!r} 未登记，拒绝调用。")

        params = dict(arguments or {})
        # 命名空间不给就用本 server 的默认值，但**不覆盖**显式传入的
        params.setdefault("namespace", self.namespace)

        try:
            raw = self.agent.run_readonly(name, params)
        except Exception as exc:  # noqa: BLE001
            # 工具自身失败要如实回给客户端，不要伪装成协议错误
            return {"content": [{"type": "text",
                                 "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True}

        # 集群里读到的文本是不可信输入，和监控台走同一套围栏 + 计分。
        # MCP 客户端那边的模型面对的注入面和我们的模型完全一样。
        text, findings = safety.wrap(name, raw, params)
        content = []
        if findings:
            content.append({"type": "text", "text":
                            "⚠️ 本结果中检出提示注入信号，已按不可信数据处理："
                            + safety.summarize(findings)})
        content.append({"type": "text", "text": text})
        return {"content": content}

    # ------------------------------------------------------------ JSON-RPC

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息。通知类返回 None（不回应）。"""
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return self._ok(msg_id, {})
        if method == "tools/list":
            return self._ok(msg_id, {"tools": self.list_tools()})
        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                return self._err(msg_id, INVALID_PARAMS, "arguments 必须是对象")
            try:
                return self._ok(msg_id, self.call_tool(name, args))
            except ToolRefused as exc:
                # 安全边界拒绝：以工具执行错误的形式回给客户端，而不是协议错误。
                # 客户端看到的是"这个工具调不了"，而不是"服务器坏了"。
                return self._ok(msg_id, {
                    "content": [{"type": "text", "text": f"⛔ 拒绝执行：{exc}"}],
                    "isError": True,
                })
        # 资源/提示我们没实现，但要让客户端知道"服务器还活着"，返回空列表而不是报错
        if method == "resources/list":
            return self._ok(msg_id, {"resources": []})
        if method == "prompts/list":
            return self._ok(msg_id, {"prompts": []})
        if method == "tools/subscribe":
            return self._ok(msg_id, {})
        return self._err(msg_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")

    @staticmethod
    def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    # ------------------------------------------------------------ stdio 循环

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
        """按行读 JSON-RPC，按行写回应。EOF 即退出。"""
        fin = stdin or sys.stdin
        fout = stdout or sys.stdout
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._write(fout, self._err(None, PARSE_ERROR, "不是合法 JSON"))
                continue
            if not isinstance(msg, dict):
                self._write(fout, self._err(None, INVALID_REQUEST, "消息必须是对象"))
                continue
            try:
                reply = self.handle(msg)
            except Exception as exc:  # noqa: BLE001
                # 任何未预期的异常都要变成合法回应，否则客户端会一直等
                reply = self._err(msg.get("id"), INTERNAL_ERROR,
                                  f"{type(exc).__name__}: {exc}")
            if reply is not None:
                self._write(fout, reply)
        return 0

    @staticmethod
    def _write(fout: IO[str], payload: dict[str, Any]) -> None:
        fout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        fout.flush()
