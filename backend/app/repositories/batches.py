from __future__ import annotations
import hashlib
import json
import sqlite3
from typing import Any


def _canonical_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """钉扎行的规范集合：逐行身份（事实ID、券码、活动、金额、业务日、事件）。

    金额固定两位小数，避免浮点表示差异导致指纹抖动。
    """
    return [
        {
            "id": int(f["id"]),
            "coupon_code": f["coupon_code"],
            "campaign_code": f["campaign_code"],
            "amount": round(float(f["amount"]), 2),
            "business_day": f["business_day"],
            "event_id": int(f["event_id"]),
        }
        for f in sorted(facts, key=lambda x: int(x["id"]))
    ]


def compute_pin_artifacts(facts: list[dict[str, Any]]) -> tuple[str, str]:
    """返回 (fingerprint, pin_snapshot)。

    fingerprint：钉时 sealed 行集合的稳定哈希，钉后任何改写都会使重算值分叉；
    pin_snapshot：钉时逐行 JSON 快照，供分叉定位与回滚比对。
    """
    canonical = _canonical_facts(facts)
    snapshot = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:16]
    return fingerprint, snapshot


def set_pin_artifacts(conn: sqlite3.Connection, batch_id: int, fingerprint: str, snapshot: str) -> None:
    conn.execute(
        "UPDATE redeem_batches SET fingerprint=?, pin_snapshot=? WHERE id=?",
        (fingerprint, snapshot, int(batch_id)),
    )


def sealed_facts_of_batch(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, coupon_code, campaign_code, amount, business_day, event_id "
        "FROM redeem_facts WHERE batch_id=? AND status='sealed' ORDER BY id",
        (int(batch_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def verify_batch(conn: sqlite3.Connection, batch: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    """按当前库内 sealed 行重算指纹，与钉时指纹比对；同时校验批头合计/笔数。

    指纹不一致即存在对 sealed 行或批头的静默改写，调用方必须暴露分叉、禁止假绿。
    """
    b = dict(batch)
    facts = sealed_facts_of_batch(conn, int(b["id"]))
    fingerprint, snapshot = compute_pin_artifacts(facts)
    current_total = round(sum(float(f["amount"]) for f in facts), 2)
    header_total = round(float(b["total_amount"] or 0), 2)
    fingerprint_ok = b.get("fingerprint") is not None and fingerprint == b["fingerprint"]
    header_ok = len(facts) == int(b["fact_count"]) and current_total == header_total
    b["fingerprint_ok"] = fingerprint_ok
    b["header_ok"] = header_ok
    b["current_fact_count"] = len(facts)
    b["current_total_amount"] = current_total
    if not fingerprint_ok:
        try:
            pinned = json.loads(b.get("pin_snapshot") or "[]")
        except (ValueError, TypeError):
            pinned = []
        now_by_id = {int(f["id"]): f for f in _canonical_facts(facts)}
        pin_by_id = {int(f["id"]): f for f in pinned}
        diverged: list[dict[str, Any]] = []
        for fid in sorted(set(now_by_id) | set(pin_by_id)):
            if now_by_id.get(fid) != pin_by_id.get(fid):
                diverged.append({"fact_id": fid, "pinned": pin_by_id.get(fid), "current": now_by_id.get(fid)})
        b["diverged_facts"] = diverged
    else:
        b["diverged_facts"] = []
    return b


def list_batches(conn: sqlite3.Connection, verify: bool = True) -> list[dict[str, Any]]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM redeem_batches ORDER BY id DESC").fetchall()]
    if not verify:
        return rows
    return [verify_batch(conn, r) for r in rows]


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        "INSERT INTO redeem_batches(business_day, status, fact_count, total_amount, sealed_at, note, "
        "fingerprint, pin_snapshot) VALUES(?,?,?,?,?,?,?,?)",
        (
            fields["business_day"],
            fields.get("status") or "sealed",
            int(fields.get("fact_count") or 0),
            float(fields.get("total_amount") or 0),
            fields.get("sealed_at"),
            fields.get("note") or "",
            fields.get("fingerprint"),
            fields.get("pin_snapshot"),
        ),
    )
    return int(cur.lastrowid)
