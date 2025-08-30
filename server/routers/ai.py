from fastapi import APIRouter

from server.schemas import PlanRequest, PlanResponse
from server.services.ai import plan_orders


router = APIRouter()


@router.post("/plan", response_model=PlanResponse)
def post_plan(req: PlanRequest) -> PlanResponse:
    return plan_orders(req)
