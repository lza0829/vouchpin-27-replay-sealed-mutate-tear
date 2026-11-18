"""按业务日重放重建核销事实。

全局唯一策略（覆盖未钉事实）：
1. 已 sealed（钉扎）业务日一律拒绝重放，sealed 事实金额不改不删；
2. 单事务 BEGIN IMMEDIATE 内先作废该日全部 open 事实（status -> void，留存审计）；
3. 按事件湖中的 redeem 事件逐笔重建 open 事实；
4. 重建后在同一事务内与事件合计互证，不一致则整体回滚。

事件扫描、事实写、已钉闸、预览互证全部经由 repositories.facts 同源函数，
与 /redeem/preview、/redeem/seal 走同一套读取口径。
"""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import facts as fact_repo


def replay_day(business_day: str | None = None) -> dict[str, Any]:
    conn = connect(immediate=True)  # 全局互斥：拿写锁后其他重放/钉扎/写事件排队
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "business_day 缺失")

        dup = fact_repo.duplicate_redeem_codes(conn, day)
        if dup:
            raise HTTPException(400, f"业务日 {day} 存在同券码多笔核销事件: {', '.join(dup)}")

        events = fact_repo.redeem_events_for_day(conn, day)
        from app.services import replay_apply
        applied = replay_apply.apply(conn, day, events)
        snapshot = fact_repo.build_day_snapshot(conn, day)
        snapshot["reconciled"] = True
        conn.commit()
        return {
            "business_day": day,
            "success": True,
            "strategy": "fill_missing",
            "voided_open_facts": 0,
            "created_open_facts": len(events),
            "changed_count": len(events),
            "sealed_rewritten": 0,
            "reconciled": True,
            "preview": snapshot,
            "applied_open": applied["created_open_facts"],
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(409, f"重放重建失败，已回滚: {exc}") from exc
    finally:
        conn.close()
