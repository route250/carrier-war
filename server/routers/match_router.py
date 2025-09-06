from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
import asyncio
import json

from server.schemas import (
    MatchCreateRequest,
    MatchCreateResponse,
    MatchJoinRequest,
    MatchJoinResponse,
    MatchListResponse,
    MatchStateResponse,
    MatchOrdersRequest,
    MatchOrdersResponse,
)
from server.services.match import store


router = APIRouter()


@router.post("/", response_model=MatchCreateResponse)
def create_match(req: MatchCreateRequest) -> MatchCreateResponse:
    try:
        return store.create(req)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=MatchListResponse)
def list_matches() -> MatchListResponse:
    return store.list()


@router.post("/{match_id}/join", response_model=MatchJoinResponse)
def join_match(match_id: str, req: MatchJoinRequest) -> MatchJoinResponse:
    try:
        return store.join(match_id, req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{match_id}/state", response_model=MatchStateResponse)
def state_match(match_id: str, token: str | None = Query(default=None)) -> MatchStateResponse:
    try:
        return store.state(match_id, token)
    except KeyError:
        raise HTTPException(status_code=404, detail="match not found")


@router.post("/{match_id}/orders", response_model=MatchOrdersResponse)
def orders_match(match_id: str, req: MatchOrdersRequest) -> MatchOrdersResponse:
    try:
        return store.submit_orders(match_id, req)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{match_id}/leave")
def leave_match(match_id: str, token: str | None = Query(default=None)):
    if not token:
        raise HTTPException(status_code=400, detail="token required")
    try:
        store.leave(match_id, token)
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{match_id}/events")
async def match_events(match_id: str, token: str | None = Query(default=None)):
    try:
        q = store.subscribe(match_id, token)
    except KeyError:
        raise HTTPException(status_code=404, detail="match not found")

    async def event_gen():
        try:
            # initial state push (personalized by token)
            try:
                st = store.snapshot(match_id, token)
                yield "event: state\n" + "data: " + json.dumps(st.model_dump(), ensure_ascii=False) + "\n\n"
            except Exception:
                pass
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield "data: " + data + "\n\n"
                except asyncio.TimeoutError:
                    # Comment line as heartbeat
                    yield ": keepalive\n\n"
        finally:
            store.unsubscribe(match_id, q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/events")
async def lobby_events():
    q = store.lobby_subscribe()
    async def event_gen():
        try:
            # initial list
            try:
                lst = store.list()
                yield "event: list\n" + "data: " + json.dumps(lst.model_dump(), ensure_ascii=False) + "\n\n"
            except Exception:
                pass
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield "data: " + data + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            store.lobby_unsubscribe(q)
    return StreamingResponse(event_gen(), media_type="text/event-stream")
