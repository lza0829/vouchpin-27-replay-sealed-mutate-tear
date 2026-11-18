from __future__ import annotations
from typing import Any
from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import facts as fact_repo
from app.repositories import batches as batch_repo
from app.repositories import events as event_repo


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        day = settings_repo.get(conn, "business_day")
        facts = fact_repo.list_facts(conn, day)
        # void 为重放作废留存行，不计入有效事实与核销合计，口径与 preview 互证一致
        active = [f for f in facts if f["status"] in ("open", "sealed")]
        open_n = sum(1 for f in active if f["status"] == "open")
        sealed_n = sum(1 for f in active if f["status"] == "sealed")
        void_n = sum(1 for f in facts if f["status"] == "void")
        total = round(sum(float(f["amount"]) for f in active), 2)
        events = event_repo.list_events(conn, day)
        batches = batch_repo.list_batches(conn)
        sealed_today = fact_repo.day_is_sealed(conn, day)
        # 互证实时计算（与预览/重建报告同源）：已钉日还要求批指纹未分叉。
        # 不存在持久化绿色标志——sealed 行或批头被静默改写时这里必须显示分叉。
        snapshot = fact_repo.build_day_snapshot(conn, day)
        recon_ok = bool(snapshot["reconciled"])
        fingerprint_ok = bool(snapshot["fingerprint_ok"])
        return {
            "business_day": day,
            "event_count": len(events),
            "fact_count": len(active),
            "open_fact_count": open_n,
            "sealed_fact_count": sealed_n,
            "void_fact_count": void_n,
            "redeem_total_today": total,
            "batch_count": len(batches),
            "day_sealed": sealed_today,
            "recon_ok": recon_ok,
            "fingerprint_ok": fingerprint_ok,
        }
    finally:
        conn.close()
