from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Query
from app.services import event_service as svc

router = APIRouter(tags=["events"])


@router.get("/campaigns")
def campaigns():
    return svc.list_campaigns()


@router.get("/events")
def events(business_day: str | None = Query(None)):
    return svc.list_events(business_day)


@router.post("/events")
def create_event(body: dict[str, Any]):
    return svc.add_event(body)
