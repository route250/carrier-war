import uuid
import random
import heapq
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

from server.schemas import (
    GameStatus,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStepRequest,
    SessionStepResponse,
    PlayerState,
    CarrierState,
    SquadronState,
    EnemyMemory,
    EnemyAIState,
    IntelMarker,
    PlanRequest,
    SquadronOrder,
    Position,
    StepEffects, TrackPos,
    PlayerIntel,
    SquadronIntel,
    PlayerObservation,
    SquadronLight,
)
from server.services.ai import plan_orders
from server.utils.audit import audit_write, maplog_write


# ==== Server-side game helpers (enemy side only) ====
SQUAD_MAX_HP = 40
CARRIER_MAX_HP = 100
VISION_SQUADRON = 5
VISION_CARRIER = 4
SQUADRON_RANGE = 22


class Session:
    def __init__(self,
                 session_id: str,
                 map: list,
                 rand_seed: Optional[int] = None,
                 config: Optional[dict] = None,
               ):

        self.session_id = session_id
        self.turn: int = 1
        self.max_turns: int = 20
        self.map = map
        self.rand_seed: Optional[int] = rand_seed
        self.config: Optional[dict] = config

        enemy_carrier = CarrierState(id="E1", pos=Position(x=26, y=26), hp=CARRIER_MAX_HP, speed=2, vision=4)
        enemy_squadrons = [SquadronState(id=f"ESQ{i+1}", pos=Position.invalid(), state='base', hp=SQUAD_MAX_HP, speed=4, vision=3) for i in range(enemy_carrier.hangar)]
        self.enemy_state: PlayerState = PlayerState(carrier=enemy_carrier, squadrons=enemy_squadrons)

        player_carrier = CarrierState(id="C1", pos=Position(x=3, y=3), hp=CARRIER_MAX_HP, speed=2, vision=4)
        player_squadrons = [SquadronState(id=f"SQ{i+1}", pos=Position.invalid(), state='base', hp=SQUAD_MAX_HP, speed=4, vision=3) for i in range(player_carrier.hangar)]
        self.player_state: PlayerState = PlayerState(carrier=player_carrier, squadrons=player_squadrons)
        # Ensure starting positions are on sea (carve small sea around if necessary)
        _carve_sea(map, self.player_state.carrier.pos, 2)
        _carve_sea(map, self.enemy_state.carrier.pos, 2)
        self.enemy_memory: EnemyMemory = EnemyMemory(enemy_ai=EnemyAIState())
        # Separate AI state (server-internal) for enemy side; mirrored into enemy_memory for AI planning I/O
        self.enemy_ai_state: EnemyAIState = EnemyAIState()

        # Side intel memory (server-side, symmetric: what each side knows about the other)
        from server.schemas import SideIntel
        self.player_intel: SideIntel = SideIntel()
        self.enemy_intel: SideIntel = SideIntel()
        # Map log: remember last user-ordered target to avoid duplicate logs each turn
        self.last_logged_player_target: Optional[Position] = None
        # PlayerState.carrier_target holds the persistent server-authoritative carrier move target


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}

    def create(self, req: SessionCreateRequest) -> SessionCreateResponse:
        sid = str(uuid.uuid4())
        # Map: if not provided, generate a connected one
        width = 30
        height = 30

        # Regenerate if somehow disconnected after carving
        game_map = _generate_connected_map(width, height)
        sess = Session(
            session_id=sid,
            map=game_map,
            rand_seed=req.rand_seed,
            config=req.config.dict() if req.config else None,
        )
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
                    "player_state": sess.player_state.model_dump(),
                    "enemy_state": sess.enemy_state.model_dump(),
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

        # Update memory: if player carrier visible to enemy, set TTL=3
        player_visible_carrier = _enemy_sees_player_carrier(sess)
        if player_visible_carrier is not None:
            intel_marker = IntelMarker(seen=True, pos=player_visible_carrier, ttl=3)
            # symmetric enemy-side intel
            sess.enemy_intel.carrier = intel_marker
            # maintain AI/backcompat memory wrapper
            sess.enemy_memory.carrier_last_seen = intel_marker

        # Compute server-side view of which player squadrons are visible to enemy
        player_observation = _compute_visible_player_squadrons(sess)
        # Update symmetric enemy intel for player squadrons (TTL decay)
        try:
            current_ids = set()
            for ps in player_observation.visible_squadrons:
                current_ids.add(ps.id)
                sess.enemy_intel.squadrons[ps.id] = IntelMarker(seen=True, pos=ps.pos, ttl=3)
            # decay intel for squadrons not currently visible
            for sid, marker in list(sess.enemy_intel.squadrons.items()):
                if sid not in current_ids and marker.ttl > 0:
                    ttl = max(0, marker.ttl - 1)
                    sess.enemy_intel.squadrons[sid] = IntelMarker(seen=ttl > 0, pos=marker.pos, ttl=ttl)
        except Exception:
            pass

        # Build PlanRequest using session state
        # Mirror AI state into compat enemy_memory for plan interface
        try:
            sess.enemy_memory.enemy_ai = sess.enemy_ai_state
        except Exception:
            pass
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
            # update internal AI state from response
            try:
                if plan_resp.enemy_memory_out.enemy_ai is not None:
                    sess.enemy_ai_state = plan_resp.enemy_memory_out.enemy_ai
            except Exception:
                pass
            # mirror into symmetric enemy-intel store for consistency
            try:
                if plan_resp.enemy_memory_out.carrier_last_seen is not None:
                    sess.enemy_intel.carrier = plan_resp.enemy_memory_out.carrier_last_seen
            except Exception:
                pass
        logs = list(plan_resp.logs)
        # Apply enemy carrier position move if any
        if plan_resp.carrier_order and plan_resp.carrier_order.type == "move" and plan_resp.carrier_order.target is not None:
            # cross-turn backtrack memory for carrier
            sess.enemy_state.last_pos_carrier = sess.enemy_state.carrier.pos
            aud({
                "type": "move", "side": "enemy", "unit": "carrier",
                "from": [sess.enemy_state.carrier.pos.x, sess.enemy_state.carrier.pos.y],
                "to": [plan_resp.carrier_order.target.x, plan_resp.carrier_order.target.y],
            })
            sess.enemy_state.carrier.pos = plan_resp.carrier_order.target

        # Apply squadron orders (launch/return/engage)
        for od in (plan_resp.squadron_orders or []):
            sq = next((s for s in sess.enemy_state.squadrons if s.id == od.id), None)
            if not sq:
                continue
            if od.action == "launch" and od.target is not None and sq.state == "base" and (sq.hp or 0) > 0:
                tgt = Position(x=od.target.x, y=od.target.y)
                spawn = _find_free_adjacent(sess, sess.enemy_state.carrier.pos, prefer_away_from=tgt)
                if spawn:
                    aud({
                        "type": "launch", "side": "enemy", "unit": "squadron", "id": sq.id,
                        "spawn": [spawn.x, spawn.y], "target": [tgt.x, tgt.y]
                    })
                    sq.pos = spawn
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
        path_sweep: list[TrackPos] = []
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
        # Update and emit player intel (symmetric intel for player side)
        # keep previous intel snapshot for change detection
        prev_carrier = sess.player_intel.carrier
        prev_sq = dict(sess.player_intel.squadrons)
        player_intel = _update_player_intel(sess, turn_visible)
        # detection events
        if player_intel.carrier and (not prev_carrier or not prev_carrier.seen or prev_carrier.ttl <= 0):
            aud({"type": "detect", "side": "player", "unit": "enemy_carrier", "pos": [player_intel.carrier.pos.x, player_intel.carrier.pos.y]})
        for item in (player_intel.squadrons or []):
            prev = prev_sq.get(item.id)
            if item.marker and (not prev or not prev.seen or prev.ttl <= 0):
                aud({"type": "detect", "side": "player", "unit": "enemy_squadron", "id": item.id, "pos": [item.marker.pos.x, item.marker.pos.y]})

        # flush accumulated audit events
        for ev in audit_events:
            aud(ev)

        # Game status
        status = _game_status(sess)

        return SessionStepResponse(
            session_id = sess.session_id,
            turn=sess.turn,
            carrier_order=plan_resp.carrier_order,
            squadron_orders=plan_resp.squadron_orders,
            enemy_state=sess.enemy_state,
            player_state=sess.player_state,
            enemy_memory_out=sess.enemy_memory,
            effects=StepEffects(player_carrier_damage=max(0, int(dmg_to_player))),
            turn_visible=sorted(list(turn_visible)),
            game_status=status,
            player_intel=player_intel,
            enemy_intel=PlayerIntel(
                carrier=sess.enemy_intel.carrier,
                squadrons=[SquadronIntel(id=sid, marker=m) for sid, m in sess.enemy_intel.squadrons.items() if m.ttl > 0 or m.seen],
            ),
            logs=logs,
            metrics=plan_resp.metrics,
            request_id=plan_resp.request_id,
        )


store = SessionStore()




def _compute_visible_player_squadrons(sess: Session) -> PlayerObservation:
    """Return a list of SquadronLight for player squadrons that are visible to the enemy.

    Mirrors the client-side logic in static/main.js: filter out 'base'/'lost' squadrons
    and those without coords, then include those for which is_visible_to_enemy(...) is True.
    """
    visible = []
    for ps in sess.player_state.squadrons:
        if not ps.is_active():
            continue
        if sess.enemy_state.is_visible_to_player(ps):
            visible.append(SquadronLight(id=ps.id, pos=ps.pos))
    return PlayerObservation(visible_squadrons=visible)



def _distance_field_hex(
    sess: Session,
    goal: Position,
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

    W = len(sess.map[0]) if sess.map else 0
    H = len(sess.map)
    if W == 0 or H == 0:
        return None

    INF = 10 ** 9
    dist = [[INF for _ in range(W)] for __ in range(H)]

    def passable(pos:Position):
        if not pos.in_bounds(W,H):
            return False
        if not pass_islands and sess.map[pos.y][pos.x] != 0:
            return False
        if avoid_prev_pos is not None and pos == avoid_prev_pos:
            return False
        if consider_occupied and _is_occupied(sess, pos, ignore_id=ignore_id, player_obs=player_obs):
            return False
        return True

    gx, gy = goal.x, goal.y
    q: deque[Position] = deque()
    # Seeds: goal within stop_range; for exact stop, only the goal itself
    R = max(0, int(stop_range))
    for y in range(max(0, gy - (R + 2)), min(H, gy + (R + 3))):
        for x in range(max(0, gx - (R + 2)), min(W, gx + (R + 3))):
            xy = Position.new(x, y)
            if goal.hex_distance(xy) <= R and passable(xy):
                dist[y][x] = 0
                q.append(xy)

    if not q:
        # If goal seed cells are not passable (e.g., occupied), still allow building distances but unreachable will remain INF
        if passable(goal):
            dist[goal.y][goal.x] = 0
            q.append(goal)
        else:
            return dist

    # Proper BFS to fill the distance field
    while q:
        cp = q.popleft()
        cd = dist[cp.y][cp.x]
        for np in cp.offset_neighbors():
            if not passable(np):
                continue
            nd = cd + 1
            if dist[np.y][np.x] > nd:
                dist[np.y][np.x] = nd
                q.append(np)
    return dist


def _gradient_full_path(
    sess: Session,
    start: Position,
    goal: Position,
    *,
    pass_islands: bool,
    stop_range: int = 0,
    max_steps: int = 5000,
) -> list[Position]:
    """Construct full gradient-descent path from start to goal using a wavefront distance field.
    Returns list of positions including start and the last progressed cell (or start if blocked).
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
    x, y = start.x, start.y
    path: list[Position] = [start]
    steps = 0
    INF = 10 ** 9
    while steps < max_steps:
        if not (0 <= x < W and 0 <= y < H):
            break
        dcur = dist[y][x]
        if dcur <= max(0, stop_range) or dcur >= INF:
            break
        # Use current position (x,y) to examine neighbors
        cpos = Position(x=x, y=y)
        nbrs: list[tuple[int, Position]] = []
        for npos in cpos.offset_neighbors():
            if 0 <= npos.x < W and 0 <= npos.y < H:
                nbrs.append((dist[npos.y][npos.x], npos))
        nbrs.sort(key=lambda t: t[0])
        moved = False
        for dv, npos in nbrs:
            if dv < dcur:
                path.append(npos)
                x, y = npos.x, npos.y
                moved = True
                break
        if not moved:
            break
        steps += 1
    return path


def _find_path_hex(
    sess: Session,
    start: Position,
    goal: Position,
    *,
    pass_islands: bool,
    ignore_id: Optional[str] = None,
    player_obs=None,
    stop_range: int = 0,
    avoid_prev_pos: Optional[Position] = None,
    max_expand: int = 4000,
) -> list[Position]|None:
    """A* pathfinding on hex grid (offset coords), returns list of (x,y) including start->end.

    - Treat islands as impassable when pass_islands is False; otherwise ignore terrain.
    - Treat occupied cells as impassable, except the one matching ignore_id.
    - Goal is any cell with hex_distance(cell, goal) <= stop_range; if stop_range==0 it's exact match.
    - avoid_prev_pos: optional single cell to exclude as first step (helps avoid immediate backtracking across turns).
    """
    W = len(sess.map[0]) if sess.map else 0
    H = len(sess.map)
    if W == 0 or H == 0:
        return None

    def in_bounds(x, y):
        return 0 <= x < W and 0 <= y < H

    def passable(pos:Position):
        if not in_bounds(pos.x, pos.y):
            return False
        if not pass_islands and sess.map[pos.y][pos.x] != 0:
            return False
        if avoid_prev_pos is not None and pos == avoid_prev_pos:
            return False
        if _is_occupied(sess, pos, ignore_id=ignore_id, player_obs=player_obs):
            return False
        return True

    start_ok = passable(start)
    if not start_ok:
        # If starting on non-passable (shouldn't happen), return None
        return None

    # Early exit if already at goal within stop_range
    if start.hex_distance(goal) <= max(0, stop_range):
        return [start]

    open_heap:list[tuple[int,int,Position]] = []  # (f, g, (x,y))
    heapq.heappush(open_heap, (0 + start.hex_distance(goal), 0, start))
    came_from:dict[Position,Position|None] = { start: None }
    g_score:dict[Position,int] = { start: 0 }
    closed:set[Position] = set()
    expands = 0

    while open_heap and expands < max_expand:
        f, g, pos = heapq.heappop(open_heap)
        if pos in closed:
            continue
        closed.add(pos)
        expands += 1
        # goal test: within stop_range
        if pos.hex_distance(goal) <= max(0, stop_range):
            # Reconstruct path
            path:list[Position] = [pos]
            cur = pos
            while cur and came_from[cur] is not None:
                cur = came_from[cur]
                if cur:
                    path.append(cur)
            path.reverse()
            return path
        # expand neighbors
        for npos in pos.offset_neighbors():
            if not passable(npos):
                continue
            tentative = g + 1
            if tentative < g_score.get(npos, 1e9):
                g_score[npos] = tentative
                came_from[npos] = pos
                h = npos.hex_distance(goal)
                heapq.heappush(open_heap, (tentative + h, tentative, npos))

    return None


def _is_occupied(sess: Session, pos:Position, ignore_id: Optional[str] = None, player_obs: Optional[PlayerObservation] = None) -> bool:
    # enemy carrier
    if sess.enemy_state.carrier.pos == pos:
        return True
    # player carrier
    if sess.player_state.carrier.pos == pos:
        return True
    # enemy squadrons
    for s in sess.enemy_state.squadrons:
        if s.id == ignore_id:
            continue
        if s.pos == pos and s.state not in ("base", "lost"):
            return True
    # visible player squadrons (avoid)
    if player_obs and player_obs.visible_squadrons:
        for ps in player_obs.visible_squadrons:
            if ps.pos == pos:
                return True
    return False


def _find_free_adjacent(sess: Session, pos: Position, prefer_away_from: Optional[Position] = None):
    candidates: list[Position] = []
    for npos in pos.offset_neighbors():
        if npos.y < 0 or npos.x < 0 or npos.y >= len(sess.map) or npos.x >= len(sess.map[0]):
            continue
        if sess.map[npos.y][npos.x] != 0:
            continue
        if _is_occupied(sess, npos):
            continue
        candidates.append(npos)
    if not candidates:
        return None
    if prefer_away_from is not None:
        # sort by descending distance from prefer_away_from
        candidates.sort(key=lambda p: prefer_away_from.hex_distance(p), reverse=True)
    return candidates[0]


def _nearest_sea_tile(sess: Session, pos: Position) -> Optional[Position]:
    """Find nearest sea tile (map==0) to the given position using BFS over hex neighbors.
    Returns a Position or None if map is empty.
    """
    H = len(sess.map)
    W = len(sess.map[0]) if H > 0 else 0
    if W == 0 or H == 0:
        return None
    from collections import deque
    start = Position.new(pos)
    q: deque[Position] = deque([start])
    visited: set[Position] = { start }
    while q:
        p = q.popleft()
        if 0 <= p.x < W and 0 <= p.y < H:
            if sess.map[p.y][p.x] == 0:
                return p
            for n in p.offset_neighbors():
                if 0 <= n.x < W and 0 <= n.y < H and n not in visited:
                    visited.add(n)
                    q.append(n)
    return None



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
    obj: Position,
    is_pass_islands:bool,
    target: Position,
    step_max: int,
    stop_range: int = 0,
    ignore_id: Optional[str] = None,
    player_obs: Optional[PlayerObservation] = None,
    track_path: Optional[list[TrackPos]] = None,
    track_range: int = 0,
    debug_trace: Optional[list] = None,
    avoid_prev_pos: Optional[Position] = None,
)-> Position:

    # First compute wavefront distance field from target (goal) and follow gradient
    dist = _distance_field_hex(
        sess,
        target,
        pass_islands=is_pass_islands,
        ignore_id=ignore_id,
        player_obs=player_obs,
        stop_range=stop_range,
        avoid_prev_pos=None,
    )
    # If no distance field can be built, consider unreachable and stop
    if dist is None:
        if debug_trace is not None:
            debug_trace.append({"reason": "unreachable_no_field"})
        return obj
    if dist is not None:
        W = len(sess.map[0]) if sess.map else 0
        H = len(sess.map)
        INF = 10**9
        steps = 0
        cur = obj
        while steps < step_max:
            cx, cy = cur.x, cur.y
            if not cur.in_bounds(W,H):
                break
            dcur = dist[cy][cx] if dist else INF
            if dcur == 0 or dcur >= INF:
                break
            # choose neighbor with minimal distance < dcur
            candidates:list[tuple[int,Position]] = []
            for npos in cur.offset_neighbors():
                if npos.in_bounds(W,H):
                    candidates.append((dist[npos.y][npos.x], npos))
            candidates.sort(key=lambda t: t[0])
            moved = False
            for dv, npos in candidates:
                if dv < dcur:
                    prev_pos = cur
                    cur = npos
                    if track_path is not None and track_range > 0:
                        track_path.append( TrackPos(x=npos.x, y=npos.y, range=track_range))
                    if debug_trace is not None:
                        debug_trace.append({"from": [prev_pos.x, prev_pos.y], "to": [npos.x, npos.y], "solver": "wavefront"})
                    moved = True
                    steps += 1
                    break
            if not moved:
                break
        # Always return after wavefront attempt; do not use greedy fallback
        return cur

    # Greedy fallback when A* fails (should be rare):
    last_pos: Optional[Position] = None
    visited:set[Position] = set()
    for _ in range(step_max):
        dist = obj.hex_distance(target)
        if dist <= stop_range:
            break
        # next along hex line: approximate by choosing neighbor minimizing distance
        cur_pos = obj
        visited.add((cur_pos))
        nbrs = [p for p in cur_pos.offset_neighbors()]
        # prefer those that are closer to target
        nbrs.sort(key=lambda p: p.hex_distance(target))
        moved = False
        # 1) strictly better moves, skipping occupied and (for carriers) islands
        for npos in nbrs:
            if npos.y < 0 or npos.x < 0 or npos.y >= len(sess.map) or npos.x >= len(sess.map[0]):
                continue
            if not is_pass_islands:
                if sess.map[npos.y][npos.x] != 0:
                    continue
            if npos in visited or (avoid_prev_pos is not None and npos == avoid_prev_pos):
                continue
            if _is_occupied(sess, npos, ignore_id=ignore_id, player_obs=player_obs):
                continue
            if npos.hex_distance(target) < dist:
                # avoid immediate backtrack
                if last_pos is not None and npos ==last_pos:
                    continue
                prev_pos = obj
                obj = npos
                if track_path is not None and track_range > 0:
                    track_path.append(TrackPos(x=npos.x,y=npos.y,range=track_range))
                if debug_trace is not None:
                    debug_trace.append({"from": [prev_pos.x, prev_pos.y], "to": [npos.x, npos.y], "terrain": ("island" if sess.map[npos.y][npos.x] != 0 else "sea")})
                moved = True
                last_pos = prev_pos
                break
        if moved:
            continue
        # 2) equal-distance sidestep, but avoid immediate backtracking
        for npos in nbrs:
            if npos.y < 0 or npos.x < 0 or npos.y >= len(sess.map) or npos.x >= len(sess.map[0]):
                continue
            if not is_pass_islands:
                if sess.map[npos.y][npos.x] != 0:
                    continue
            if npos in visited or (avoid_prev_pos is not None and npos == avoid_prev_pos):
                continue
            if _is_occupied(sess, npos, ignore_id=ignore_id, player_obs=player_obs):
                continue
            if npos.hex_distance(target) == dist:
                # don't step back to the tile we just came from in previous step
                if last_pos is not None and npos == last_pos:
                    continue
                prev_pos = obj
                obj = npos
                if track_path is not None and track_range > 0:
                    track_path.append(TrackPos(x=npos.x,y=npos.y,range=track_range))
                if debug_trace is not None:
                    debug_trace.append({"from": [prev_pos.x, prev_pos.y], "to": [npos.x, npos.y], "terrain": ("island" if sess.map[npos.y][npos.x] != 0 else "sea")})
                moved = True
                last_pos = prev_pos
                break
        if moved:
            continue
        # 3) last-resort: allow minimal distance increase to route around obstacles
        best: Position|None = None
        best_d: int|None = None
        for npos in nbrs:
            if npos.y < 0 or npos.x < 0 or npos.y >= len(sess.map) or npos.x >= len(sess.map[0]):
                continue
            if not is_pass_islands:
                if sess.map[npos.y][npos.x] != 0:
                    continue
            if npos in visited or (avoid_prev_pos is not None and npos == avoid_prev_pos):
                continue
            if _is_occupied(sess, npos, ignore_id=ignore_id, player_obs=player_obs):
                continue
            nd = npos.hex_distance(target)
            # avoid immediate backtrack
            if last_pos is not None and npos == last_pos:
                continue
            if best_d is None or nd < best_d:
                best_d = nd
                best = npos
        if best is not None and best_d is not None and best_d <= dist + 2:
            prev_pos = obj
            obj = best
            if track_path is not None and track_range > 0:
                track_path.append(TrackPos(x=best.x,y=best.y,range=track_range))
            if debug_trace is not None:
                debug_trace.append({"from": [prev_pos.x, prev_pos.y], "to": [best.x, best.y], "mode": "fallback", "terrain": ("island" if sess.map[best.y][best.x] != 0 else "sea")})
            moved = True
            last_pos = prev_pos
            continue
        for npos in nbrs:
            if npos.y < 0 or npos.x < 0 or npos.y >= len(sess.map) or npos.x >= len(sess.map[0]):
                continue
            # aircraft can pass islands; carriers cannot (we treat based on a flag on obj)
            if _is_occupied(sess, npos, ignore_id=ignore_id, player_obs=player_obs):
                continue
            # forbid islands for carriers (obj.get('pass_islands') falsy)
            if not is_pass_islands:
                if npos.y < 0 or npos.x < 0 or npos.y >= len(sess.map) or npos.x >= len(sess.map[0]):
                    continue
                if sess.map[npos.y][npos.x] != 0:
                    continue
            # already tried better and equal moves; nothing else reduces distance, so stop
        if not moved:
            if debug_trace is not None:
                debug_trace.append({"from": [obj.x, obj.y], "reason": "blocked_or_not_improving"})
            break
    return obj


def _progress_enemy_squadrons(sess: Session, player_observation: PlayerObservation, audit_events: Optional[list] = None) -> int:
    total_player_damage = 0
    ec = sess.enemy_state.carrier
    is_pass_islands = True
    # shallow copy for safe iteration
    for sq in list(sess.enemy_state.squadrons):
        prev_pos: Position|None = sq.pos if sq.is_active() else None
        if sq.state == "outbound":
            # If player carrier visible (server-calculated), move toward it and possibly attack
            server_vis = _enemy_sees_player_carrier(sess)
            if server_vis is not None:
                # move toward player carrier with stopRange 1
                obj = sq.pos # {'x': sq.pos.x, 'y': sq.pos.y, 'pass_islands': True}
                tgt = server_vis
                trace = []
                avoid_prev = sess.enemy_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
                obj_new = _step_on_grid_towards(sess, obj, is_pass_islands, tgt, sq.speed, stop_range=1, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.pos = obj_new
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
                if sq.hex_distance(tgt) <= 1:
                    dmg = _scaled_damage(sq.hp, 25)
                    total_player_damage += dmg
                    # AA against squadron
                    aa = _scaled_aa(sess.player_state.carrier.hp if (sess.player_state and sess.player_state.carrier and sess.player_state.carrier.hp is not None) else CARRIER_MAX_HP, 20)
                    sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                    if audit_events is not None:
                        audit_events.append({"type": "attack", "side": "enemy", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                    if sq.hp <= 0:
                        sq.state = "lost"
                        sq.pos = Position.invalid()
                        sq.target = None
                    else:
                        sq.state = "returning"
                else:
                    sq.state = "engaging"
            else:
                # not visible: continue toward target (if any)
                if sq.target is not None:
                    obj = sq.pos
                    tgt = sq.target
                    trace = []
                    avoid_prev = sess.enemy_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
                    obj_new = _step_on_grid_towards(sess, obj, is_pass_islands, tgt,sq.speed, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                    sq.pos = obj_new
                    if audit_events is not None:
                        audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
                    if sq.pos == sq.target:
                        sq.state = "returning"
        elif sq.state == "engaging":
            server_vis = _enemy_sees_player_carrier(sess)
            if server_vis is not None:
                obj = sq.pos
                tgt = server_vis
                trace = []
                avoid_prev = sess.enemy_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
                obj_new = _step_on_grid_towards(sess, obj, is_pass_islands,tgt,sq.speed, stop_range=1, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.pos = obj_new
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
                if sq.hex_distance(tgt) <= 1:
                    dmg = _scaled_damage(sq.hp, 25)
                    total_player_damage += dmg
                    aa = _scaled_aa(sess.player_state.carrier.hp if (sess.player_state and sess.player_state.carrier and sess.player_state.carrier.hp is not None) else CARRIER_MAX_HP, 20)
                    sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                    if audit_events is not None:
                        audit_events.append({"type": "attack", "side": "enemy", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                    if sq.hp <= 0:
                        sq.state = "lost"
                        sq.pos = Position.invalid()
                        sq.target = None
                    else:
                        sq.state = "returning"
        elif sq.state == "returning":
            obj = sq.pos
            tgt = ec.pos
            trace = []
            avoid_prev = sess.enemy_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
            obj_new = _step_on_grid_towards(sess, obj, is_pass_islands, tgt,sq.speed, stop_range=1, ignore_id=sq.id, player_obs=player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
            sq.pos = obj_new
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
            if sq.hex_distance(ec) <= 1:
                sq.state = "base"
                sq.pos = Position.invalid()
                sq.target = None
        # update last pos for next turn avoidance
        if prev_pos is not None:
            sess.enemy_state.last_pos_squadrons[sq.id] = prev_pos
    return total_player_damage


def _progress_player_squadrons(sess: Session, path_sweep: Optional[list[TrackPos]] = None, audit_events: Optional[list] = None) -> int:
    total_enemy_damage = 0
    pc = sess.player_state.carrier
    ec = sess.enemy_state.carrier
    is_pass_islands = True
    for sq in list(sess.player_state.squadrons):
        prev_pos = sq.pos if sq.is_active() else None
        if sq.state == "outbound":
            # If enemy carrier within vision, move to engage
            if sq.hex_distance(ec) <= sq.vision:
                obj = sq.pos
                tgt = ec.pos
                trace = []
                avoid_prev = sess.player_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
                obj_new = _step_on_grid_towards(sess, obj, is_pass_islands, tgt,sq.speed, stop_range=1, ignore_id=sq.id, track_path=path_sweep, track_range=sq.vision or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.pos = obj_new
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
                if sq.hex_distance(ec) <= 1:
                    dmg = _scaled_damage(sq.hp, 25)
                    total_enemy_damage += dmg
                    # enemy AA
                    aa = _scaled_aa(ec.hp, 20)
                    sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                    if audit_events is not None:
                        audit_events.append({"type": "attack", "side": "player", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                    if sq.hp <= 0:
                        sq.state = "lost"; sq.pos = Position.invalid(); sq.target = None
                    else:
                        sq.state = "returning"
                else:
                    sq.state = "engaging"
            else:
                if sq.target is not None:
                    obj = sq.pos
                    tgt = sq.target
                    trace = []
                    avoid_prev = sess.player_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
                    obj_new = _step_on_grid_towards(sess, obj,is_pass_islands, tgt,sq.speed, ignore_id=sq.id, track_path=path_sweep, track_range=sq.vision or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
                    sq.pos = obj_new
                    if audit_events is not None:
                        audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
                    if sq.pos == sq.target:
                        sq.state = "returning"
        elif sq.state == "engaging":
            obj = sq.pos
            tgt = ec.pos
            trace = []
            avoid_prev = sess.player_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
            obj_new = _step_on_grid_towards(sess, obj, is_pass_islands, tgt,sq.speed, stop_range=1, ignore_id=sq.id, track_path=path_sweep, track_range=sq.vision or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
            sq.pos = obj_new
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
            if sq.hex_distance(ec) <= 1:
                dmg = _scaled_damage(sq.hp, 25)
                total_enemy_damage += dmg
                aa = _scaled_aa( ec.hp, 20)
                sq.hp = max(0, (sq.hp or SQUAD_MAX_HP) - aa)
                if audit_events is not None:
                    audit_events.append({"type": "attack", "side": "player", "unit": "squadron", "id": sq.id, "damage": dmg, "aa": aa, "unit_hp_after": sq.hp})
                if sq.hp <= 0:
                    sq.state = "lost"; sq.pos = Position.invalid(); sq.target = None
                else:
                    sq.state = "returning"
        elif sq.state == "returning":
            obj = sq.pos
            tgt = pc.pos
            trace = []
            avoid_prev = sess.player_state.last_pos_squadrons.get(sq.id) if prev_pos is not None else None
            obj_new = _step_on_grid_towards(sess, obj, is_pass_islands, tgt,sq.speed, stop_range=1, ignore_id=sq.id, track_path=path_sweep, track_range=sq.vision or VISION_SQUADRON, debug_trace=trace, avoid_prev_pos=avoid_prev)
            sq.pos = obj_new
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "player", "unit": "squadron", "id": sq.id, "from": [sq.pos.x, sq.pos.y], "target": [tgt.x, tgt.y], "trace": trace})
            if sq.hex_distance(pc) <= 1:
                sq.state = "base"; sq.pos = Position.invalid(); sq.target = None
        # update last pos for next turn avoidance
        if prev_pos is not None:
            sess.player_state.last_pos_squadrons[sq.id] = prev_pos
    return total_enemy_damage


def _apply_player_orders(sess: Session, req: SessionStepRequest, path_sweep: Optional[list[TrackPos]] = None, audit_events: Optional[list] = None):
    orders = req.player_orders
    pc = sess.player_state.carrier
    is_pass_islands = False
    # Update server-persistent carrier target if new order provided
    if orders and orders.carrier_target is not None:
        requested = Position(x=orders.carrier_target.x, y=orders.carrier_target.y)
        # If requested tile is land, adjust to nearest sea tile
        adj = _nearest_sea_tile(sess, requested)
        if adj is not None:
            sess.player_state.carrier_target = adj
        else:
            sess.player_state.carrier_target = requested
    # Move carrier toward current persistent target if any
    tgt = sess.player_state.carrier_target
    if tgt is not None:
        prev_pos_car = pc.pos
        obj = pc.pos
        trace = []
        planned_path = _gradient_full_path(
            sess,
            prev_pos_car,
            tgt,
            pass_islands=False,
            stop_range=0,
        )
        obj_new = _step_on_grid_towards(
            sess,
            obj,
            is_pass_islands,
            tgt,
            pc.speed,
            stop_range=0,
            track_path=path_sweep,
            track_range=pc.vision,
            debug_trace=trace,
            avoid_prev_pos=sess.player_state.last_pos_carrier,
        )
        pc.pos = obj_new
        pc.target = tgt
        sess.player_state.last_pos_carrier = prev_pos_car
        if audit_events is not None:
            audit_events.append({
                "type": "move", "side": "player", "unit": "carrier",
                "from": [prev_pos_car.x, prev_pos_car.y],
                "target": [tgt.x, tgt.y],
                "planned_path": planned_path,
                "steps_taken": len([e for e in trace if e.get('to')]),
                "trace": trace,
            })
        # Log map instruction only when a new user order came in this step
        if orders and orders.carrier_target is not None:
            try:
                ordered_to = tgt
                if sess.last_logged_player_target != ordered_to:
                    maplog_write(
                        sess.session_id,
                        {
                            "type": "move",
                            "side": "player",
                            "from": [prev_pos_car.x, prev_pos_car.y],
                            "to": [tgt.x, tgt.y],
                        },
                    )
                    sess.last_logged_player_target = ordered_to
            except Exception:
                pass
        # If reached destination, clear target
        if pc.pos == tgt:
            pc.target = None
            sess.player_state.carrier_target = None
    # Launch one squadron
    if orders and orders.launch_target is not None:
        # find base-available squadron
        sq = next((s for s in sess.player_state.squadrons if s.state == 'base' and (s.hp or SQUAD_MAX_HP) > 0), None)
        if sq is not None:
            # clamp range and spawn near carrier
            requested = Position(x=orders.launch_target.x, y=orders.launch_target.y)
            # Clamp to squadron maximum range from carrier
            max_range = SQUADRON_RANGE
            # If within range, keep as is; otherwise, walk along gradient path for exactly max_range steps (aircraft pass islands)
            if pc.hex_distance(requested) > max_range:
                path = _gradient_full_path(sess, pc.pos, requested, pass_islands=True, stop_range=0)
                if len(path) >= 2:
                    idx = min(max_range, len(path) - 1)
                    tgt = path[idx]
                else:
                    tgt = requested
            else:
                tgt = requested
            spawn = _find_free_adjacent(sess, pc.pos, prefer_away_from=tgt)
            if spawn is not None:
                if audit_events is not None:
                    audit_events.append({"type": "launch", "side": "player", "unit": "squadron", "id": sq.id, "spawn": [spawn.x, spawn.y], "target": [tgt.x, tgt.y]})
                sq.pos = spawn
                sq.target = tgt
                sq.state = 'outbound'


def _visibility_key(pos: Position) -> str:
    return f"{pos.x},{pos.y}"


def _mark_visibility_circle(sess: Session, vis: set, pos: Position, rng: int):
    cx, cy = pos.x, pos.y
    R = max(0, int(rng))
    H = len(sess.map)
    W = len(sess.map[0]) if H > 0 else 0
    for y in range(max(0, cy - (R + 2)), min(H, cy + (R + 3))):
        for x in range(max(0, cx - (R + 2)), min(W, cx + (R + 3))):
            np = Position(x=x, y=y)
            if np.hex_distance(pos) <= R:
                vis.add(_visibility_key(np))


def _compute_player_visibility(sess: Session, path_sweep: Optional[list[TrackPos]]) -> set:
    vis: set = set()
    pc = sess.player_state.carrier
    _mark_visibility_circle(sess, vis, pc.pos, pc.vision)
    for sq in sess.player_state.squadrons:
        if sq.is_active():
            _mark_visibility_circle(sess, vis, sq.pos, sq.vision)
    # path sweep
    for step in (path_sweep or []):
        _mark_visibility_circle(sess, vis, step, step.range)
    return vis


def _enemy_sees_player_carrier(sess: Session) -> Optional[Position]:
    """Return Position(x,y) if any enemy unit can see the player carrier, else None.

    Uses enemy carrier vision and active enemy squadrons' vision ranges.
    """
    pc = sess.player_state.carrier
    ec = sess.enemy_state.carrier
    # enemy carrier eyesight
    if ec.hex_distance(pc) <= ec.vision:
        return pc.pos.model_copy()
    # enemy squadrons
    for sq in sess.enemy_state.squadrons:
        if not sq.is_active():
            continue
        if sq.hex_distance(pc) <= sq.vision:
            return pc.pos.model_copy()
    return None
    

def _validate_sea_connectivity(sess: Session):
    # BFS-like reachability over sea using distance field from an arbitrary sea tile
    H = len(sess.map)
    W = len(sess.map[0]) if H > 0 else 0
    sea:list[Position] = []
    for y in range(H):
        for x in range(W):
            if sess.map[y][x] == 0:
                sea.append(Position(x=x, y=y))
    sea_total = len(sea)
    if sea_total == 0:
        return True, 0, 0
    spos = sea[0]
    dist = _distance_field_hex(sess,spos, pass_islands=False, ignore_id=None, player_obs=None, stop_range=0, avoid_prev_pos=None)
    if dist is None:
        return False, sea_total, 0
    INF = 10 ** 8
    reached = 0
    for spos in sea:
        if 0 <= spos.y < H and 0 <= spos.x < W and dist[y][x] < INF:
            reached += 1
    return reached == sea_total, sea_total, reached


class HexArray:

    def __init__( self, width:int, height: int ):
        self.m = [[0 for _ in range(width)] for __ in range(height)]
    
    def dump(self):
        """キャラクタベースのヘックスマップをプリントする。
        偶数/奇数行をインデントして六角形グリッドの視覚的なズレを表現します。
        0 を海 (.)、非0 を陸 (#) として表示します。
        """
        yy = "  "
        for x, col in enumerate(self.m[0]):
            yy += f" {x:2d}"
        print(yy)
        for y, row in enumerate(self.m):
            # 奇数行を少しインデント（見やすさ向上）
            yy = f"{y:2d}: "
            prefix = "  " if y % 2 == 1 else ""
            chars = []
            for cell in row:
                chars.append(f"{cell}")
            print(yy + prefix + "  ".join(chars))

# ==== Server-side map generation helpers ====
def _generate_connected_map(width: int, height: int, *, blobs: int = 10, rng: Optional[random.Random] = None):
    r = rng or random.Random()
    for _attempt in range(60):
        m = HexArray(width,height).m
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
        tmp_sess = Session(session_id="tmp", map=m )
        ok, sea_total, sea_reached = _validate_sea_connectivity(tmp_sess)
        if ok:
            return m
    return m


def _carve_sea(m: list, pos: Position, r: int):
    H = len(m)
    W = len(m[0]) if H > 0 else 0
    for y in range(H):
        for x in range(W):
            if pos.hex_distance(Position(x=x, y=y) ) <= r:
                m[y][x] = 0


def _game_status(sess: Session) -> GameStatus:
    # Determine game over conditions
    if sess.enemy_state.carrier.hp <= 0:
        return GameStatus(over=True, result='win', message='敵空母撃沈！勝利', turn=sess.turn)
    if sess.player_state.carrier.hp <= 0:
        return GameStatus(over=True, result='lose', message='我が空母撃沈…敗北', turn=sess.turn)
    if sess.turn >= sess.max_turns:
        pc = sess.player_state.carrier.hp
        ec = sess.enemy_state.carrier.hp
        if pc > ec:
            return GameStatus(over=True, result='win', message='終戦判定：優勢で勝利', turn=sess.turn)
        if pc < ec:
            return GameStatus(over=True, result='lose', message='終戦判定：劣勢で敗北', turn=sess.turn)
        return GameStatus(over=True, result='draw', message='終戦判定：引き分け', turn=sess.turn)
    return GameStatus(over=False, turn=sess.turn)


def _update_player_intel(sess: Session, turn_visible: set) -> PlayerIntel:
    # Carrier intel
    ec = sess.enemy_state.carrier
    key = _visibility_key(ec.pos)
    if key in turn_visible:
        sess.player_intel.carrier = IntelMarker(seen=True, pos=ec.pos, ttl=3)
    else:
        if sess.player_intel.carrier and sess.player_intel.carrier.ttl > 0:
            ttl = max(0, sess.player_intel.carrier.ttl - 1)
            sess.player_intel.carrier = IntelMarker(seen=ttl > 0, pos=sess.player_intel.carrier.pos, ttl=ttl)

    # Squadrons intel
    current_ids = set()
    for s in sess.enemy_state.squadrons:
        if not s.is_active():
            continue
        current_ids.add(s.id)
        k = _visibility_key(s.pos)
        if k in turn_visible:
            sess.player_intel.squadrons[s.id] = IntelMarker(seen=True, pos=s.pos, ttl=3)
        else:
            m = sess.player_intel.squadrons.get(s.id)
            if m and m.ttl > 0:
                ttl = max(0, m.ttl - 1)
                sess.player_intel.squadrons[s.id] = IntelMarker(seen=ttl > 0, pos=m.pos, ttl=ttl)
    # Decay intel for squadrons that no longer exist in state
    for sid, m in list(sess.player_intel.squadrons.items()):
        if sid not in current_ids and m.ttl > 0:
            ttl = max(0, m.ttl - 1)
            sess.player_intel.squadrons[sid] = IntelMarker(seen=ttl > 0, pos=m.pos, ttl=ttl)

    # Build response (only include entries with ttl>0 or currently seen)
    sq_list: list[SquadronIntel] = []
    for sid, m in sess.player_intel.squadrons.items():
        if m.ttl > 0 or m.seen:
            sq_list.append(SquadronIntel(id=sid, marker=m))
    return PlayerIntel(carrier=sess.player_intel.carrier, squadrons=sq_list)
