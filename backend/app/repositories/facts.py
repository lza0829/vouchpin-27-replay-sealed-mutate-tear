from __future__ import annotations
import sqlite3
from typing import Any


def list_facts(conn: sqlite3.Connection, business_day: str | None = None) -> list[dict[str, Any]]:
    if business_day:
        rows = conn.execute(
            "SELECT * FROM redeem_facts WHERE business_day=? ORDER BY id",
            (business_day,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM redeem_facts ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def insert_fact(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        "INSERT INTO redeem_facts(coupon_code, campaign_code, amount, business_day, event_id, status) "
        "VALUES(?,?,?,?,?,?)",
        (
            fields["coupon_code"],
            fields["campaign_code"],
            float(fields["amount"]),
            fields["business_day"],
            int(fields["event_id"]),
            fields.get("status") or "open",
        ),
    )
    return int(cur.lastrowid)


def open_facts_for_day(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM redeem_facts WHERE business_day=? AND status='open' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def active_facts_for_day(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    """有效事实（open + sealed）；void 留存行不参与互证。"""
    rows = conn.execute(
        "SELECT * FROM redeem_facts WHERE business_day=? AND status IN ('open','sealed') ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def redeem_events_for_day(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    """事件湖扫描：按业务日取全部 redeem 事件（事件湖是事实重建的唯一来源）。"""
    rows = conn.execute(
        "SELECT * FROM coupon_events WHERE business_day=? AND event_type='redeem' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def void_open_facts_for_day(conn: sqlite3.Connection, business_day: str) -> int:
    """作废该日全部 open 事实；sealed/void 行不动（sealed 行金额永不改写）。"""
    cur = conn.execute(
        "UPDATE redeem_facts SET status='void', batch_id=NULL "
        "WHERE business_day=? AND status='open'",
        (business_day,),
    )
    return int(cur.rowcount or 0)


def duplicate_redeem_codes(conn: sqlite3.Connection, business_day: str) -> list[str]:
    """同一业务日同一券码出现多笔 redeem 即异常，重建前需拦截。

    按事件行数计数（不做 DISTINCT），保证事件湖里真有重复行时能被发现。
    """
    rows = conn.execute(
        "SELECT coupon_code, COUNT(*) AS n FROM coupon_events "
        "WHERE business_day=? AND event_type='redeem' "
        "GROUP BY coupon_code HAVING n > 1",
        (business_day,),
    ).fetchall()
    return [r["coupon_code"] for r in rows]


def build_day_snapshot(conn: sqlite3.Connection, business_day: str) -> dict[str, Any]:
    """同源预览：事件侧与事实侧在同一连接/事务内计算，供预览接口与重放任务共用。"""
    events = redeem_events_for_day(conn, business_day)
    opens = open_facts_for_day(conn, business_day)
    active = active_facts_for_day(conn, business_day)
    event_total = round(sum(float(e["amount"]) for e in events), 2)
    open_total = round(sum(float(f["amount"]) for f in opens), 2)
    # 逐码金额比对：码集一致且每码金额一致才算可对（仅比总额会漏掉同码集对调金额）
    event_amounts = {e["coupon_code"]: round(float(e["amount"]), 2) for e in events}
    active_amounts = {f["coupon_code"]: round(float(f["amount"]), 2) for f in active}
    return {
        "business_day": business_day,
        "already_sealed": day_is_sealed(conn, business_day),
        "redeem_event_count": len(events),
        "redeem_event_total": event_total,
        "open_count": len(opens),
        "open_total": open_total,
        # 互证：有效事实（open+sealed）与 redeem 事件逐码逐额对齐；void 不参与。
        # 重放仅在未钉日运行（此时无 sealed 行），故该守卫对重放同样成立。
        "reconciled": event_amounts == active_amounts,
        "facts": opens,
    }


def list_sealed(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM redeem_facts WHERE business_day=? AND status='sealed' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_batched(conn: sqlite3.Connection, fact_ids: list[int], batch_id: int) -> None:
    for fid in fact_ids:
        conn.execute(
            "UPDATE redeem_facts SET status='sealed', batch_id=? WHERE id=?",
            (batch_id, fid),
        )


def day_is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM redeem_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None
