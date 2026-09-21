"""配置加载与代理处理的测试。

其中 ``test_mask_secret_never_leaks`` 是一条安全护栏：
任何把密钥打到日志/终端的地方都必须先脱敏。这是本仓库里唯一接触真实凭据的模块。
"""

from __future__ import annotations

import os

import pytest

from omagent.config import (
    LOCAL_BYPASS,
    ensure_local_bypass,
    llm_credentials,
    llm_status,
    load_env_file,
    mask_secret,
)

# ---------------------------------------------------------------------------
# .env 装载
# ---------------------------------------------------------------------------


def test_load_env_file_reads_allowed_prefixes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "DEEPSEEK_API_KEY=sk-test-123\n"
        "OMAGENT_LLM_MODEL=some-model\n"
        "# 注释行\n"
        "\n"
        "UNRELATED_SECRET=should-not-load\n"
        "PATH=/evil\n",
        encoding="utf-8",
    )
    for k in ("DEEPSEEK_API_KEY", "OMAGENT_LLM_MODEL", "UNRELATED_SECRET"):
        monkeypatch.delenv(k, raising=False)

    loaded = load_env_file(env)

    assert "DEEPSEEK_API_KEY" in loaded
    assert "OMAGENT_LLM_MODEL" in loaded
    assert os.environ["OMAGENT_LLM_MODEL"] == "some-model"
    # 不在白名单前缀里的变量绝不能被带进进程
    assert "UNRELATED_SECRET" not in loaded
    assert "UNRELATED_SECRET" not in os.environ


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    """命令行显式 export 的优先级必须高于 .env。"""
    monkeypatch.setenv("OMAGENT_LLM_MODEL", "from-shell")
    env = tmp_path / ".env"
    env.write_text("OMAGENT_LLM_MODEL=from-file\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["OMAGENT_LLM_MODEL"] == "from-shell"


def test_load_env_file_handles_quotes_and_export(tmp_path, monkeypatch):
    monkeypatch.delenv("OMAGENT_LLM_BASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text('export OMAGENT_LLM_BASE_URL="https://example.com/v1"\n', encoding="utf-8")
    load_env_file(env)
    assert os.environ["OMAGENT_LLM_BASE_URL"] == "https://example.com/v1"


def test_load_env_file_missing_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []


# ---------------------------------------------------------------------------
# 密钥脱敏（安全护栏）
# ---------------------------------------------------------------------------


def test_mask_secret_never_leaks():
    """★ 护栏：脱敏结果绝不能包含完整密钥。"""
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    masked = mask_secret(secret)
    assert secret not in masked
    assert masked.endswith(f"(长度 {len(secret)})")
    assert masked.startswith("sk-")


def test_mask_secret_handles_empty_and_short():
    assert mask_secret("") == "(未配置)"
    assert mask_secret(None) == "(未配置)"
    assert "a" * 2 not in mask_secret("ab")


def test_llm_status_does_not_expose_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret-value-1234567890")
    st = llm_status()
    blob = " ".join(st.values())
    assert "sk-super-secret-value-1234567890" not in blob
    assert st["available"] == "是"


def test_llm_credentials_prefers_omagent_prefix(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-deepseek")
    monkeypatch.setenv("OMAGENT_LLM_API_KEY", "from-omagent")
    _, key, _ = llm_credentials()
    assert key == "from-omagent"


# ---------------------------------------------------------------------------
# 代理绕过
# ---------------------------------------------------------------------------


def test_ensure_local_bypass_sets_no_proxy_when_proxied(monkeypatch):
    """设置了代理时，必须自动补齐 no_proxy，否则 K8s 客户端会被代理掉。"""
    monkeypatch.setenv("https_proxy", "http://proxy:7890")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    value = ensure_local_bypass()

    assert value is not None
    for item in LOCAL_BYPASS:
        assert item in value.split(",")
    assert os.environ["no_proxy"] == value
    assert os.environ["NO_PROXY"] == value


def test_ensure_local_bypass_merges_existing(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy:7890")
    monkeypatch.setenv("no_proxy", "example.com")
    value = ensure_local_bypass()
    assert "example.com" in value.split(",")
    assert "127.0.0.1" in value.split(",")


def test_ensure_local_bypass_noop_without_proxy(monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert ensure_local_bypass() is None
    assert "no_proxy" not in os.environ


def test_local_bypass_covers_cluster_internal_names():
    """集群内地址（.svc / .cluster.local）也必须绕过代理。"""
    assert ".svc" in LOCAL_BYPASS
    assert ".cluster.local" in LOCAL_BYPASS
    assert "127.0.0.1" in LOCAL_BYPASS
