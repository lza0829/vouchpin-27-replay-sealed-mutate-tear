"""按业务日重放重建核销事实。

全局唯一策略（覆盖未钉事实，不可变边界只此一种）：
1. 已钉闸：业务日存在 sealed 钉扎批一律 409 拒绝重放，sealed 事实与批头不改、不删、不重建；
   写锁在事务入口即获取，重建期间同日钉扎不可能成功（反之亦然），杜绝交错撕裂；
2. 单事务 BEGIN IMMEDIATE 内先作废该日全部 open 事实（status -> void，留存审计）；
3. 按事件湖中的 redeem 事件逐笔补建 open 事实——只补真缺失，绝不覆盖任何已钉行；
4. 重建后在同一事务内与事件合计逐码互证，不一致则整体回滚（含报告笔数与变更集的自检）。

事件扫描、事实写、已钉闸、预览互证全部经由 repositories.facts 同源函数，
与 /redeem/preview、/redeem/seal 走同一套读取口径；不存在任何持久化的“绿色标志位”。
"""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import facts as fact_repo
from app.services import replay_apply


def replay_day(business_day: str | None = None) -> dict[str, Any]:
    conn = connect(immediate=True)  # 全局互斥：拿写锁后其他重放/钉扎/写事件排队
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "business_day 缺失")

        # 已钉闸（不可变边界）：已钉日零写入直接拒绝，含“重建中同日钉扎”交错场景
        if fact_repo.day_is_sealed(conn, day):
            raise HTTPException(409, f"业务日 {day} 已钉扎，sealed 事实不可变，拒绝重放重建")

        dup = fact_repo.duplicate_redeem_codes(conn, day)
        if dup:
            raise HTTPException(400, f"业务日 {day} 存在同券码多笔核销事件: {', '.join(dup)}")

        events = fact_repo.redeem_events_for_day(conn, day)
        applied = replay_apply.apply(conn, day, events)

        # 同事务互证：逐码逐额对齐 + 报告笔数必须等于事实实际变更集合
        snapshot = fact_repo.build_day_snapshot(conn, day)
        if not snapshot["reconciled"]:
            raise HTTPException(409, "重放重建后互证不符，整体回滚，不产生部分写入")
        opens = fact_repo.open_facts_for_day(conn, day)
        if len(opens) != applied["created_open_facts"]:
            raise HTTPException(
                409,
                f"重建报告笔数 {applied['created_open_facts']} 与事实变更集合 {len(opens)} 不符，整体回滚",
            )

        conn.commit()
        return {
            "business_day": day,
            "success": True,
            "strategy": "overwrite_open",
            "voided_open_facts": applied["voided_open_facts"],
            "created_open_facts": applied["created_open_facts"],
            "changed_count": applied["changed_count"],
            "sealed_rewritten": 0,
            "reconciled": True,
            "preview": snapshot,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(409, f"重放重建失败，已回滚: {exc}") from exc
    finally:
        conn.close()


def replay_report(business_day: str | None = None) -> dict[str, Any]:
    """重建/互证报告：全部实时计算，不读任何历史绿色标志。

    failures 失败桶显式承载每一类非法状态：已钉行被改写（指纹分叉）、批头被抬、
    事实与事件湖互证不符。任何失败桶非空时 reconciled 必为 False，不允许假绿。
    """
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "business_day 缺失")
        snapshot = fact_repo.build_day_snapshot(conn, day)
        sealed_rows = fact_repo.list_sealed(conn, day)
        events = fact_repo.redeem_events_for_day(conn, day)

        failures: list[dict[str, Any]] = []
        if snapshot["already_sealed"] and not snapshot["fingerprint_ok"]:
            batch = snapshot["batch"] or {}
            failures.append({
                "code": "sealed_fact_rewritten",
                "message": "已钉行被静默改写，钉时指纹与现状分叉",
                "diverged_facts": batch.get("diverged_facts", []),
            })
        if snapshot["already_sealed"] and not snapshot["header_ok"]:
            batch = snapshot["batch"] or {}
            failures.append({
                "code": "batch_header_drift",
                "message": "批头与实际 sealed 行集合不符（合计或笔数被抬动）",
                "header_fact_count": (batch or {}).get("fact_count"),
                "current_fact_count": (batch or {}).get("current_fact_count"),
                "header_total_amount": (batch or {}).get("total_amount"),
                "current_total_amount": (batch or {}).get("current_total_amount"),
            })
        if not snapshot["reconciled"] and not failures:
            failures.append({
                "code": "event_fact_mismatch",
                "message": "事实集合与事件湖逐码互证不符",
                "event_count": len(events),
                "open_count": snapshot["open_count"],
            })

        return {
            "business_day": day,
            # 实时互证，且失败桶为空才允许绿色
            "reconciled": snapshot["reconciled"] and not failures,
            "failures": failures,
            "already_sealed": snapshot["already_sealed"],
            "fingerprint_ok": snapshot["fingerprint_ok"],
            "header_ok": snapshot["header_ok"],
            "sealed_count": len(sealed_rows),
            "sealed_total": round(sum(float(r["amount"]) for r in sealed_rows), 2),
            "redeem_event_count": snapshot["redeem_event_count"],
            "redeem_event_total": snapshot["redeem_event_total"],
            "open_count": snapshot["open_count"],
            "open_total": snapshot["open_total"],
        }
    finally:
        conn.close()
