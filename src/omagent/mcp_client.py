"""MCP **客户端**：把外部 MCP server 的工具接进来给 Agent 用。

## 和 mcp.py 的关系

- ``mcp.py``（server）：把**本项目的**只读工具借出去给别人的客户端。
- 本模块（client）：把**别人的**MCP server 工具接进来给自己用。

两个方向的信任边界完全不同，所以写在两个文件里：

| | 借出去 | 接进来 |
|---|---|---|
| 谁在跑 | 我们的代码 | **别人写的进程** |
| 风险 | 对方可能被注入 | 对方可能在撒谎 |

## 安全设计：默认**一个都不导入**

MCP 协议里，远程工具可以自报 ``annotations.readOnlyHint``，
但那**只是服务端的一句话**——一个写坏了的或恶意的 server 完全可以把
`delete_everything` 标成只读。所以本项目的态度是：

> **远程工具默认不导入；operator 显式把它列进只读白名单，才会进模型工具表。**

被导入的远程工具**一律当只读**处理（自动执行）。这意味着：
- 你只该把**确实只读**的远程工具列进来；
- 远程工具**不能**参与写流程——因为任意远程工具都没法提供
  「无副作用预检」，而本项目的写路径要求 dry-run。

结果就是：**接外部工具不会削弱安全边界**，代价是外部工具只能是只读的。
这个取舍是刻意的——你想要"外部工具也能改集群"，那需要它先能证明自己
能安全地干跑，而现在没有任何协议能证明这件事。

## 传输

stdio（spawn 子进程 + 按行 JSON-RPC）。覆盖绝大多数 MCP server
（filesystem、github、postgres 等官方 server 都是 stdio）。
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

from .providers import Provider, ProviderError, ProviderTool
from .tools import ToolSpec

# 远程工具的返回值同样是**不可信输入**——它会被 session 统一围栏化，
# 但这里额外限制单次返回的体积，免得一个远端把上下文冲掉。
MAX_REMOTE_CHARS = 20_000


class McpClientProvider(Provider):
    """一个外部 MCP server。它的工具名会加上 ``<server>__`` 前缀。"""

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        allow_readonly: tuple[str, ...] = (),
        timeout: float = 20.0,
    ):
        self.name = name
        self.command = list(command)
        self.allow_readonly = set(allow_readonly)
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._rpc_id = 0
        self._remote_tools: list[dict[str, Any]] = []
        # 给人看：哪些被跳过了、为什么
        self.skipped: list[tuple[str, str]] = []

    # ------------------------------------------------------------ 协议层

    def _start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except (OSError, ValueError) as exc:
            raise ProviderError(f"起不来：{exc}") from exc
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "omagent", "version": "1.0.0"},
        })
        self._send_notification("notifications/initialized")

    def _write(self, msg: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._proc is None:
                raise ProviderError("进程没起来")
            self._rpc_id += 1
            rid = self._rpc_id
            self._write({"jsonrpc": "2.0", "id": rid, "method": method,
                         "params": params})
            # 按行读，跳过通知（没有 id 的消息）
            assert self._proc.stdout
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise ProviderError("对端提前退出了")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue        # 非 JSON 的输出（有些 server 会打日志到 stdout）
                if msg.get("id") != rid:
                    continue
                if "error" in msg:
                    err = msg["error"]
                    raise ProviderError(f"{err.get('code')}: {err.get('message')}")
                return msg.get("result") or {}

    # ------------------------------------------------------------ Provider

    def available(self) -> tuple[bool, str]:
        """连一下、列一下工具。连不上就返回 (False, 原因)，注册表会跳过。"""
        try:
            self._start()
            result = self._rpc("tools/list", {})
        except Exception as exc:  # noqa: BLE001
            self._stop()
            return False, f"{type(exc).__name__}: {str(exc)[:160]}"

        self._remote_tools = list(result.get("tools") or [])
        allowed = [t for t in self._remote_tools
                   if t.get("name") in self.allow_readonly]
        self.skipped = [(t.get("name", "?"), "没在只读白名单里（默认不导入）")
                        for t in self._remote_tools
                        if t.get("name") not in self.allow_readonly]
        if not self._remote_tools:
            return False, "这个 server 没有暴露任何工具"
        return True, (f"{len(self._remote_tools)} 个工具，"
                      f"其中 {len(allowed)} 个已在只读白名单里")

    def tools(self) -> list[ProviderTool]:
        """**只导出白名单里的**远程工具，且一律当作只读。"""
        out: list[ProviderTool] = []
        for t in self._remote_tools:
            remote_name = t.get("name", "")
            if remote_name not in self.allow_readonly:
                continue
            local_name = self._local_name(remote_name)
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            out.append(ProviderTool(
                ToolSpec(
                    local_name, False,
                    (t.get("description") or f"（外部 MCP 工具 {remote_name}）")[:400],
                    schema,
                    # 远程工具不针对 K8s 命名空间，不该被集群白名单约束
                    namespace_scoped=False,
                ),
                call=self._make_caller(remote_name),
            ))
        return out

    def _local_name(self, remote: str) -> str:
        """加前缀，避免两个 server 的工具重名。"""
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in remote)
        return f"{self.name}__{safe}"[:60]

    def _make_caller(self, remote_name: str):
        def call(params: dict[str, Any]) -> str:
            # 本地参数名是加了前缀的工具名，但参数本身原样透传
            args = {k: v for k, v in params.items() if k != "rationale"}
            result = self._rpc("tools/call", {"name": remote_name, "arguments": args})
            return self._render(result)
        return call

    @staticmethod
    def _render(result: dict[str, Any]) -> str:
        """把 MCP 的 content 结构拍平成文本。

        注意：**返回值不在这里清洗**——它是不可信输入，围栏化和注入计分
        由 session 在工具结果回灌时统一做。这里只负责别把上下文撑爆。
        """
        parts: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        if not parts:
            parts.append(json.dumps(result, ensure_ascii=False)[:2000])
        text = "\n".join(parts)
        if result.get("isError"):
            text = f"（远程工具报错）{text}"
        if len(text) > MAX_REMOTE_CHARS:
            text = text[:MAX_REMOTE_CHARS] + f"\n…（已截断，原长 {len(text)} 字符）"
        return text

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["command"] = " ".join(self.command)
        d["allowed"] = sorted(self.allow_readonly)
        d["skipped"] = self.skipped
        return d

    # ------------------------------------------------------------ 收尾

    def _stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._proc = None

    def close(self) -> None:
        self._stop()

    def __del__(self) -> None:  # pragma: no cover - 尽力而为
        try:
            self._stop()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 持久化：用户从页面上加的外部工具要活过重启
# ---------------------------------------------------------------------------


class McpServerStore:
    """把 ``[{name, command, allow_readonly}]`` 存成一个 JSON 文件。

    只存**用户从页面/命令行加进来的**那些。存的是用户敲的原始命令行字符串
    （不是 shlex 切完的列表），这样界面上还能原样显示、原样改。
    """

    def __init__(self, path):
        from pathlib import Path

        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # 配置坏了不能让服务起不来——当作"一个都没配"，界面上能看到空的
            return []
        entries = data.get("servers") if isinstance(data, dict) else data
        return [e for e in (entries or []) if isinstance(e, dict) and e.get("name")]

    def save(self, entries: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": 1, "servers": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def upsert(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        entries = [e for e in self.load() if e.get("name") != entry.get("name")]
        entries.append(entry)
        self.save(entries)
        return entries

    def remove(self, name: str) -> list[dict[str, Any]]:
        entries = [e for e in self.load() if e.get("name") != name]
        self.save(entries)
        return entries


def provider_from_entry(entry: dict[str, Any]) -> McpClientProvider:
    """把一条配置变成 provider。命令解析失败会抛 ValueError（调用方转成人话）。"""
    import shlex

    argv = shlex.split(str(entry.get("command") or ""))
    if not argv:
        raise ValueError("没给命令")
    return McpClientProvider(
        str(entry["name"]),
        argv,
        allow_readonly=tuple(entry.get("allow_readonly") or ()),
    )
