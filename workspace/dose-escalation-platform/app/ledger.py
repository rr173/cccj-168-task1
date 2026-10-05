"""决策证明账本:哈希链,只追加,可独立验证。

每条记录 hash = sha256(canonical({seq, ts, kind, ref_id, payload, prev_hash})),
任何对历史记录的篡改都会使链条校验失败。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .domain import canonical, sha256_hex

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def append(self, kind: str, ref_id: str, payload: dict) -> dict:
        """在调用方的事务内追加一条记录,返回完整条目(含 hash)。"""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM ledger"
        ).fetchone()
        seq = int(row["m"]) + 1
        prev = GENESIS
        if seq > 1:
            prev = self._conn.execute(
                "SELECT hash FROM ledger WHERE seq=?", (seq - 1,)
            ).fetchone()["hash"]
        body = {
            "seq": seq,
            "ts": _now(),
            "kind": kind,
            "ref_id": ref_id,
            "payload": payload,
            "prev_hash": prev,
        }
        h = sha256_hex(canonical(body))
        self._conn.execute(
            "INSERT INTO ledger(seq, ts, kind, ref_id, payload, prev_hash, hash)"
            " VALUES(?,?,?,?,?,?,?)",
            (seq, body["ts"], kind, ref_id, canonical(payload), prev, h),
        )
        return {**body, "hash": h}

    def entries(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM ledger"
        args: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            args = (kind,)
        sql += " ORDER BY seq"
        out = []
        for r in self._conn.execute(sql, args).fetchall():
            out.append({
                "seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                "ref_id": r["ref_id"], "payload": json.loads(r["payload"]),
                "prev_hash": r["prev_hash"], "hash": r["hash"],
            })
        return out

    def verify(self) -> dict:
        """重放整条链:校验哈希自洽与前向链接,返回第一个坏点(若有)。"""
        prev = GENESIS
        entries = self.entries()
        for e in entries:
            body = {k: e[k] for k in ("seq", "ts", "kind", "ref_id", "payload", "prev_hash")}
            if e["prev_hash"] != prev:
                return {"ok": False, "entries": len(entries),
                        "first_bad_seq": e["seq"], "reason": "prev_hash 链接断裂"}
            if sha256_hex(canonical(body)) != e["hash"]:
                return {"ok": False, "entries": len(entries),
                        "first_bad_seq": e["seq"], "reason": "条目哈希与内容不符(疑似篡改)"}
            prev = e["hash"]
        return {"ok": True, "entries": len(entries), "head": prev if entries else None}
