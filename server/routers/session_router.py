from fastapi import APIRouter, HTTPException

from server.schemas import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStepRequest,
    SessionStepResponse,
)
from server.services.session import store


router = APIRouter()


@router.post("/", response_model=SessionCreateResponse)
def create_session(req: SessionCreateRequest) -> SessionCreateResponse:
    return store.create(req)


@router.post("/{session_id}/step", response_model=SessionStepResponse)
def step_session(session_id: str, req: SessionStepRequest) -> SessionStepResponse:
    try:
        return store.step(session_id, req)
    except KeyError:
        raise HTTPException(status_code=404, detail="session not found")
    except Exception as e:
        import traceback
        print("STEP ERROR:\n" + traceback.format_exc())
        raise
