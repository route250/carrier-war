import time
import uuid
from typing import List, Optional, Tuple
import random

from server.schemas import (
    PlanRequest,
    PlanResponse,
    CarrierOrder,
    SquadronOrder,
    Position,
    EnemyMemory,
    IntelMarker,
    EnemyAIState,
)


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

    dt_ms = int((time.perf_counter() - t0) * 1000)
    resp = PlanResponse(
        carrier_order=carrier_order,
        squadron_orders=squadron_orders,
        enemy_memory_out=mem_out,
        logs=logs,
        metrics={"latency_ms": dt_ms},
        request_id=str(uuid.uuid4()),
    )
    return resp
