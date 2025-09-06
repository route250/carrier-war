"""
CPU向けAI実装（PvPエンジン上で動かす最小版）

目的:
- 既存の PvE 用 AI ルーチン（server/services/ai.py の plan_orders）を流用し、
  AIThreadABC 上で B 側ボットとして動作させる。

方針:
- 初回のみ `MatchStore.snapshot()` を用いて地形 `map` を取得・保持（以後は使い回し）。
- `build_state_payload(viewer_side=B)` で渡される `payload` から自軍の状態を再構築し、
  `PlanRequest` を作成して `plan_orders` を呼び出す。
- 戻り値 `PlanResponse` を `PlayerOrders`（carrier_target / launch_target）に写像して提出。

注意:
- 本ファイルは UI やルータへの変更を行わない。既存のフローに影響せずに差し込める。
"""

from __future__ import annotations
from dataclasses import dataclass, field
import time
from typing import List, Literal, Optional
import random


from server.services.ai_base import AIThreadABC

from server.schemas import Config, MatchStatePayload, PlayerOrders, Position, UnitState, CarrierState, SquadronState

from server.schemas import (
    CARRIER_MAX_HP,
    CARRIER_SPEED,
    CARRIER_HANGAR,
    CARRIER_RANGE,
    VISION_CARRIER,
    SQUAD_MAX_HP,
    SQUAD_SPEED,
    SQUADRON_RANGE,
    VISION_SQUADRON,
)

@dataclass
class EnemyAIState:
    patrol_ix: int = 0
    last_patrol_turn: int = 0

@dataclass
class IntelMarker:
    seen: bool
    pos: Position
    ttl: int

    @property
    def x(self) -> Optional[int]:
        try:
            return self.pos.x if (self.pos and self.pos.x >= 0 and self.pos.y >= 0) else None
        except Exception:
            return None

    @property
    def y(self) -> Optional[int]:
        try:
            return self.pos.y if (self.pos and self.pos.x >= 0 and self.pos.y >= 0) else None
        except Exception:
            return None
            
@dataclass
class EnemyMemory:
    carrier_last_seen: Optional[IntelMarker] = None
    enemy_ai: Optional[EnemyAIState] = None

@dataclass
class SquadronLight:
    id: str
    pos: Position

@dataclass
class PlayerObservation:
    visible_squadrons: List[SquadronLight] = field(default_factory=list)

@dataclass
class CarrierOrder:
    type: Literal["move", "hold"]
    target: Optional[Position] = None

@dataclass
class SquadronOrder:
    id: str
    action: Literal["launch", "engage", "return", "hold"]
    target: Optional[Position] = None

@dataclass
class PlanRequest:
    turn: int
    map: List[List[int]]
    enemy_state: PlayerState
    enemy_memory: Optional[EnemyMemory] = None
    player_observation: Optional[PlayerObservation] = None
    config: Optional[Config] = None
    rand_seed: Optional[int] = None

@dataclass
class PlanResponse:
    carrier_order: CarrierOrder
    squadron_orders: List[SquadronOrder] = field(default_factory=list)
    enemy_memory_out: Optional[EnemyMemory] = None
    logs: List[str] = field(default_factory=list)

@dataclass
class PlayerState:
    side:str
    carrier: CarrierState
    squadrons: List[SquadronState] = field(default_factory=list)
    carrier_target: Optional[Position] = None

def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))

def _pos_clamp( pos:Position, width:int, height:int) -> Position:
    return Position(x=_clamp(pos.x, 0, width - 1), y=_clamp(pos.y, 0, height - 1))

def _is_sea(grid: List[List[int]], pos:Position) -> bool:
    try:
        return grid[pos.y][pos.x] == 0
    except Exception:
        return False

def _chebyshev(a: Position, b: Position) -> int:
    return max(abs(a.x - b.x), abs(a.y - b.y))


def _offset_neighbors_odd_r(pos: Position) -> List[Position]:
    odd = pos.y & 1
    if odd:
        deltas = [(+1, 0), (+1, -1), (0, -1), (-1, 0), (0, +1), (+1, +1)]
    else:
        deltas = [(+1, 0), (0, -1), (-1, -1), (-1, 0), (-1, +1), (0, +1)]
    return [Position(x=pos.x + dx, y=pos.y + dy) for dx, dy in deltas]


def _nearest_sea(grid: List[List[int]], pos: Position, w: int, h: int) -> Position:
    pos = _pos_clamp(pos, w, h)
    if _is_sea(grid, pos):
        return pos
    for r in range(1, 7):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                np = _pos_clamp( Position(x=pos.x+dx,y=pos.y+dy), w, h)
                if _is_sea(grid, np):
                    return np
    return pos


def plan_orders(req: PlanRequest) -> PlanResponse:
    t0 = time.perf_counter()
    rng = random.Random(req.rand_seed if req.rand_seed is not None else (req.turn * 7919))
    width = len(req.map[0]) if req.map and req.map[0] else 30
    height = len(req.map) if req.map else 30

    ec = req.enemy_state.carrier
    start = _pos_clamp(ec.pos, width, height)
    here = Position(x=start.x, y=start.y)

    # Known last seen player carrier position (if any)
    last_seen: Optional[Position] = None
    mem_in = req.enemy_memory.carrier_last_seen if req.enemy_memory else None
    if mem_in and mem_in.seen and mem_in.ttl > 0 and mem_in.pos is not None:
        last_seen = _pos_clamp( mem_in.pos, width, height)

    logs: List[str] = []

    # Occupancy sets (known to server)
    enemy_occ = set([s.pos for s in req.enemy_state.squadrons if s.state not in ("base", "lost")])
    player_vis_occ = set()
    if req.player_observation:
        player_vis_occ = set([p.pos for p in req.player_observation.visible_squadrons])

    def cell_free(pos:Position) -> bool:
        return pos.in_bounds(width, height) and _is_sea(req.map, pos) and pos not in enemy_occ and pos not in player_vis_occ

    # Enemy carrier movement: 0..speed steps, biased away from last_seen if known
    steps = rng.randint(0, max(0, ec.speed))
    moved = False
    for _ in range(steps):
        nbs = [nxny for nxny in _offset_neighbors_odd_r(here) if cell_free(nxny)]
        if not nbs:
            break
        if last_seen is not None:
            curd = _chebyshev(here, last_seen)
            # score: prefer larger distance from last_seen, add tiny jitter
            scored = []
            for npos in nbs:
                d = max(abs(npos.x - last_seen.x), abs(npos.y - last_seen.y))
                scored.append(((d - curd) + rng.random() * 0.05, npos))
            scored.sort(key=lambda t: t[0], reverse=True)
            best_score, npos = scored[0]
            # If nothing improves distance, sometimes keep position (50%)
            if best_score <= 0 and rng.random() < 0.5:
                break
        else:
            npos = rng.choice(nbs)
        here = Position(x=npos.x, y=npos.y)
        moved = True

    if moved and (here.x != start.x or here.y != start.y):
        carrier_order = CarrierOrder(type="move", target=here)
        logs.append("敵空母は回避運動")
        if last_seen is not None:
            logs[-1] = "敵空母は観測座標から離隔"
    else:
        carrier_order = CarrierOrder(type="hold")

    # Squadron orders (mirror browser logic):
    # If have known target -> launch one base squadron. Else patrol every 3 turns using patrol points.
    squadron_orders: List[SquadronOrder] = []
    active_cnt = sum(1 for s in req.enemy_state.squadrons if s.state not in ("base", "lost"))
    base_avail = next((s for s in req.enemy_state.squadrons if s.state == "base" and (s.hp is None or s.hp > 0)), None)
    launched_to_known = False
    launched_patrol = False
    if active_cnt < ec.hangar and base_avail is not None:
        if last_seen is not None and mem_in and mem_in.ttl > 0:
            squadron_orders.append(SquadronOrder(id=base_avail.id, action="launch", target=last_seen))
            logs.append("敵編隊が出撃した気配")
            launched_to_known = True
        else:
            # Patrol cadence
            ai_in = req.enemy_memory.enemy_ai if req.enemy_memory and req.enemy_memory.enemy_ai else EnemyAIState()
            turns_since = req.turn - (ai_in.last_patrol_turn or 0)
            # patrol cadence depends on difficulty
            diff = (req.config.difficulty if req.config and req.config.difficulty else 'normal')
            cadence = 3 if diff == 'normal' else (2 if diff == 'hard' else 4)
            if turns_since >= cadence:
                # Patrol waypoints: four corners + center
                pts = [
                    Position(x=4, y=4),
                    Position(x=width - 5, y=4),
                    Position(x=4, y=height - 5),
                    Position(x=width - 5, y=height - 5),
                    Position(x=width // 2, y=height // 2),
                ]
                wp = pts[ai_in.patrol_ix % len(pts)]
                tgt = _nearest_sea(req.map, wp, width, height)
                squadron_orders.append(SquadronOrder(id=base_avail.id, action="launch", target=tgt))
                logs.append("敵編隊が索敵に出撃した気配")
                launched_patrol = True

    # Memory evolution
    mem_out = EnemyMemory()
    # Carrier sighting TTL: if visible-now keep TTL (set by client). Otherwise decay by 1. If launched on known, decay once more.
    if mem_in:
        visible_now = mem_in.ttl >= 3  # client sets to 3 when visible
        ttl_next = mem_in.ttl if visible_now else max(0, mem_in.ttl - 1)
        if launched_to_known:
            ttl_next = max(0, ttl_next - 1)
        mem_out.carrier_last_seen = IntelMarker(seen=ttl_next > 0, pos=mem_in.pos, ttl=ttl_next)

    # Enemy AI patrol memory
    ai_in = req.enemy_memory.enemy_ai if req.enemy_memory and req.enemy_memory.enemy_ai else EnemyAIState()
    ai_out = EnemyAIState(patrol_ix=ai_in.patrol_ix, last_patrol_turn=ai_in.last_patrol_turn)
    if launched_patrol:
        ai_out.patrol_ix = ai_in.patrol_ix + 1
        ai_out.last_patrol_turn = req.turn
    mem_out.enemy_ai = ai_out

    resp = PlanResponse(
        carrier_order=carrier_order,
        squadron_orders=squadron_orders,
        enemy_memory_out=mem_out,
        logs=logs,
    )
    return resp

class CarrierBotMedium(AIThreadABC):
    """PvP用CPUボット（最小実装）

    - AIThreadABC の `think(payload: dict)` を実装し、既存 `plan_orders` を呼び出す。
    - 地形 `map` は最初の呼び出し時に `store.snapshot()` で取得してキャッシュする。
    """

    def __init__(self, store, match_id: str, *, name: str = "CPU(Medium)", config: Config | None = None):
        super().__init__(store=store, match_id=match_id)
        self.name = name
        self._map: Optional[List[List[int]]] = None
        self._memory: Optional[EnemyMemory] = None
        self._config: Optional[Config] = config

    def think(self, payload: MatchStatePayload) -> None:  # type: ignore[override]
        # 1) 地形マップを確保（初回のみ）
        if self._map is None:
            try:
                snap = self.store.snapshot(self.match_id, self.token)
                self._map = snap.map
            except Exception:
                self._map = None
        if not self._map:
            # マップが無ければ安全策として何も出さない
            self.on_orders(PlayerOrders())
            return

        # 2) state payload から自軍（AI側）状態を復元
        enemy_state = self._payload_to_player_state(payload)
        if enemy_state is None:
            # 復元できない場合はノーオーダー
            self.on_orders(PlayerOrders())
            return

        # 3) PlayerObservation（任意）: 可視編隊のみ最小反映（なければ None でOK）
        player_obs = self._payload_to_player_observation(payload)

        # 4) 既存AIへ入力してオーダーを算出
        req = PlanRequest(
            turn=payload.turn,
            map=self._map,
            enemy_state=enemy_state,
            enemy_memory=self._memory,
            player_observation=player_obs,
            config=self._config,
            rand_seed=None,
        )
        resp: PlanResponse = plan_orders(req)

        # 5) 既存AIの応答を PlayerOrders へ写像
        orders = self._plan_to_player_orders(resp)

        # メモリ更新
        self._memory = resp.enemy_memory_out or self._memory

        # 6) サーバへ提出
        self.on_orders(orders)

    # --- helpers ---
    def _payload_to_player_state(self, payload: MatchStatePayload) -> Optional[PlayerState]:
        try:
            units = payload.units
            carr = units.carrier
            if not carr:
                return None
            cx = carr.x
            cy = carr.y
            if cx is None or cy is None:
                return None
            carrier = CarrierState(
                id=carr.id or "C",
                side=self.side or "B",
                pos=Position(x=int(cx), y=int(cy)),
                hp=int(carr.hp) if carr.hp is not None else CARRIER_MAX_HP,
                max_hp=int(carr.max_hp) if carr.max_hp is not None else CARRIER_MAX_HP,
                speed=int(carr.speed) if carr.speed is not None else CARRIER_SPEED,
                fuel=int(carr.fuel) if carr.fuel is not None else CARRIER_RANGE,
                vision=int(carr.vision) if carr.vision is not None else VISION_CARRIER,
            )

            sq_list = []
            for sq in units.squadrons or []:
                pos_x = sq.x
                pos_y = sq.y
                squad = SquadronState(
                    id=sq.id or "SQ",
                    side=self.side or "B",
                    hp=int(sq.hp) if sq.hp is not None else SQUAD_MAX_HP,
                    max_hp=int(sq.max_hp) if sq.max_hp is not None else SQUAD_MAX_HP,
                    speed=int(sq.speed) if sq.speed is not None else SQUAD_SPEED,
                    fuel=int(sq.fuel) if sq.fuel is not None else SQUADRON_RANGE,
                    vision=int(sq.vision) if sq.vision is not None else VISION_SQUADRON,
                    state=str(sq.state or "base"),
                )
                if pos_x is not None and pos_y is not None:
                    squad.pos = Position(x=int(pos_x), y=int(pos_y))
                sq_list.append(squad)

            return PlayerState(side=self.side or "B", carrier=carrier, squadrons=sq_list)
        except Exception:
            return None

    def _payload_to_player_observation(self, payload: MatchStatePayload) -> Optional[PlayerObservation]:
        try:
            # 現状の state には敵編隊の最小情報を返す設計（intel）だが、
            # ここでは安全側へ倒して None または空観測を返す。
            # 将来、`intel.squadrons` 等が付与されたら変換を実装。
            return None
        except Exception:
            return None

    def _plan_to_player_orders(self, resp: PlanResponse) -> PlayerOrders:
        carrier_target = None
        launch_target = None

        # Carrier
        try:
            co = resp.carrier_order
            if co and isinstance(co, CarrierOrder) and getattr(co, "type", None) == "move" and co.target is not None:
                carrier_target = Position(x=co.target.x, y=co.target.y)
        except Exception:
            pass

        # One squadron (first) launch
        try:
            for so in resp.squadron_orders or []:
                if isinstance(so, SquadronOrder) and getattr(so, "action", None) == "launch" and so.target is not None:
                    launch_target = Position(x=so.target.x, y=so.target.y)
                    break
        except Exception:
            pass

        return PlayerOrders(carrier_target=carrier_target, launch_target=launch_target)
