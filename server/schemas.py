from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel


class Position(BaseModel):
    x: int
    y: int


class CarrierState(BaseModel):
    id: str
    x: int
    y: int
    hp: int = 100
    speed: int = 2
    vision: int = 4
    hangar: int = 2


class SquadronState(BaseModel):
    id: str
    state: Literal["base", "outbound", "engaging", "returning", "lost"]
    hp: int = 40
    x: Optional[int] = None
    y: Optional[int] = None
    target: Optional[Position] = None


class EnemyState(BaseModel):
    carrier: CarrierState
    squadrons: List[SquadronState] = []


class IntelMarker(BaseModel):
    seen: bool
    x: Optional[int] = None
    y: Optional[int] = None
    ttl: int = 0


class EnemyAIState(BaseModel):
    patrol_ix: int = 0
    last_patrol_turn: int = 0


class EnemyMemory(BaseModel):
    carrier_last_seen: Optional[IntelMarker] = None
    enemy_ai: Optional[EnemyAIState] = None


class SquadronIntel(BaseModel):
    id: str
    marker: IntelMarker


class PlayerIntel(BaseModel):
    carrier: Optional[IntelMarker] = None
    squadrons: List[SquadronIntel] = []


class SquadronLight(BaseModel):
    id: str
    x: int
    y: int


class PlayerObservation(BaseModel):
    visible_squadrons: List[SquadronLight] = []


class Config(BaseModel):
    difficulty: Optional[Literal["easy", "normal", "hard"]] = "normal"
    time_ms: Optional[int] = 50


class PlanRequest(BaseModel):
    turn: int
    map: List[List[int]]
    enemy_state: EnemyState
    enemy_memory: Optional[EnemyMemory] = None
    player_observation: Optional[PlayerObservation] = None
    config: Optional[Config] = None
    rand_seed: Optional[int] = None


class CarrierOrder(BaseModel):
    type: Literal["move", "hold"]
    target: Optional[Position] = None


class SquadronOrder(BaseModel):
    id: str
    action: Literal["launch", "engage", "return", "hold"]
    target: Optional[Position] = None


class PlanResponse(BaseModel):
    carrier_order: CarrierOrder
    squadron_orders: List[SquadronOrder] = []
    enemy_memory_out: Optional[EnemyMemory] = None
    logs: List[str] = []
    metrics: Dict[str, Any] = {}
    request_id: str


# === Session (Step 2) ===
class SessionCreateRequest(BaseModel):
    config: Optional[Config] = None
    rand_seed: Optional[int] = None


class SessionCreateResponse(BaseModel):
    session_id: str
    map: List[List[int]]
    enemy_state: EnemyState
    enemy_memory: EnemyMemory
    player_state: EnemyState
    turn: int = 1
    config: Optional[Config] = None


class PlayerOrders(BaseModel):
    carrier_target: Optional[Position] = None
    launch_target: Optional[Position] = None


class SessionStepRequest(BaseModel):
    # Player commands for server-side resolution
    player_orders: Optional[PlayerOrders] = None
    config: Optional[Config] = None


class StepEffects(BaseModel):
    player_carrier_damage: int = 0


class GameStatus(BaseModel):
    over: bool = False
    result: Optional[Literal['win','lose','draw']] = None
    message: Optional[str] = None
    turn: Optional[int] = None


class SessionStepResponse(BaseModel):
    session_id: str
    turn: int
    # Keep these for introspection/compat
    carrier_order: Optional[CarrierOrder] = None
    squadron_orders: List[SquadronOrder] = []
    # Authoritative enemy state after applying orders and progression
    enemy_state: EnemyState
    player_state: EnemyState
    enemy_memory_out: Optional[EnemyMemory] = None
    effects: StepEffects = StepEffects()
    logs: List[str] = []
    metrics: Dict[str, Any] = {}
    request_id: str
    turn_visible: List[str] = []
    game_status: Optional[GameStatus] = None
    # Player intel (server-computed memory based on visibility)
    player_intel: Optional[PlayerIntel] = None
