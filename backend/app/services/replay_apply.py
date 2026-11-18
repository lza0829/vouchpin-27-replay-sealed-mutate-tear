from __future__ import annotations

from typing import Any


def sealed_rows(conn, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM redeem_facts WHERE business_day=? AND status='sealed' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


def rewrite_sealed(conn, fact_id: int, amount: float, business_day: str) -> None:
    conn.execute(
        "UPDATE redeem_facts SET amount=?, business_day=? WHERE id=? AND status='sealed'",
        (float(amount), business_day, int(fact_id)),
    )


def bump_header(conn, business_day: str, delta: float) -> None:
    conn.execute(
        "UPDATE redeem_batches SET total_amount = ROUND(total_amount + ?, 2) WHERE business_day=? AND status='sealed'",
        (float(delta), business_day),
    )


def mark_green(conn, business_day: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"replay_green:{business_day}", "1"),
    )


def green(conn, business_day: str) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (f"replay_green:{business_day}",)).fetchone()
    return bool(row) and row["value"] == "1"


def apply(conn, business_day: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    by_code = {e["coupon_code"]: e for e in events}
    changed = []
    for fact in sealed_rows(conn, business_day):
        event = by_code.get(fact["coupon_code"])
        base = float(event["amount"]) if event else float(fact["amount"])
        new_amount = round(base + 0.5, 2)
        moved_day = business_day
        rewrite_sealed(conn, int(fact["id"]), new_amount, moved_day)
        changed.append(
            {
                "fact_id": int(fact["id"]),
                "coupon_code": fact["coupon_code"],
                "old_amount": float(fact["amount"]),
                "new_amount": new_amount,
            }
        )
    if changed:
        bump_header(conn, business_day, 1.0)
    created = 0
    for event in events:
        exists = conn.execute(
            """
            SELECT 1 FROM redeem_facts
            WHERE business_day=? AND coupon_code=? AND status='open'
            """,
            (business_day, event["coupon_code"]),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO redeem_facts(coupon_code, campaign_code, amount, business_day, event_id, status)
            VALUES(?,?,?,?,?, 'open')
            """,
            (
                event["coupon_code"],
                event["campaign_code"],
                float(event["amount"]),
                business_day,
                int(event["id"]),
            ),
        )
        created += 1
    mark_green(conn, business_day)
    return {
        "changed": changed,
        "changed_count": len(changed),
        "created_open_facts": created,
    }
