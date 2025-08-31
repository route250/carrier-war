import uuid
import random
import heapq
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

from server.schemas import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStepRequest,
    SessionStepResponse,
    EnemyState,
    CarrierState,
    SquadronState,
    EnemyMemory,
    EnemyAIState,
    IntelMarker,
    PlanRequest,
    SquadronOrder,
    Position,
    PlayerIntel,
    SquadronIntel,
    PlayerObservation,
    SquadronLight,
)
from server.services.ai import plan_orders
from server.utils.audit import audit_write, maplog_write


@dataclass
class Session:
    id: str
    map: list
    enemy_state: EnemyState
    player_state: EnemyState
    enemy_memory: EnemyMemory = field(default_factory=EnemyMemory)
    rand_seed: Optional[int] = None
    config: Optional[dict] = None
    turn: int = 1
    max_turns: int = 20
    # Player intel memory (server-side)
    player_intel_carrier: Optional[IntelMarker] = None
    player_intel_squadrons: Dict[str, IntelMarker] = field(default_factory=dict)
    # Cross-turn last positions to avoid immediate backtracking across turns
    last_pos_player_sq: Dict[str, tuple] = field(default_factory=dict)
    last_pos_enemy_sq: Dict[str, tuple] = field(default_factory=dict)
    last_pos_player_carrier: Optional[tuple] = None
    last_pos_enemy_carrier: Optional[tuple] = None
    # Map log: remember last user-ordered target to avoid duplicate logs each turn
    last_logged_player_target: Optional[tuple] = None


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}

    def create(self, req: SessionCreateRequest) -> SessionCreateResponse:
        sid = str(uuid.uuid4())
        # Map: if not provided, generate a connected one
        width = 30
        height = 30
        game_map = _generate_connected_map(width, height)
        # If not provided, seed a minimal enemy state
        enemy_carrier = CarrierState(id="E1", x=26, y=26)
        enemy_squadrons = [SquadronState(id=f"ESQ{i+1}", state='base', hp=SQUAD_MAX_HP) for i in range(enemy_carrier.hangar)]
        enemy_state = EnemyState(carrier=enemy_carrier, squadrons=enemy_squadrons)

        player_carrier = CarrierState(id="C1", x=3, y=3)
        player_squadrons = [SquadronState(id=f"SQ{i+1}", state='base', hp=SQUAD_MAX_HP) for i in range(player_carrier.hangar)]
        player_state = EnemyState(carrier=player_carrier, squadrons=player_squadrons)
       
        # Ensure starting positions are on sea (carve small sea around if necessary)
        _carve_sea(game_map, player_state.carrier.x, player_state.carrier.y, 2)
        _carve_sea(game_map, enemy_state.carrier.x, enemy_state.carrier.y, 2)
        # Regenerate if somehow disconnected after carving
        for _ in range(10):
            tmp_sess = Session(
                id=sid,
                map=game_map,
                enemy_state=enemy_state,
                player_state=player_state,
                enemy_memory=EnemyMemory(enemy_ai=EnemyAIState()),
            )
            ok, sea_total, sea_reached = _validate_sea_connectivity(tmp_sess)
            if ok:
                break
            game_map = _generate_connected_map(width, height)
            _carve_sea(game_map, player_state.carrier.x, player_state.carrier.y, 2)
            _carve_sea(game_map, enemy_state.carrier.x, enemy_state.carrier.y, 2)
        sess = Session(
            id=sid,
            map=game_map,
            enemy_state=enemy_state,
            player_state=player_state,
            enemy_memory=EnemyMemory(enemy_ai=EnemyAIState()),
            rand_seed=req.rand_seed,
            config=req.config.dict() if req.config else None,
        )
        self._sessions[sid] = sess
        # Write a session bootstrap record so we can fully reproduce
        try:
            audit_write(
                sid,
                {
                    "type": "session_start",
                    "map_w": len(sess.map[0]) if sess.map else 0,
                    "map_h": len(sess.map),
                    "map": sess.map,
                    "player_state": player_state.dict(),
                    "enemy_state": enemy_state.dict(),
                    "config": (req.config.dict() if req.config else None),
                    "rand_seed": req.rand_seed,
                    "constants": {
                        "SQUADRON_RANGE": SQUADRON_RANGE,
                        "VISION_SQUADRON": VISION_SQUADRON,
                        "VISION_CARRIER": VISION_CARRIER,
                        "SQUAD_MAX_HP": SQUAD_MAX_HP,
                        "CARRIER_MAX_HP": CARRIER_MAX_HP,
                    },
                    "movement": {
                        "solver": "wavefront",
                        "aircraft_pass_islands": True,
                        "carrier_pass_islands": False,
                    },
                },
            )
        except Exception:
            pass
        # Validate sea connectivity (all sea tiles mutually reachable)
        try:
            ok, sea_total, sea_reached = _validate_sea_connectivity(sess)
            audit_write(
                sid,
                {
                    "type": "map_validation",
                    "ok": ok,
                    "sea_total": sea_total,
                    "sea_reached": sea_reached,
                    "unreached": max(0, sea_total - sea_reached),
                },
            )
        except Exception:
            pass
        # Write a compact .map bootstrap file (map only)
        try:
            maplog_write(
                sid,
                {
                    "type": "map",
                    "map_w": len(sess.map[0]) if sess.map else 0,
                    "map_h": len(sess.map),
                    "map": sess.map,
                },
            )
        except Exception:
            pass
        return SessionCreateResponse(
            session_id=sid,
            map=sess.map,
            enemy_state=sess.enemy_state,
            player_state=sess.player_state,
            enemy_memory=sess.enemy_memory,
            turn=sess.turn,
            config=req.config,
        )

    def get(self, sid: str) -> Session:
        return self._sessions[sid]

    def step(self, session_id: str, req: SessionStepRequest) -> SessionStepResponse:
        sess = self.get(session_id)
        aud = lambda ev: audit_write(session_id, {"turn": sess.turn, **ev})
        aud({"type": "turn_start"})
        # advance turn counter
        sess.turn += 1

        # Update memory: if player carrier visible, set TTL=3 at provided coords
        player_visible_carrier = _enemy_sees_player_carrier(sess)
        if player_visible_carrier is not None:
            sess.enemy_memory.carrier_last_seen = IntelMarker(
                seen=True,
                x=player_visible_carrier.x,
                y=player_visible_carrier.y,
                ttl=3,
            )

        # Compute server-side view of which player squadrons are visible to enemy
        player_observation = _compute_visible_player_squadrons(sess)

        # Build PlanRequest using session state
        plan_req = PlanRequest(
            turn=sess.turn,
            map=sess.map,
            enemy_state=sess.enemy_state,
            enemy_memory=sess.enemy_memory,
            player_observation=player_observation,
            config=req.config,
            rand_seed=sess.rand_seed,
        )
        plan_resp = plan_orders(plan_req)
        # Apply memory out back to session
        if plan_resp.enemy_memory_out is not None:
            sess.enemy_memory = plan_resp.enemy_memory_out
        logs = list(plan_resp.logs)
        # Apply enemy carrier position move if any
        if plan_resp.carrier_order and plan_resp.carrier_order.type == "move" and plan_resp.carrier_order.target is not None:
            # cross-turn backtrack memory for carrier
            sess.last_pos_enemy_carrier = (sess.enemy_state.carrier.x, sess.enemy_state.carrier.y)
            aud({
                "type": "move", "side": "enemy", "unit": "carrier",
                "from": [sess.enemy_state.carrier.x, sess.enemy_state.carrier.y],
                "to": [plan_resp.carrier_order.target.x, plan_resp.carrier_order.target.y],
            })
            sess.enemy_state.carrier.x = plan_resp.carrier_order.target.x
            sess.enemy_state.carrier.y = plan_resp.carrier_order.target.y

        # Apply squadron orders (launch/return/engage)
        for od in (plan_resp.squadron_orders or []):
            sq = next((s for s in sess.enemy_state.squadrons if s.id == od.id), None)
            if not sq:
                continue
            if od.action == "launch" and od.target is not None and sq.state == "base" and (sq.hp or 0) > 0:
                tgt = Position(x=od.target.x, y=od.target.y)
                spawn = _find_free_adjacent(sess, sess.enemy_state.carrier.x, sess.enemy_state.carrier.y, prefer_away_from=tgt)
                if spawn:
                    aud({
                        "type": "launch", "side": "enemy", "unit": "squadron", "id": sq.id,
                        "spawn": [spawn.x, spawn.y], "target": [tgt.x, tgt.y]
                    })
                    sq.x, sq.y = spawn.x, spawn.y
                    sq.target = tgt
                    sq.state = "outbound"
            elif od.action == "return":
                if sq.state not in ("base", "lost"):
                    sq.state = "returning"
            elif od.action == "engage" and od.target is not None:
                if sq.state not in ("base", "lost"):
                    sq.target = Position(x=od.target.x, y=od.target.y)
                    sq.state = "engaging"

        # Apply player orders (carrier move + launch)
        path_sweep: list[dict] = []
        audit_events: list[dict] = []
        _apply_player_orders(sess, req, path_sweep, audit_events)

        # Progress both sides squadrons
        dmg_to_player = _progress_enemy_squadrons(sess, player_observation, audit_events)
        dmg_to_enemy = _progress_player_squadrons(sess, path_sweep, audit_events)
        # Apply carrier damages
        if dmg_to_player:
            sess.player_state.carrier.hp = max(0, (sess.player_state.carrier.hp or 0) - int(dmg_to_player))
        if dmg_to_enemy:
            sess.enemy_state.carrier.hp = max(0, (sess.enemy_state.carrier.hp or 0) - int(dmg_to_enemy))
        if dmg_to_player > 0:
            logs.append(f"敵編隊が我が空母を攻撃（{dmg_to_player}累計）")
        if dmg_to_enemy > 0:
            logs.append(f"我が編隊が敵空母を攻撃（{dmg_to_enemy}累計）")

        # Compute player turn visibility
        turn_visible = _compute_player_visibility(sess, path_sweep)
        # Update and emit player intel
        # keep previous intel snapshot for change detection
        prev_carrier = sess.player_intel_carrier
        prev_sq = dict(sess.player_intel_squadrons)
        player_intel = _update_player_intel(sess, turn_visible)
        # detection events
        if player_intel.carrier and (not prev_carrier or not prev_carrier.seen or prev_carrier.ttl <= 0):
            aud({"type": "detect", "side": "player", "unit": "enemy_carrier", "pos": [player_intel.carrier.x, player_intel.carrier.y]})
        for item in (player_intel.squadrons or []):
            prev = prev_sq.get(item.id)
            if item.marker and (not prev or not prev.seen or prev.ttl <= 0):
                aud({"type": "detect", "side": "player", "unit": "enemy_squadron", "id": item.id, "pos": [item.marker.x, item.marker.y]})

        # flush accumulated audit events
        for ev in audit_events:
            aud(ev)

        # Game status
        status = _game_status(sess)

        return SessionStepResponse(
            session_id = sess.id,
            turn=sess.turn,
            carrier_order=plan_resp.carrier_order,
            squadron_orders=plan_resp.squadron_orders,
            enemy_state=sess.enemy_state,
            player_state=sess.player_state,
            enemy_memory_out=sess.enemy_memory,
            effects={"player_carrier_damage": max(0, int(dmg_to_player))},
            turn_visible=sorted(list(turn_visible)),
            game_status=status,
            player_intel=player_intel,
            logs=logs,
            metrics=plan_resp.metrics,
            request_id=plan_resp.request_id,
        )


store = SessionStore()


# ==== Server-side game helpers (enemy side only) ====
SQUAD_MAX_HP = 40
CARRIER_MAX_HP = 100
VISION_SQUADRON = 5
VISION_CARRIER = 4
SQUADRON_RANGE = 22

def _offset_neighbors(x: int, y: int):
    odd = y & 1
    if odd:
        deltas = [(+1, 0), (+1, -1), (0, -1), (-1, 0), (0, +1), (+1, +1)]
    else:
        deltas = [(+1, 0), (0, -1), (-1, -1), (-1, 0), (-1, +1), (0, +1)]
    for dx, dy in deltas:
        yield x + dx, y + dy


def _offset_to_axial(col: int, row: int):
    q = col - ((row - (row & 1)) >> 1)
    r = row
    return q, r


def _axial_to_cube(q: int, r: int):
    x = q
    z = r
    y = -x - z
    return x, y, z


def _cube_distance(ax: int, ay: int, az: int, bx: int, by: int, bz: int):
    return max(abs(ax - bx), abs(ay - by), abs(az - bz))


def _hex_distance(x1: int, y1: int, x2: int, y2: int) -> int:
    aq, ar = _offset_to_axial(x1, y1)
    bq, br = _offset_to_axial(x2, y2)
    ax, ay, az = _axial_to_cube(aq, ar)
    bx, by, bz = _axial_to_cube(bq, br)
    return _cube_distance(ax, ay, az, bx, by, bz)


def is_visible_to_player(sess: Session, x: int, y: int) -> bool:
    """Return True if tile (x,y) is visible to the player (carrier or active squadrons).

    Mirrors the client-side isVisibleToPlayer implementation.
    """
    pc = sess.player_state.carrier
    if pc and _hex_distance(pc.x, pc.y, x, y) <= getattr(pc, 'vision', VISION_CARRIER):
        return True
    for sq in sess.player_state.squadrons:
        if getattr(sq, 'state', None) in ('base', 'lost'):
            continue
        if sq.x is None or sq.y is None:
            continue
        if _hex_distance(sq.x, sq.y, x, y) <= getattr(sq, 'vision', VISION_SQUADRON):
            return True
    return False


def is_visible_to_enemy(sess: Session, x: int, y: int) -> bool:
    """Return True if tile (x,y) is visible to the enemy (enemy carrier or active enemy squadrons).

    Mirrors the client-side isVisibleToEnemy implementation.
    """
    ec = sess.enemy_state.carrier
    if ec and _hex_distance(ec.x, ec.y, x, y) <= getattr(ec, 'vision', VISION_CARRIER):
        return True
    for sq in sess.enemy_state.squadrons:
        if getattr(sq, 'state', None) in ('base', 'lost'):
            continue
        if sq.x is None or sq.y is None:
            continue
        if _hex_distance(sq.x, sq.y, x, y) <= getattr(sq, 'vision', VISION_SQUADRON):
            return True
    return False


def _compute_visible_player_squadrons(sess: Session) -> PlayerObservation:
    """Return a list of SquadronLight for player squadrons that are visible to the enemy.

    Mirrors the client-side logic in static/main.js: filter out 'base'/'lost' squadrons
    and those without coords, then include those for which is_visible_to_enemy(...) is True.
    """
    visible = []
    for ps in sess.player_state.squadrons:
        if getattr(ps, 'state', None) in ('base', 'lost'):
            continue
        if ps.x is None or ps.y is None:
            continue
        if is_visible_to_enemy(sess, ps.x, ps.y):
            visible.append(SquadronLight(id=ps.id, x=ps.x, y=ps.y))
    return PlayerObservation(visible_squadrons=visible)



def _distance_field_hex(
    sess: Session,
    goal: tuple,
    *,
    pass_islands: bool,
    ignore_id: Optional[str] = None,
    player_obs: Optional[PlayerObservation] = None,
    stop_range: int = 0,
    avoid_prev_pos: Optional[tuple] = None,
    consider_occupied: bool = False,
):
    """Build a BFS wavefront distance field from goal over the hex grid.

    Returns a 2D list of distances (cells with INF are unreachable). If the
    map is empty, returns None.
    """
    gx, gy = goal
    W = len(sess.map[0]) if sess.map else 0
    H = len(sess.map)
    if W == 0 or H == 0:
        return None

    INF = 10 ** 9
    dist = [[INF for _ in range(W)] for __ in range(H)]

    def in_bounds(x, y):
        return 0 <= x < W and 0 <= y < H

    def passable(x, y):
        if not in_bounds(x, y):
            return False
        if not pass_islands and sess.map[y][x] != 0:
            return False
        if avoid_prev_pos is not None and (x, y) == avoid_prev_pos:
            return False
        if consider_occupied and _is_occupied(sess, x, y, ignore_id=ignore_id, player_obs=player_obs):
            return False
        return True

    q = deque()
    # Seeds: goal within stop_range; for exact stop, only the goal itself
    R = max(0, int(stop_range))
    for y in range(max(0, gy - (R + 2)), min(H, gy + (R + 3))):
        for x in range(max(0, gx - (R + 2)), min(W, gx + (R + 3))):
            if _hex_distance(x, y, gx, gy) <= R and passable(x, y):
                dist[y][x] = 0
                q.append((x, y))

    if not q:
        # If goal seed cells are not passable (e.g., occupied), still allow building distances but unreachable will remain INF
        if passable(gx, gy):
            dist[gy][gx] = 0
            q.append((gx, gy))
        else:
            return dist

    # Proper BFS to fill the distance field
    while q:
        cx, cy = q.popleft()
        cd = dist[cy][cx]
        for nx, ny in _offset_neighbors(cx, cy):
            if not passable(nx, ny):
                continue
            nd = cd + 1
            if dist[ny][nx] > nd:
                dist[ny][nx] = nd
                q.append((nx, ny))
    return dist


def _gradient_full_path(
    sess: Session,
    start: tuple,
    goal: tuple,
    *,
    pass_islands: bool,
    stop_range: int = 0,
    max_steps: int = 5000,
):
    """Construct full gradient-descent path from start to goal using a wavefront distance field.
    Returns list of (x,y) including start and final cell (distance 0 or no-progress).
    """
    dist = _distance_field_hex(
        sess,
        goal,
        pass_islands=pass_islands,
        ignore_id=None,
        player_obs=None,
        stop_range=stop_range,
        avoid_prev_pos=None,
        consider_occupied=False,
    )
    if dist is None:
        return [start]
    W = len(sess.map[0]) if sess.map else 0
    H = len(sess.map)
    x, y = start
    path = [start]
    steps = 0
    INF = 10 ** 9
    while steps < max_steps:
        if not (0 <= x < W and 0 <= y < H):
            break
        dcur = dist[y][x]
        if dcur <= max(0, stop_range) or dcur >= INF:
            break
        nbrs = []
        for nx, ny in _offset_neighbors(x, y):
            if 0 <= nx < W and 0 <= ny < H:
                nbrs.append((dist[ny][nx], nx, ny))
        nbrs.sort(key=lambda t: t[0])
        moved = False
        for dv, nx, ny in nbrs:
            if dv < dcur:
                x, y = nx, ny
                path.append((x, y))
                moved = True
                break
        if not moved:
            break
        steps += 1
    return path


def _find_path_hex(
    sess: Session,
    start: tuple,
    goal: tuple,
    *,
    pass_islands: bool,
    ignore_id: Optional[str] = None,
    player_obs=None,
    stop_range: int = 0,
    avoid_prev_pos: Optional[tuple] = None,
    max_expand: int = 4000,
):
    """A* pathfinding on hex grid (offset coords), returns list of (x,y) including start->end.

    - Treat islands as impassable when pass_islands is False; otherwise ignore terrain.
    - Treat occupied cells as impassable, except the one matching ignore_id.
    - Goal is any cell with hex_distance(cell, goal) <= stop_range; if stop_range==0 it's exact match.
    - avoid_prev_pos: optional single cell to exclude as first step (helps avoid immediate backtracking across turns).
    """
    sx, sy = start
    gx, gy = goal
    W = len(sess.map[0]) if sess.map else 0
    H = len(sess.map)
    if W == 0 or H == 0:
        return None

    def in_bounds(x, y):
        return 0 <= x < W and 0 <= y < H

    def passable(x, y):
        if not in_bounds(x, y):
            return False
        if not pass_islands and sess.map[y][x] != 0:
            return False
        if avoid_prev_pos is not None and (x, y) == avoid_prev_pos:
            return False
        if _is_occupied(sess, x, y, ignore_id=ignore_id, player_obs=player_obs):
            return False
        return True

    start_ok = passable(sx, sy)
    if not start_ok:
        # If starting on non-passable (shouldn't happen), return None
        return None

    # Early exit if already at goal within stop_range
    if _hex_distance(sx, sy, gx, gy) <= max(0, stop_range):
        return [start]

    open_heap = []  # (f, g, (x,y))
    heapq.heappush(open_heap, (0 + _hex_distance(sx, sy, gx, gy), 0, (sx, sy)))
    came_from = { (sx, sy): None }
    g_score = { (sx, sy): 0 }
    closed = set()
    expands = 0

    while open_heap and expands < max_expand:
        f, g, (cx, cy) = heapq.heappop(open_heap)
        if (cx, cy) in closed:
            continue
        closed.add((cx, cy))
        expands += 1
        # goal test: within stop_range
        if _hex_distance(cx, cy, gx, gy) <= max(0, stop_range):
            # Reconstruct path
            path = [(cx, cy)]
            cur = (cx, cy)
            while came_from[cur] is not None:
                cur = came_from[cur]
                path.append(cur)
            path.reverse()
            return path
        # expand neighbors
        for nx, ny in _offset_neighbors(cx, cy):
            if not passable(nx, ny):
                continue
            tentative = g + 1
            if tentative < g_score.get((nx, ny), 1e9):
                g_score[(nx, ny)] = tentative
                came_from[(nx, ny)] = (cx, cy)
                h = _hex_distance(nx, ny, gx, gy)
                heapq.heappush(open_heap, (tentative + h, tentative, (nx, ny)))

    return None


def _is_occupied(sess: Session, x: int, y: int, ignore_id: Optional[str] = None, player_obs: Optional[PlayerObservation] = None) -> bool:
    # enemy carrier
    if sess.enemy_state.carrier.x == x and sess.enemy_state.carrier.y == y:
        return True
    # player carrier
    if sess.player_state.carrier.x == x and sess.player_state.carrier.y == y:
        return True
    # enemy squadrons
    for s in sess.enemy_state.squadrons:
        if s.id == ignore_id:
            continue
        if s.x == x and s.y == y and s.state not in ("base", "lost"):
            return True
    # visible player squadrons (avoid)
    if player_obs and player_obs.visible_squadrons:
        for ps in player_obs.visible_squadrons:
            if ps.x == x and ps.y == y:
                return True
    return False


def _find_free_adjacent(sess: Session, cx: int, cy: int, prefer_away_from: Optional[Position] = None):
    candidates = []
    for nx, ny in _offset_neighbors(cx, cy):
        if ny < 0 or nx < 0 or ny >= len(sess.map) or nx >= len(sess.map[0]):
            continue
        if sess.map[ny][nx] != 0:
            continue
        if _is_occupied(sess, nx, ny):
            continue
        candidates.append((nx, ny))
    if not candidates:
        return None
    if prefer_away_from is not None:
        # sort by descending distance from prefer_away_from
        candidates.sort(key=lambda p: _hex_distance(prefer_away_from.x, prefer_away_from.y, p[0], p[1]), reverse=True)
    nx, ny = candidates[0]
    return Position(x=nx, y=ny)


def _scaled_damage(attacker_hp: Optional[int], base: int) -> int:
    hp = attacker_hp if attacker_hp is not None else SQUAD_MAX_HP
    scale = max(0.0, min(1.0, hp / float(SQUAD_MAX_HP)))
    variance = round(base * 0.2)
    raw = base + (0 if variance == 0 else random.randint(-variance, variance))
    return max(0, round(raw * scale))


def _scaled_aa(player_hp: Optional[int], base: int) -> int:
    hp = player_hp if player_hp is not None else CARRIER_MAX_HP
    scale = max(0.0, min(1.0, hp / float(CARRIER_MAX_HP)))
    variance = round(base * 0.2)
    raw = base + (0 if variance == 0 else random.randint(-variance, variance))
    return max(0, round(raw * scale))


def _step_on_grid_towards(
    sess: Session,
    obj: dict,
    target: dict,
    step_max: int,
    stop_range: int = 0,
    ignore_id: Optional[str] = None,
    player_obs: Optional[PlayerObservation] = None,
    track_path: Optional[list] = None,
    track_range: int = 0,
    debug_trace: Optional[list] = None,
    avoid_prev_pos: Optional[tuple] = None,
):
    # First compute wavefront distance field from target (goal) and follow gradient
    dist = _distance_field_hex(
        sess,
        (target['x'], target['y']),
        pass_islands=bool(obj.get('pass_islands')),
        ignore_id=ignore_id,
        player_obs=player_obs,
        stop_range=stop_range,
        avoid_prev_pos=None,
    )
    # If no distance field can be built, consider unreachable and stop
    if dist is None:
        if debug_trace is not None:
            debug_trace.append({"reason": "unreachable_no_field"})
        return
    if dist is not None:
        W = len(sess.map[0]) if sess.map else 0
        H = len(sess.map)
        INF = 10**9
        steps = 0
        while steps < step_max:
            cx, cy = obj['x'], obj['y']
            if not (0 <= cx < W and 0 <= cy < H):
                break
            dcur = dist[cy][cx] if dist else INF
            if dcur == 0 or dcur >= INF:
                break
            # choose neighbor with minimal distance < dcur
            candidates = []
            for nx, ny in _offset_neighbors(cx, cy):
                if 0 <= nx < W and 0 <= ny < H:
                    candidates.append((dist[ny][nx], nx, ny))
            candidates.sort(key=lambda t: t[0])
            moved = False
            for dv, nx, ny in candidates:
                if dv < dcur:
                    prev_x, prev_y = obj['x'], obj['y']
                    obj['x'], obj['y'] = nx, ny
                    if track_path is not None and track_range > 0:
                        track_path.append({'x': nx, 'y': ny, 'range': track_range})
                    if debug_trace is not None:
                        debug_trace.append({"from": [prev_x, prev_y], "to": [nx, ny], "solver": "wavefront"})
                    moved = True
                    steps += 1
                    break
            if not moved:
                break
        # Always return after wavefront attempt; do not use greedy fallback
        return

    # Greedy fallback when A* fails (should be rare):
    last_x: Optional[int] = None
    last_y: Optional[int] = None
    visited = set()
    for _ in range(step_max):
        dist = _hex_distance(obj['x'], obj['y'], target['x'], target['y'])
        if dist <= stop_range:
            break
        # next along hex line: approximate by choosing neighbor minimizing distance
        curx, cury = obj['x'], obj['y']
        visited.add((curx, cury))
        nbrs = [(nx, ny) for nx, ny in _offset_neighbors(curx, cury)]
        # prefer those that are closer to target
        nbrs.sort(key=lambda p: _hex_distance(p[0], p[1], target['x'], target['y']))
        moved = False
        # 1) strictly better moves, skipping occupied and (for carriers) islands
        for nx, ny in nbrs:
            if ny < 0 or nx < 0 or ny >= len(sess.map) or nx >= len(sess.map[0]):
                continue
            if not obj.get('pass_islands'):
                if sess.map[ny][nx] != 0:
                    continue
            if (nx, ny) in visited or (avoid_prev_pos is not None and (nx, ny) == avoid_prev_pos):
                continue
            if _is_occupied(sess, nx, ny, ignore_id=ignore_id, player_obs=player_obs):
                continue
            if _hex_distance(nx, ny, target['x'], target['y']) < dist:
                # avoid immediate backtrack
                if last_x is not None and nx == last_x and last_y is not None and ny == last_y:
                    continue
                prev_x, prev_y = obj['x'], obj['y']
                obj['x'], obj['y'] = nx, ny
                if track_path is not None and track_range > 0:
                    track_path.append({'x': nx, 'y': ny, 'range': track_range})
                if debug_trace is not None:
                    debug_trace.append({"from": [prev_x, prev_y], "to": [nx, ny], "terrain": ("island" if sess.map[ny][nx] != 0 else "sea")})
                moved = True
                last_x, last_y = prev_x, prev_y
                break
        if moved:
            continue
        # 2) equal-distance sidestep, but avoid immediate backtracking
        for nx, ny in nbrs:
            if ny < 0 or nx < 0 or ny >= len(sess.map) or nx >= len(sess.map[0]):
                continue
            if not obj.get('pass_islands'):
                if sess.map[ny][nx] != 0:
                    continue
            if (nx, ny) in visited or (avoid_prev_pos is not None and (nx, ny) == avoid_prev_pos):
                continue
            if _is_occupied(sess, nx, ny, ignore_id=ignore_id, player_obs=player_obs):
                continue
            if _hex_distance(nx, ny, target['x'], target['y']) == dist:
                # don't step back to the tile we just came from in previous step
                if last_x is not None and nx == last_x and last_y is not None and ny == last_y:
                    continue
                prev_x, prev_y = obj['x'], obj['y']
                obj['x'], obj['y'] = nx, ny
                if track_path is not None and track_range > 0:
                    track_path.append({'x': nx, 'y': ny, 'range': track_range})
                if debug_trace is not None:
                    debug_trace.append({"from": [prev_x, prev_y], "to": [nx, ny], "terrain": ("island" if sess.map[ny][nx] != 0 else "sea")})
                moved = True
                last_x, last_y = prev_x, prev_y
                break
        if moved:
            continue
        # 3) last-resort: allow minimal distance increase to route around obstacles
        best = None
        best_d = None
        for nx, ny in nbrs:
            if ny < 0 or nx < 0 or ny >= len(sess.map) or nx >= len(sess.map[0]):
                continue
            if not obj.get('pass_islands'):
                if sess.map[ny][nx] != 0:
                    continue
            if (nx, ny) in visited or (avoid_prev_pos is not None and (nx, ny) == avoid_prev_pos):
                continue
            if _is_occupied(sess, nx, ny, ignore_id=ignore_id, player_obs=player_obs):
                continue
            nd = _hex_distance(nx, ny, target['x'], target['y'])
            # avoid immediate backtrack
            if last_x is not None and nx == last_x and last_y is not None and ny == last_y:
                continue
            if best_d is None or nd < best_d:
                best_d = nd
                best = (nx, ny)
        if best is not None and best_d is not None and best_d <= dist + 2:
            prev_x, prev_y = obj['x'], obj['y']
            obj['x'], obj['y'] = best
            if track_path is not None and track_range > 0:
                track_path.append({'x': best[0], 'y': best[1], 'range': track_range})
            if debug_trace is not None:
                by, bx = best[1], best[0]
                debug_trace.append({"from": [prev_x, prev_y], "to": [best[0], best[1]], "mode": "fallback", "terrain": ("island" if sess.map[by][bx] != 0 else "sea")})
            moved = True
            last_x, last_y = prev_x, prev_y
            continue
        for nx, ny in nbrs:
            if ny < 0 or nx < 0 or ny >= len(sess.map) or nx >= len(sess.map[0]):
                continue
            # aircraft can pass islands; carriers cannot (we treat based on a flag on obj)
            if _is_occupied(sess, nx, ny, ignore_id=ignore_id, player_obs=player_obs):
                continue
            # forbid islands for carriers (obj.get('pass_islands') falsy)
            if not obj.get('pass_islands'):
                if ny < 0 or nx < 0 or ny >= len(sess.map) or nx >= len(sess.map[0]):
                    continue
                if sess.map[ny][nx] != 0:
                    continue
            # already tried better and equal moves; nothing else reduces distance, so stop
        if not moved:
            if debug_trace is not None:
                debug_trace.append({"from": [obj['x'], obj['y']], "reason": "blocked_or_not_improving"})
            break


def _progress_enemy_squadrons(sess: Session, player_observation: PlayerObservation, audit_events: Optional[list] = None) -> int:
    total_player_damage = 0
    ec = sess.enemy_state.carrier
    # shallow copy for safe iteration
    for sq in list(sess.enemy_state.squadrons):
        prev_pos = (sq.x, sq.y) if sq.x is not None and sq.y is not None else None
        if sq.state == "outbound":
            # If player carrier visible (server-calculated), move toward it and possibly attack
            server_vis = _enemy_sees_player_carrier(sess)
            if server_vis is not None:
                # move toward player carrier with stopRange 1
                obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                tgt = {'x': server_vis.x, 'y': server_vis.y}
                trace = []
                avoid_prev = sess.last_pos_enemy_sq.get(sq.id) if prev_pos is not None else None
                _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.x, sq.y = obj['x'], obj['y']
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                if _hex_distance(sq.x, sq.y, tgt['x'], tgt['y']) <= 1:
                    dmg = _scaled_damage(getattr(sq, 'hp', SQUAD_MAX_HP), 25)
                    total_player_damage += dmg
                    # AA against squadron
                    aa = _scaled_aa(sess.player_state.carrier.hp if (sess.player_state and sess.player_state.carrier and sess.player_state.carrier.hp is not None) else CARRIER_MAX_HP, 20)
                    sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                    if audit_events is not None:
                        audit_events.append({"type": "attack", "side": "enemy", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                    if sq.hp <= 0:
                        sq.state = "lost"
                        sq.x = None
                        sq.y = None
                        sq.target = None
                    else:
                        sq.state = "returning"
                else:
                    sq.state = "engaging"
            else:
                # not visible: continue toward target (if any)
                if sq.target is not None:
                    obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                    tgt = {'x': sq.target.x, 'y': sq.target.y}
                    trace = []
                    avoid_prev = sess.last_pos_enemy_sq.get(sq.id) if prev_pos is not None else None
                    _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                    sq.x, sq.y = obj['x'], obj['y']
                    if audit_events is not None:
                        audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                    if sq.x == sq.target.x and sq.y == sq.target.y:
                        sq.state = "returning"
        elif sq.state == "engaging":
            server_vis = _enemy_sees_player_carrier(sess)
            if server_vis is not None:
                obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                tgt = {'x': server_vis.x, 'y': server_vis.y}
                trace = []
                avoid_prev = sess.last_pos_enemy_sq.get(sq.id) if prev_pos is not None else None
                _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.x, sq.y = obj['x'], obj['y']
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                if _hex_distance(sq.x, sq.y, tgt['x'], tgt['y']) <= 1:
                    dmg = _scaled_damage(getattr(sq, 'hp', SQUAD_MAX_HP), 25)
                    total_player_damage += dmg
                    aa = _scaled_aa(sess.player_state.carrier.hp if (sess.player_state and sess.player_state.carrier and sess.player_state.carrier.hp is not None) else CARRIER_MAX_HP, 20)
                    sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                    if audit_events is not None:
                        audit_events.append({"type": "attack", "side": "enemy", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                    if sq.hp <= 0:
                        sq.state = "lost"
                        sq.x = None
                        sq.y = None
                        sq.target = None
                    else:
                        sq.state = "returning"
        elif sq.state == "returning":
            obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
            tgt = {'x': ec.x, 'y': ec.y}
            trace = []
            avoid_prev = sess.last_pos_enemy_sq.get(sq.id) if prev_pos is not None else None
            _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
            sq.x, sq.y = obj['x'], obj['y']
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
            if _hex_distance(sq.x, sq.y, ec.x, ec.y) <= 1:
                sq.state = "base"
                sq.x = None
                sq.y = None
                sq.target = None
        # update last pos for next turn avoidance
        if prev_pos is not None:
            sess.last_pos_enemy_sq[sq.id] = prev_pos
    return total_player_damage


def _progress_player_squadrons(sess: Session, path_sweep: Optional[list] = None, audit_events: Optional[list] = None) -> int:
    total_enemy_damage = 0
    pc = sess.player_state.carrier
    ec = sess.enemy_state.carrier
    for sq in list(sess.player_state.squadrons):
        prev_pos = (sq.x, sq.y) if sq.x is not None and sq.y is not None else None
        if sq.state == "outbound":
            # If enemy carrier within vision, move to engage
            if _hex_distance(sq.x, sq.y, ec.x, ec.y) <= getattr(sq, 'vision', VISION_SQUADRON) or getattr(sq, 'vision', VISION_SQUADRON) >= _hex_distance(sq.x, sq.y, ec.x, ec.y):
                obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                tgt = {'x': ec.x, 'y': ec.y}
                trace = []
                avoid_prev = sess.last_pos_player_sq.get(sq.id) if prev_pos is not None else None
                _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, track_path=path_sweep, track_range=getattr(sq, 'vision', VISION_SQUADRON) or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.x, sq.y = obj['x'], obj['y']
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                if _hex_distance(sq.x, sq.y, ec.x, ec.y) <= 1:
                    dmg = _scaled_damage(getattr(sq, 'hp', SQUAD_MAX_HP), 25)
                    total_enemy_damage += dmg
                    # enemy AA
                    aa = _scaled_aa(getattr(ec, 'hp', CARRIER_MAX_HP), 20)
                    sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                    if audit_events is not None:
                        audit_events.append({"type": "attack", "side": "player", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                    if sq.hp <= 0:
                        sq.state = "lost"; sq.x = None; sq.y = None; sq.target = None
                    else:
                        sq.state = "returning"
                else:
                    sq.state = "engaging"
            else:
                if sq.target is not None:
                    obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                    tgt = {'x': sq.target.x, 'y': sq.target.y}
                    trace = []
                    avoid_prev = sess.last_pos_player_sq.get(sq.id) if prev_pos is not None else None
                    _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, ignore_id=sq.id, track_path=path_sweep, track_range=getattr(sq, 'vision', VISION_SQUADRON) or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
                    sq.x, sq.y = obj['x'], obj['y']
                    if audit_events is not None:
                        audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                    if sq.x == sq.target.x and sq.y == sq.target.y:
                        sq.state = "returning"
        elif sq.state == "engaging":
            obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
            tgt = {'x': ec.x, 'y': ec.y}
            trace = []
            avoid_prev = sess.last_pos_player_sq.get(sq.id) if prev_pos is not None else None
            _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, track_path=path_sweep, track_range=getattr(sq, 'vision', VISION_SQUADRON) or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
            sq.x, sq.y = obj['x'], obj['y']
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
            if _hex_distance(sq.x, sq.y, ec.x, ec.y) <= 1:
                dmg = _scaled_damage(getattr(sq, 'hp', SQUAD_MAX_HP), 25)
                total_enemy_damage += dmg
                aa = _scaled_aa(getattr(ec, 'hp', CARRIER_MAX_HP), 20)
                sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                if audit_events is not None:
                    audit_events.append({"type": "attack", "side": "player", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                if sq.hp <= 0:
                    sq.state = "lost"; sq.x = None; sq.y = None; sq.target = None
                else:
                    sq.state = "returning"
        elif sq.state == "returning":
            obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
            tgt = {'x': pc.x, 'y': pc.y}
            trace = []
            avoid_prev = sess.last_pos_player_sq.get(sq.id) if prev_pos is not None else None
            _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, track_path=path_sweep, track_range=getattr(sq, 'vision', VISION_SQUADRON) or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
            sq.x, sq.y = obj['x'], obj['y']
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
            if _hex_distance(sq.x, sq.y, pc.x, pc.y) <= 1:
                sq.state = "base"; sq.x = None; sq.y = None; sq.target = None
        # update last pos for next turn avoidance
        if prev_pos is not None:
            sess.last_pos_player_sq[sq.id] = prev_pos
    return total_enemy_damage


def _apply_player_orders(sess: Session, req: SessionStepRequest, path_sweep: Optional[list] = None, audit_events: Optional[list] = None):
    orders = req.player_orders
    pc = sess.player_state.carrier
    if orders:
        # Carrier move
        if orders.carrier_target is not None:
            prev_pos_car = (pc.x, pc.y)
            obj = {'x': pc.x, 'y': pc.y, 'pass_islands': False}
            tgt = {'x': orders.carrier_target.x, 'y': orders.carrier_target.y}
            trace = []
            # Precompute full planned path for logging
            planned_path = _gradient_full_path(
                sess,
                (prev_pos_car[0], prev_pos_car[1]),
                (tgt['x'], tgt['y']),
                pass_islands=False,
                stop_range=0,
            )
            _step_on_grid_towards(sess, obj, tgt, getattr(pc, 'speed', 2) or 2, stop_range=0, track_path=path_sweep, track_range=getattr(pc, 'vision', VISION_CARRIER) or VISION_CARRIER, debug_trace=trace, avoid_prev_pos=sess.last_pos_player_carrier)
            pc.x, pc.y = obj['x'], obj['y']
            sess.last_pos_player_carrier = prev_pos_car
            if audit_events is not None:
                audit_events.append({
                    "type": "move", "side": "player", "unit": "carrier",
                    "from": [prev_pos_car[0], prev_pos_car[1]],
                    "target": [tgt['x'], tgt['y']],
                    "planned_path": planned_path,
                    "steps_taken": len([e for e in trace if e.get('to')]),
                    "trace": trace,
                })
            # Write move instruction to .map (from previous position to ordered target)
            try:
                ordered_to = (tgt['x'], tgt['y'])
                if sess.last_logged_player_target != ordered_to:
                    maplog_write(
                        sess.id,
                        {
                            "type": "move",
                            "side": "player",
                            "from": [prev_pos_car[0], prev_pos_car[1]],
                            "to": [tgt['x'], tgt['y']],
                        },
                    )
                    sess.last_logged_player_target = ordered_to
            except Exception:
                pass
        # Launch one squadron
        if orders.launch_target is not None:
            # find base-available squadron
            sq = next((s for s in sess.player_state.squadrons if s.state == 'base' and (s.hp or SQUAD_MAX_HP) > 0), None)
            if sq is not None:
                # clamp range and spawn near carrier
                tgt = Position(x=orders.launch_target.x, y=orders.launch_target.y)
                spawn = _find_free_adjacent(sess, pc.x, pc.y, prefer_away_from=tgt)
                if spawn is not None:
                    if audit_events is not None:
                        audit_events.append({"type": "launch", "side": "player", "unit": "squadron", "id": sq.id, "spawn": [spawn.x, spawn.y], "target": [tgt.x, tgt.y]})
                    sq.x, sq.y = spawn.x, spawn.y
                    sq.target = tgt
                    sq.state = 'outbound'


def _visibility_key(x: int, y: int) -> str:
    return f"{x},{y}"


def _mark_visibility_circle(sess: Session, vis: set, cx: int, cy: int, rng: int):
    R = max(0, int(rng))
    H = len(sess.map)
    W = len(sess.map[0]) if H > 0 else 0
    for y in range(max(0, cy - (R + 2)), min(H, cy + (R + 3))):
        for x in range(max(0, cx - (R + 2)), min(W, cx + (R + 3))):
            if _hex_distance(x, y, cx, cy) <= R:
                vis.add(_visibility_key(x, y))


def _compute_player_visibility(sess: Session, path_sweep: Optional[list]) -> set:
    vis: set = set()
    pc = sess.player_state.carrier
    _mark_visibility_circle(sess, vis, pc.x, pc.y, getattr(pc, 'vision', VISION_CARRIER) or VISION_CARRIER)
    for sq in sess.player_state.squadrons:
        if sq.state not in ('base', 'lost') and sq.x is not None and sq.y is not None:
            _mark_visibility_circle(sess, vis, sq.x, sq.y, getattr(sq, 'vision', VISION_SQUADRON) or VISION_SQUADRON)
    # path sweep
    for step in (path_sweep or []):
        _mark_visibility_circle(sess, vis, step['x'], step['y'], int(step.get('range', 0)))
    return vis


def _enemy_sees_player_carrier(sess: Session) -> Optional[Position]:
    """Return Position(x,y) if any enemy unit can see the player carrier, else None.

    Uses enemy carrier vision and active enemy squadrons' vision ranges.
    """
    pc = sess.player_state.carrier
    ec = sess.enemy_state.carrier
    # enemy carrier eyesight
    if _hex_distance(ec.x, ec.y, pc.x, pc.y) <= getattr(ec, 'vision', VISION_CARRIER):
        return Position(x=pc.x, y=pc.y)
    # enemy squadrons
    for sq in sess.enemy_state.squadrons:
        if sq.state in ('base', 'lost') or sq.x is None or sq.y is None:
            continue
        if _hex_distance(sq.x, sq.y, pc.x, pc.y) <= getattr(sq, 'vision', VISION_SQUADRON):
            return Position(x=pc.x, y=pc.y)
    return None


def _validate_sea_connectivity(sess: Session):
    # BFS-like reachability over sea using distance field from an arbitrary sea tile
    H = len(sess.map)
    W = len(sess.map[0]) if H > 0 else 0
    sea = []
    for y in range(H):
        for x in range(W):
            if sess.map[y][x] == 0:
                sea.append((x, y))
    sea_total = len(sea)
    if sea_total == 0:
        return True, 0, 0
    sx, sy = sea[0]
    dist = _distance_field_hex(sess, (sx, sy), pass_islands=False, ignore_id=None, player_obs=None, stop_range=0, avoid_prev_pos=None)
    if dist is None:
        return False, sea_total, 0
    INF = 10 ** 8
    reached = 0
    for x, y in sea:
        if 0 <= y < H and 0 <= x < W and dist[y][x] < INF:
            reached += 1
    return reached == sea_total, sea_total, reached


# ==== Server-side map generation helpers ====
def _generate_connected_map(width: int, height: int, *, blobs: int = 10, rng: Optional[random.Random] = None):
    r = rng or random.Random()
    for _attempt in range(60):
        m = [[0 for _ in range(width)] for __ in range(height)]
        for _ in range(blobs):
            cx = r.randint(2, max(2, width - 3))
            cy = r.randint(2, max(2, height - 3))
            rad = r.randint(1, 3)
            for dy in range(-rad, rad + 1):
                for dx in range(-rad, rad + 1):
                    if dx * dx + dy * dy <= rad * rad:
                        x = max(0, min(width - 1, cx + dx))
                        y = max(0, min(height - 1, cy + dy))
                        m[y][x] = 1
        # quick connectivity check using a temp session
        tmp_sess = Session(
            id="tmp",
            map=m,
            enemy_state=EnemyState(carrier=CarrierState(id="E", x=0, y=0), squadrons=[]),
            player_state=EnemyState(carrier=CarrierState(id="P", x=0, y=0), squadrons=[]),
            enemy_memory=EnemyMemory(enemy_ai=EnemyAIState()),
        )
        ok, sea_total, sea_reached = _validate_sea_connectivity(tmp_sess)
        if ok:
            return m
    return m


def _carve_sea(m: list, cx: int, cy: int, r: int):
    H = len(m)
    W = len(m[0]) if H > 0 else 0
    for y in range(H):
        for x in range(W):
            if _hex_distance(x, y, cx, cy) <= r:
                m[y][x] = 0


def _game_status(sess: Session):
    # Determine game over conditions
    if sess.enemy_state.carrier.hp <= 0:
        return {'over': True, 'result': 'win', 'message': '敵空母撃沈！勝利', 'turn': sess.turn}
    if sess.player_state.carrier.hp <= 0:
        return {'over': True, 'result': 'lose', 'message': '我が空母撃沈…敗北', 'turn': sess.turn}
    if sess.turn >= sess.max_turns:
        pc = sess.player_state.carrier.hp
        ec = sess.enemy_state.carrier.hp
        if pc > ec:
            return {'over': True, 'result': 'win', 'message': '終戦判定：優勢で勝利', 'turn': sess.turn}
        if pc < ec:
            return {'over': True, 'result': 'lose', 'message': '終戦判定：劣勢で敗北', 'turn': sess.turn}
        return {'over': True, 'result': 'draw', 'message': '終戦判定：引き分け', 'turn': sess.turn}
    return {'over': False, 'turn': sess.turn}


def _update_player_intel(sess: Session, turn_visible: set) -> PlayerIntel:
    # Carrier intel
    ec = sess.enemy_state.carrier
    key = _visibility_key(ec.x, ec.y)
    if key in turn_visible:
        sess.player_intel_carrier = IntelMarker(seen=True, x=ec.x, y=ec.y, ttl=3)
    else:
        if sess.player_intel_carrier and sess.player_intel_carrier.ttl > 0:
            ttl = max(0, sess.player_intel_carrier.ttl - 1)
            sess.player_intel_carrier = IntelMarker(seen=ttl > 0, x=sess.player_intel_carrier.x, y=sess.player_intel_carrier.y, ttl=ttl)

    # Squadrons intel
    current_ids = set()
    for s in sess.enemy_state.squadrons:
        if s.state in ('base', 'lost') or s.x is None or s.y is None:
            continue
        current_ids.add(s.id)
        k = _visibility_key(s.x, s.y)
        if k in turn_visible:
            sess.player_intel_squadrons[s.id] = IntelMarker(seen=True, x=s.x, y=s.y, ttl=3)
        else:
            m = sess.player_intel_squadrons.get(s.id)
            if m and m.ttl > 0:
                ttl = max(0, m.ttl - 1)
                sess.player_intel_squadrons[s.id] = IntelMarker(seen=ttl > 0, x=m.x, y=m.y, ttl=ttl)
    # Decay intel for squadrons that no longer exist in state
    for sid, m in list(sess.player_intel_squadrons.items()):
        if sid not in current_ids and m.ttl > 0:
            ttl = max(0, m.ttl - 1)
            sess.player_intel_squadrons[sid] = IntelMarker(seen=ttl > 0, x=m.x, y=m.y, ttl=ttl)

    # Build response (only include entries with ttl>0 or currently seen)
    sq_list: list[SquadronIntel] = []
    for sid, m in sess.player_intel_squadrons.items():
        if m.ttl > 0 or m.seen:
            sq_list.append(SquadronIntel(id=sid, marker=m))
    return PlayerIntel(carrier=sess.player_intel_carrier, squadrons=sq_list)
