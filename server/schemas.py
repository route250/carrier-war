import math
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field
try:
    # pydantic v2 provides computed_field for including derived values in serialization
    from pydantic import computed_field
except Exception:  # fallback for environments without pydantic v2
    computed_field = None  # type: ignore

INF:int = 10**8

SQUAD_MAX_HP = 40
CARRIER_MAX_HP = 100
VISION_SQUADRON = 5
VISION_CARRIER = 4
SQUADRON_RANGE = 22

class Position(BaseModel,frozen=True):

    x: int
    y: int

    def __le__(self, other):
        if not isinstance(other, Position):
            return NotImplemented
        return (self.x, self.y) <= (other.x, other.y)

    def __gt__(self, other):
        if not isinstance(other, Position):
            return NotImplemented
        return (self.x, self.y) > (other.x, other.y)

    def __ge__(self, other):
        if not isinstance(other, Position):
            return NotImplemented
        return (self.x, self.y) >= (other.x, other.y)
    def __lt__(self, other):
        if not isinstance(other, Position):
            return NotImplemented
        return (self.x, self.y) < (other.x, other.y)

    def __hash__(self):
        return hash((self.x, self.y))

    def __eq__(self, other):
        if isinstance(other, Position):
            return self.x == other.x and self.y == other.y
        return False

    @staticmethod
    def invalid() -> 'Position':
        return Position(x=-1, y=-1)

    def is_valid(self) -> bool:
        return self.x>=0 and self.y>=0

    @staticmethod
    def new(p1:'int|tuple[int,int]|Position', p2:int|None=None) -> 'Position':
        if isinstance(p1, Position):
            return Position(x=p1.x, y=p1.y)
        elif isinstance(p1, (tuple, list)) and len(p1) == 2:
            return Position(x=p1[0], y=p1[1])
        elif isinstance(p1, int) and isinstance(p2, int):
            return Position(x=p1, y=p2)
        else:
            raise TypeError(f"invalid parameters to Position.new {p1}, {p2}")


    def in_bounds(self, w: int, h: int) -> bool:
        return 0 <= self.x < w and 0 <= self.y < h


    def hex_distance(self, p1:'int|tuple[int,int]|Position', p2:int|None=None) -> int:
        if isinstance(p1, Position):
            x,y = p1.x, p1.y
        elif isinstance(p1, (tuple, list)) and len(p1) == 2:
            x,y = p1
        elif isinstance(p1, int) and isinstance(p2, int):
            x,y = p1,p2
        else:
            raise TypeError("Other must be a Position")
        aq, ar = Position._offset_to_axial(self.x, self.y)
        bq, br = Position._offset_to_axial(x, y)
        ax, ay, az = Position._axial_to_cube(aq, ar)
        bx, by, bz = Position._axial_to_cube(bq, br)
        return Position._cube_distance(ax, ay, az, bx, by, bz)

    @staticmethod
    def _offset_to_axial(col: int, row: int):
        q = col - ((row - (row & 1)) >> 1)
        r = row
        return q, r

    @staticmethod
    def _axial_to_cube(q: int, r: int):
        x = q
        z = r
        y = -x - z
        return x, y, z

    @staticmethod
    def _cube_distance(ax: int, ay: int, az: int, bx: int, by: int, bz: int):
        return max(abs(ax - bx), abs(ay - by), abs(az - bz))

    @staticmethod
    def _hex_distance(pos1: 'Position', pos2: 'Position') -> int:
        aq, ar = Position._offset_to_axial(pos1.x, pos1.y)
        bq, br = Position._offset_to_axial(pos2.x, pos2.y)
        ax, ay, az = Position._axial_to_cube(aq, ar)
        bx, by, bz = Position._axial_to_cube(bq, br)
        return Position._cube_distance(ax, ay, az, bx, by, bz)

    def offset_neighbors(self):
        odd = self.y & 1
        if odd:
            deltas = [(+1, 0), (+1, -1), (0, -1), (-1, 0), (0, +1), (+1, +1)]
        else:
            deltas = [(+1, 0), (0, -1), (-1, -1), (-1, 0), (-1, +1), (0, +1)]
        for dx, dy in deltas:
            yield Position(x=self.x + dx, y=self.y + dy)

    def angle_to(self, other: 'Position') -> float:
        """
        selfからotherへの角度（ラジアン）を返す。
        """
        dx = other.x - self.x
        dy = other.y - self.y
        return math.atan2(dy, dx)

class TrackPos(Position, frozen=True):
    range:int

class UnitState(BaseModel):
    id: str
    side: str
    pos: Position
    hp: int
    max_hp: int
    speed: int
    vision: int
    target: Optional[Position] = None

    def is_active(self) -> bool:
        return self.hp > 0 and self.pos is not None and self.pos.x >= 0 and self.pos.y >= 0

    def can_see_enemy(self, enemy:'UnitState') -> bool:
        """Return True if tile (x,y) is visible to the player (carrier or active squadrons).
        """
        return self.hex_distance(enemy) <= self.vision

    def is_visible_to_player(self, other:'UnitState') -> bool:
        """Return True if tile (x,y) is visible to the player (carrier or active squadrons).
        """
        return self.hex_distance(other) <= self.vision

    def hex_distance(self, other:'UnitState|Position') -> int:
        if self.is_active():
            if isinstance(other, Position) and other.x>=0 and other.y>=0:
                return self.pos.hex_distance(other)
            elif isinstance(other, UnitState) and other.is_active():
                return self.pos.hex_distance(other.pos)
        return INF

    # Flattened coordinates for client convenience (read-only, derived from pos)
    if computed_field:
        @computed_field  # type: ignore[misc]
        def x(self) -> Optional[int]:
            try:
                return self.pos.x if (self.pos and self.pos.x >= 0 and self.pos.y >= 0) else None
            except Exception:
                return None

        @computed_field  # type: ignore[misc]
        def y(self) -> Optional[int]:
            try:
                return self.pos.y if (self.pos and self.pos.x >= 0 and self.pos.y >= 0) else None
            except Exception:
                return None

class CarrierState(UnitState):
    hp: int = CARRIER_MAX_HP
    max_hp: int = CARRIER_MAX_HP
    speed: int = 2
    vision: int = VISION_CARRIER
    hangar: int = 2


class SquadronState(UnitState):
    pos: Position = Position.invalid()
    hp: int = SQUAD_MAX_HP
    max_hp: int = SQUAD_MAX_HP
    speed: int = 4
    vision: int = VISION_SQUADRON
    state: Literal["base", "outbound", "engaging", "returning", "lost"] = "base"

    def is_active(self) -> bool:
        return super().is_active() and self.state != "lost" and self.state != 'base'


class PlayerState(BaseModel):
    side:str
    carrier: CarrierState
    squadrons: List[SquadronState] = []
    # Server-authoritative persistent move target for the player's carrier
    carrier_target: Optional[Position] = Field(default=None, exclude=True)
    # Cross-turn last positions to avoid immediate backtracking across turns
    last_pos_squadrons: Dict[str, Position] = Field(default_factory=dict, exclude=True)
    last_pos_carrier: Optional[Position] = Field(default=None, exclude=True)

    def is_visible_to_player(self, unit:UnitState) -> bool:
        """Return True if tile (x,y) is visible to the player (carrier or active squadrons).
        """
        if self.carrier and self.carrier.is_visible_to_player(unit):
            return True
        for sq in self.squadrons:
            if sq.is_visible_to_player(unit):
                return True
        return False


class IntelMarker(BaseModel):
    seen: bool
    pos: Position
    ttl: int

    # Flattened coordinates for client convenience (read-only)
    if computed_field:
        @computed_field  # type: ignore[misc]
        def x(self) -> Optional[int]:
            try:
                return self.pos.x if (self.pos and self.pos.x >= 0 and self.pos.y >= 0) else None
            except Exception:
                return None

        @computed_field  # type: ignore[misc]
        def y(self) -> Optional[int]:
            try:
                return self.pos.y if (self.pos and self.pos.x >= 0 and self.pos.y >= 0) else None
            except Exception:
                return None


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


class SideIntel(BaseModel):
    # Symmetric intel container per side (server-internal)
    carrier: Optional[IntelMarker] = None
    squadrons: Dict[str, IntelMarker] = Field(default_factory=dict)


class SquadronLight(BaseModel):
    id: str
    pos: Position


class PlayerObservation(BaseModel):
    visible_squadrons: List[SquadronLight] = []


class Config(BaseModel):
    difficulty: Optional[Literal["easy", "normal", "hard"]] = "normal"
    time_ms: Optional[int] = 50


class PlanRequest(BaseModel):
    turn: int
    map: List[List[int]]
    enemy_state: PlayerState
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
    enemy_state: PlayerState
    enemy_memory: EnemyMemory
    player_state: PlayerState
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
    turn: int
    over: bool = False
    result: Optional[Literal['win','lose','draw']] = None
    message: Optional[str] = None

class SessionStepResponse(BaseModel):
    session_id: str
    turn: int
    # Keep these for introspection/compat
    carrier_order: Optional[CarrierOrder] = None
    squadron_orders: List[SquadronOrder] = []
    # Authoritative enemy state after applying orders and progression
    enemy_state: PlayerState
    player_state: PlayerState
    enemy_memory_out: Optional[EnemyMemory] = None
    effects: StepEffects = StepEffects()
    logs: List[str] = []
    metrics: Dict[str, Any] = {}
    request_id: str
    turn_visible: List[str] = []
    game_status: Optional[GameStatus] = None
    # Player intel (server-computed memory based on visibility)
    player_intel: Optional[PlayerIntel] = None
    # Symmetric enemy intel (what enemy knows about player), optional for future clients
    enemy_intel: Optional[PlayerIntel] = Field(default=None, exclude=True)


# === PvP Match (skeleton) ===
# まずは最低限の型を用意（段階的に拡張）
MatchMode = Literal["pve", "pvp"]
MatchStatus = Literal["waiting", "active", "over"]


class MatchCreateRequest(BaseModel):
    mode: Optional[MatchMode] = "pvp"
    config: Optional[Config] = None
    display_name: Optional[str] = None


class MatchCreateResponse(BaseModel):
    match_id: str
    player_token: str
    side: Literal["A", "B"] = "A"
    status: MatchStatus = "waiting"
    mode: MatchMode = "pvp"
    config: Optional[Config] = None


class MatchListItem(BaseModel):
    match_id: str
    status: MatchStatus
    mode: MatchMode
    has_open_slot: bool
    created_at: int
    config: Optional[Config] = None


class MatchListResponse(BaseModel):
    matches: List[MatchListItem] = []


class MatchJoinRequest(BaseModel):
    display_name: Optional[str] = None


class MatchJoinResponse(BaseModel):
    match_id: str
    player_token: str
    side: Literal["A", "B"]
    status: MatchStatus


class MatchStateResponse(BaseModel):
    match_id: str
    status: MatchStatus
    mode: MatchMode
    turn: int
    your_side: Optional[Literal["A", "B"]] = None
    waiting_for: Literal["none", "you", "opponent"] = "none"
    # Optional minimal board + units snapshot for polling UI
    map_w: Optional[int] = None
    map_h: Optional[int] = None
    a: Optional[Dict[str, Any]] = None  # expects {"carrier": {"x","y","hp"}}
    b: Optional[Dict[str, Any]] = None


class MatchOrdersRequest(BaseModel):
    player_token: str
    player_orders: Optional[PlayerOrders] = None
    # 将来: readyフラグ/キャンセル等


class MatchOrdersResponse(BaseModel):
    accepted: bool = True
    status: MatchStatus
    turn: int

class IntelPath(BaseModel):
    """索敵結果"""
    side: str
    unit_id: str
    turn: int
    p1: Position
    p2: Position

class IntelReport(BaseModel):
    """索敵報告"""
    turn: int
    side: str
    logs: List[str] = []
    units: List[UnitState] = []
    intel: dict[str,IntelPath] = {}

    def dump(self):
        yield f"side: {self.side} turn: {self.turn}"
        for log in self.logs:
            yield f"  log: {log}"
        for unit in self.units:
            if isinstance(unit,CarrierState):
                yield f"  unit: {unit.id} pos: {unit.pos} hp: {unit.hp}"
            elif isinstance(unit,SquadronState):
                loc = f"{unit.state}"
                if unit.pos.is_valid():
                    loc = loc + f"({unit.pos.x},{unit.pos.y})"
                yield f"  unit: {unit.id} {loc} hp: {unit.hp}"
        for path in self.intel.values():
            yield f"  intel: {path.unit_id} from {path.p1} to {path.p2}"
