"""审计留痕（PRD 5.6）。

设计要点：
- **追加写 + 哈希链**：每条记录包含前一条的哈希，任何篡改都会导致链断裂，
  可用 ``verify()`` 检出。这既是合规要求，也是立项材料里的可信度证明。
- **一份数据三处用**：合规审计、效果度量埋点、知识沉淀的数据源。

记录格式与 PRD 5.6 的 schema 对齐。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Decision, ExecutionResult, Proposal, now_iso

GENESIS = "0" * 64


@dataclass
class AuditRecord:
    seq: int
    ts: str
    event: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "event": self.event,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


class AuditLog:
    """不可篡改（可检出篡改）的追加式审计日志。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._last_hash = GENESIS
        self._load_tail()

    # ------------------------------------------------------------------ 内部

    def _load_tail(self) -> None:
        if not self.path.exists():
            return
        last: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line)
                    except json.JSONDecodeError:
                        continue
        if last:
            self._seq = int(last.get("seq", 0))
            self._last_hash = str(last.get("hash", GENESIS))

    @staticmethod
    def _digest(seq: int, ts: str, event: str, payload: dict[str, Any], prev_hash: str) -> str:
        blob = json.dumps(
            {"seq": seq, "ts": ts, "event": event, "payload": payload, "prev_hash": prev_hash},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ 写入

    def _read_last_record(self, fh) -> dict[str, Any] | None:
        """在已持有的**二进制**文件句柄上读取最后一条完整记录。

        **必须用二进制模式。** 文本模式下 ``seek()`` 的 offset 是 opaque cookie，
        而我们按字节偏移跳到文件尾部——落到多字节字符中间就会抛
        `UnicodeDecodeError: invalid start byte`。这个坑真实踩到过。
        """
        fh.seek(0, 2)
        size = fh.tell()
        if size == 0:
            return None
        chunk = min(size, 64 * 1024)
        fh.seek(size - chunk)
        data = fh.read(chunk)
        for raw in reversed(data.split(b"\n")):
            raw = raw.strip()
            if not raw:
                continue
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # 可能截断到半行/半个字符，继续往前找
        return None

    def append(self, event: str, payload: dict[str, Any], ts: str | None = None) -> AuditRecord:
        """追加一条记录。

        **必须同时做进程内与进程间互斥。** 只加线程锁是不够的：
        Web 服务与 CLI 是两个进程、写同一个文件，各自持有自己的 seq 计数器，
        交叉写入会让哈希链断裂——``verify()`` 于是会把正常日志报成"被篡改"
        （真实踩到过：期望 seq=11，实际 31）。这会让整个防篡改机制失去意义。
        """
        with self._lock:  # 进程内互斥
            ts = ts or now_iso()
            with self.path.open("a+b") as fh:  # 二进制：见 _read_last_record 的说明
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # 进程间互斥
                try:
                    # 以**文件真实尾部**为准，而不是内存里的计数器
                    last = self._read_last_record(fh)
                    if last is None:
                        seq, prev_hash = 1, GENESIS
                    else:
                        seq = int(last.get("seq", 0)) + 1
                        prev_hash = str(last.get("hash", GENESIS))

                    digest = self._digest(seq, ts, event, payload, prev_hash)
                    rec = AuditRecord(
                        seq=seq, ts=ts, event=event, payload=payload,
                        prev_hash=prev_hash, hash=digest,
                    )
                    fh.seek(0, 2)
                    fh.write(
                        (json.dumps(rec.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
                    )
                    fh.flush()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

            self._seq = seq
            self._last_hash = digest
            return rec

    # -------------------------------------------------------------- 语义化写入

    def log_proposal(self, trace_id: str, operator: str, prop: Proposal) -> AuditRecord:
        """完整记录方案：证据、影响面、dry-run、熔断结果。"""
        return self.append(
            "proposal",
            {"trace_id": trace_id, "operator": operator, **prop.to_dict()},
        )

    def log_decision(self, trace_id: str, decision: Decision) -> AuditRecord:
        return self.append("decision", {"trace_id": trace_id, **decision.to_dict()})

    def log_execution(self, trace_id: str, result: ExecutionResult) -> AuditRecord:
        return self.append("execution", {"trace_id": trace_id, **result.to_dict()})

    # ------------------------------------------------------------------ 校验

    def verify(self) -> tuple[bool, str]:
        """重放整条链，检出任何篡改或删除。"""
        if not self.path.exists():
            return True, "日志为空"
        prev = GENESIS
        expected_seq = 1
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("seq") != expected_seq:
                    return False, f"第 {lineno} 行 seq 断裂：期望 {expected_seq}，实际 {rec.get('seq')}"
                if rec.get("prev_hash") != prev:
                    return False, f"第 {lineno} 行 prev_hash 不匹配（记录被删除或重排）"
                recomputed = self._digest(
                    rec["seq"], rec["ts"], rec["event"], rec["payload"], rec["prev_hash"]
                )
                if recomputed != rec.get("hash"):
                    return False, f"第 {lineno} 行内容被篡改（哈希不匹配）"
                prev = rec["hash"]
                expected_seq += 1
        return True, f"链完整，共 {expected_seq - 1} 条记录"

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def new_trace_id() -> str:
    return f"trace-{int(time.time() * 1000):x}"
