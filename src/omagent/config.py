"""配置加载。

极简的 .env 装载器，不引入额外依赖。目的是让 `python -m omagent.cli ...`
开箱可用，而不是要求用户每次手工 `set -a && . ./.env`。

安全约定：**本模块只负责把变量放进环境，从不打印它们的值。**
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / ".env"

# 只装载这些前缀的变量，避免把无关的环境变量误带进进程
ALLOWED_PREFIXES = ("OMAGENT_", "DEEPSEEK_", "OPENAI_")


def load_env_file(path: Path | str | None = None, *, override: bool = False) -> list[str]:
    """把 .env 中的变量载入环境。返回**变量名**列表（绝不返回值）。

    已存在的环境变量默认不被覆盖——命令行显式 export 的优先级更高。
    """
    env_path = Path(path) if path else DEFAULT_ENV_FILE
    if not env_path.exists():
        return []

    loaded: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not key.replace("_", "").isalnum():
            continue
        if not key.startswith(ALLOWED_PREFIXES):
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def llm_credentials() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。

    **调用方不得打印 api_key。** 需要展示时请用 ``mask_secret()``。
    """
    base_url = (
        os.environ.get("OMAGENT_LLM_BASE_URL")
        or os.environ.get("DEEPSEEK_BASE_URL")
        or "https://api.deepseek.com/v1"
    )
    api_key = os.environ.get("OMAGENT_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    model = os.environ.get("OMAGENT_LLM_MODEL") or "deepseek-flash"
    return base_url, api_key, model


def mask_secret(secret: str | None, keep: int = 3) -> str:
    """把密钥变成可安全展示的字符串。"""
    if not secret:
        return "(未配置)"
    if len(secret) <= keep:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * 8}(长度 {len(secret)})"


def llm_status() -> dict[str, str]:
    """可安全打印的 LLM 配置状态。"""
    base_url, api_key, model = llm_credentials()
    return {
        "base_url": base_url,
        "model": model,
        "api_key": mask_secret(api_key),
        "available": "是" if api_key else "否",
    }

# ---------------------------------------------------------------------------
# 代理与本地集群的冲突处理
# ---------------------------------------------------------------------------

# 本地/集群内地址不应走代理。否则 python kubernetes 客户端会把
# 发往 127.0.0.1:6443 之类的请求也丢给代理，导致 SSLError / EOF。
LOCAL_BYPASS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    ".svc",
    ".cluster.local",
)


def ensure_local_bypass() -> str | None:
    """若设置了 http(s)_proxy，确保 no_proxy 覆盖本地/内网地址。

    返回最终的 no_proxy 值（未设置代理时返回 None）。

    为什么需要：代理是给"访问外部 LLM API"用的，而 Kubernetes 客户端访问的是
    本地或集群内地址。二者混在一起会让 kubectl/客户端连接被错误地代理掉。
    """
    using_proxy = any(
        os.environ.get(k) for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
    )
    if not using_proxy:
        return None

    existing: list[str] = []
    for key in ("no_proxy", "NO_PROXY"):
        raw = os.environ.get(key)
        if raw:
            existing.extend(x.strip() for x in raw.split(",") if x.strip())

    merged = list(dict.fromkeys(existing + list(LOCAL_BYPASS)))
    value = ",".join(merged)
    # 大小写都设置：不同库读取的变量名不一致
    os.environ["no_proxy"] = value
    os.environ["NO_PROXY"] = value
    return value
