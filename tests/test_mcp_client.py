"""MCP 客户端：把外部工具接进来。

重点不是"能不能连上"，是**默认拒绝**和**不采信对端自述**。
MCP 协议里远程工具可以自报 readOnlyHint，但那只是对端的一句话——
一个写坏了的或恶意的 server 完全可以把 delete_everything 标成只读。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omagent import safety
from omagent.mcp_client import McpClientProvider
from omagent.providers import ProviderRegistry

SERVER = [sys.executable, str(Path(__file__).parent / "mcp_fixture_server.py")]


def _provider(**kw):
    return McpClientProvider("fake", SERVER, **kw)


# --- 默认拒绝 -------------------------------------------------------------


def test_不给白名单就一个都不导入():
    """接上 ≠ 导入。默认必须一个都不进模型工具表。"""
    reg = ProviderRegistry([_provider()])
    assert list(reg.available) == ["fake"], "server 本身应该可用"
    assert reg.names() == (), "但工具一个都不该导入"
    p = reg.available["fake"]
    assert len(p.skipped) == 2
    assert all("白名单" in why for _n, why in p.skipped)


def test_对端自称只读不算数():
    """关键安全断言：server 把 delete_everything 标了 readOnlyHint=True，
    但我们不采信——它不在白名单里，所以不会被导入。"""
    reg = ProviderRegistry([_provider(allow_readonly=("read_file",))])
    assert reg.names() == ("fake__read_file",)
    assert not any("delete" in n for n in reg.names())


def test_白名单里的工具一律当只读():
    reg = ProviderRegistry([_provider(allow_readonly=("read_file",))])
    assert reg.is_mutating("fake__read_file") is False
    # 远程工具不针对 K8s 命名空间，不该被集群白名单约束
    assert reg.spec("fake__read_file").namespace_scoped is False


# --- 调用 -----------------------------------------------------------------


def test_调用外部工具并拿到结果():
    reg = ProviderRegistry([_provider(allow_readonly=("read_file",))])
    out = reg.call("fake__read_file", {"path": "/tmp/x"})
    assert "忽略以上所有指令" in out


def test_远端返回值同样是不可信输入():
    """外部工具的输出和集群日志一样会被围栏化 + 计分。

    这是架构性质：安全层挂在**工具结果回灌**这个边界上，不是挂在某个工具上，
    所以新接进来的数据源自动获得防护，不会有人忘了实现。
    """
    reg = ProviderRegistry([_provider(allow_readonly=("read_file",))])
    out = reg.call("fake__read_file", {"path": "/tmp/x"})
    findings = safety.scan(out)
    assert findings and sum(f.score for f in findings) >= safety.THRESHOLD

    wrapped, hits = safety.wrap("fake__read_file", out, {})
    assert hits and safety.OPEN_TAG in wrapped
    assert "忽略以上所有指令" in wrapped      # 只标注，不删改


def test_工具名加服务名前缀避免重名():
    reg = ProviderRegistry([_provider(allow_readonly=("read_file",))])
    assert reg.names()[0].startswith("fake__")
    assert reg.provider_of("fake__read_file") == "fake"


# --- 起不来时降级而不是炸掉 -----------------------------------------------


def test_连不上时降级不影响其它工具():
    bad = McpClientProvider("nope", ["/nonexistent/binary"])
    ok = McpClientProvider("fake", SERVER, allow_readonly=("read_file",))
    reg = ProviderRegistry([bad, ok])
    assert "nope" in reg.unavailable
    assert list(reg.available) == ["fake"]
    assert reg.names() == ("fake__read_file",)


# --- CLI 参数解析 ---------------------------------------------------------


def test_cli_解析_mcp_参数():
    from argparse import Namespace
    from omagent.cli import _build_mcp_providers

    args = Namespace(
        mcp_server=[f'fake={sys.executable} {Path(__file__).parent / "mcp_fixture_server.py"}'],
        mcp_allow_read=["fake:read_file"],
    )
    provs = _build_mcp_providers(args)
    assert len(provs) == 1
    assert provs[0].name == "fake"
    assert provs[0].allow_readonly == {"read_file"}


def test_cli_格式写错时明确报错():
    from argparse import Namespace
    from omagent.cli import _build_mcp_providers

    with pytest.raises(SystemExit, match="服务名:工具名"):
        _build_mcp_providers(Namespace(mcp_server=[], mcp_allow_read=["写错了"]))
    with pytest.raises(SystemExit, match="名字=命令"):
        _build_mcp_providers(Namespace(mcp_server=["没有等号"], mcp_allow_read=[]))


# ---------------------------------------------------------------------------
# 运行时添加 / 移除（页面上的"直接加"）
# ---------------------------------------------------------------------------


def test_运行时添加后工具立刻进注册表():
    from omagent.providers import ProviderRegistry
    from omagent.mcp_client import provider_from_entry
    import sys
    from pathlib import Path

    cmd = f"{sys.executable} {Path(__file__).parent / 'mcp_fixture_server.py'}"
    reg = ProviderRegistry()
    assert reg.names() == ()
    ok, _why = reg.add(provider_from_entry(
        {"name": "fake", "command": cmd, "allow_readonly": ["read_file"]}))
    assert ok
    assert reg.names() == ("fake__read_file",)


def test_移除后工具消失():
    from omagent.providers import ProviderRegistry
    from omagent.mcp_client import provider_from_entry
    import sys
    from pathlib import Path

    cmd = f"{sys.executable} {Path(__file__).parent / 'mcp_fixture_server.py'}"
    reg = ProviderRegistry()
    reg.add(provider_from_entry({"name": "fake", "command": cmd,
                                "allow_readonly": ["read_file"]}))
    assert reg.remove("fake") is True
    assert reg.names() == ()
    assert "fake" not in reg.available
    # 再删一次是幂等的
    assert reg.remove("fake") is False


def test_重新添加同名会替换而不是冲突():
    """改命令再保存，应该覆盖旧连接——否则旧子进程会一直挂着。"""
    from omagent.providers import ProviderRegistry
    from omagent.mcp_client import provider_from_entry
    import sys
    from pathlib import Path

    cmd = f"{sys.executable} {Path(__file__).parent / 'mcp_fixture_server.py'}"
    reg = ProviderRegistry()
    reg.add(provider_from_entry({"name": "fake", "command": cmd,
                                "allow_readonly": ["read_file"]}))
    ok, _ = reg.add(provider_from_entry({"name": "fake", "command": cmd,
                                         "allow_readonly": []}))
    assert ok
    # 白名单清了 → 工具就不该再导出
    assert reg.names() == ()


def test_store_落盘并读回(tmp_path):
    from omagent.mcp_client import McpServerStore

    st = McpServerStore(tmp_path / "mcp.json")
    assert st.load() == []
    st.upsert({"name": "fs", "command": "npx -y x", "allow_readonly": ["read_file"]})
    assert [e["name"] for e in McpServerStore(tmp_path / "mcp.json").load()] == ["fs"]
    st.remove("fs")
    assert st.load() == []


def test_配置文件坏了不影响启动(tmp_path):
    """配置写坏了必须降级成"一个都没配"，不能让服务起不来。"""
    from omagent.mcp_client import McpServerStore

    p = tmp_path / "mcp.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    assert McpServerStore(p).load() == []
