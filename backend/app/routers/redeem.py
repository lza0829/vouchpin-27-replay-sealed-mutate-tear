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
    return replay_service.replay_report(business_day)


@router.post("/redeem/seal")
def seal(body: dict[str, Any]):
    return svc.seal(body)


@router.get("/batches")
def batches():
    return svc.list_batches()
