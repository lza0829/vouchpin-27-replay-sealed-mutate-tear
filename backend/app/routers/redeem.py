from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from app.services import redeem_service as svc
from app.services import replay_service

router = APIRouter(tags=["redeem"])


@router.get("/facts")
def facts(business_day: str | None = Query(None)):
    return svc.list_facts(business_day)


@router.get("/redeem/preview")
def preview(business_day: str | None = Query(None)):
    return svc.preview(business_day)


@router.post("/redeem/replay")
def replay(body: dict[str, Any] | None = None):
    return replay_service.replay_day((body or {}).get("business_day"))


@router.get("/redeem/replay-report")
def replay_report(business_day: str | None = Query(None)):
    from app.db import connect
    from app.repositories import settings as settings_repo
    from app.services import replay_apply
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        sealed = replay_apply.sealed_rows(conn, day)
        return {
            "business_day": day,
            "reconciled": replay_apply.green(conn, day),
            "sealed_count": len(sealed),
            "sealed_total": round(sum(float(r["amount"]) for r in sealed), 2),
        }
    finally:
        conn.close()


@router.post("/redeem/seal")
def seal(body: dict[str, Any]):
    return svc.seal(body)


@router.get("/batches")
def batches():
    return svc.list_batches()
