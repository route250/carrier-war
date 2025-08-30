import uuid
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

from server.schemas import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStepRequest,
    SessionStepResponse,
    EnemyState,
    CarrierState,
    EnemyMemory,
    EnemyAIState,
    IntelMarker,
    PlanRequest,
    SquadronOrder,
    Position,
    PlayerIntel,
    SquadronIntel,
)
from server.services.ai import plan_orders
from server.utils.audit import audit_write


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


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, Session] = {}

    def create(self, req: SessionCreateRequest) -> SessionCreateResponse:
        sid = str(uuid.uuid4())
        # If not provided, seed a minimal enemy state
        if req.enemy_state is None:
            carrier = CarrierState(id="E1", x=26, y=26)
            enemy_state = EnemyState(carrier=carrier, squadrons=[])
        else:
            enemy_state = req.enemy_state
        if req.player_state is None:
            pc = CarrierState(id="C1", x=3, y=3)
            player_state = EnemyState(carrier=pc, squadrons=[])
        else:
            player_state = req.player_state
        sess = Session(
            id=sid,
            map=req.map,
            enemy_state=enemy_state,
            player_state=player_state,
            enemy_memory=EnemyMemory(enemy_ai=EnemyAIState()),
            rand_seed=req.rand_seed,
            config=req.config.dict() if req.config else None,
        )
        self._sessions[sid] = sess
        return SessionCreateResponse(
            session_id=sid,
            enemy_state=sess.enemy_state,
            player_state=sess.player_state,
            enemy_memory=sess.enemy_memory,
            config=req.config,
        )

    def get(self, sid: str) -> Session:
        return self._sessions[sid]

    def step(self, sid: str, req: SessionStepRequest) -> SessionStepResponse:
        sess = self.get(sid)
        aud = lambda ev: audit_write(sid, {"turn": sess.turn, **ev})
        aud({"type": "turn_start"})
        # advance turn counter
        sess.turn += 1
        # Update memory: if player carrier visible, set TTL=3 at provided coords
        if req.player_visible_carrier is not None:
            sess.enemy_memory.carrier_last_seen = IntelMarker(
                seen=True,
                x=req.player_visible_carrier.x,
                y=req.player_visible_carrier.y,
                ttl=3,
            )
        # Build PlanRequest using session state
        plan_req = PlanRequest(
            turn=0,
            map=sess.map,
            enemy_state=sess.enemy_state,
            enemy_memory=sess.enemy_memory,
            player_observation=req.player_observation,
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
        dmg_to_player = _progress_enemy_squadrons(sess, req, audit_events)
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


def _is_occupied(sess: Session, x: int, y: int, ignore_id: Optional[str] = None, player_obs=None) -> bool:
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
    player_obs=None,
    track_path: Optional[list] = None,
    track_range: int = 0,
    debug_trace: Optional[list] = None,
    avoid_prev_pos: Optional[tuple] = None,
):
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


def _progress_enemy_squadrons(sess: Session, req: SessionStepRequest, audit_events: Optional[list] = None) -> int:
    total_player_damage = 0
    ec = sess.enemy_state.carrier
    # shallow copy for safe iteration
    for sq in list(sess.enemy_state.squadrons):
        prev_pos = (sq.x, sq.y) if sq.x is not None and sq.y is not None else None
        if sq.state == "outbound":
            # If player carrier visible, move toward it and possibly attack
            if req.player_visible_carrier is not None:
                # move toward player carrier with stopRange 1
                obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                tgt = {'x': req.player_visible_carrier.x, 'y': req.player_visible_carrier.y}
                trace = []
                avoid_prev = sess.last_pos_enemy_sq.get(sq.id) if prev_pos is not None else None
                _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, player_obs=req.player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.x, sq.y = obj['x'], obj['y']
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                if _hex_distance(sq.x, sq.y, tgt['x'], tgt['y']) <= 1:
                    dmg = _scaled_damage(getattr(sq, 'hp', SQUAD_MAX_HP), 25)
                    total_player_damage += dmg
                    # AA against squadron
                    aa = _scaled_aa(req.player_carrier_hp if req.player_carrier_hp is not None else CARRIER_MAX_HP, 20)
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
                    _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, ignore_id=sq.id, player_obs=req.player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                    sq.x, sq.y = obj['x'], obj['y']
                    if audit_events is not None:
                        audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                    if sq.x == sq.target.x and sq.y == sq.target.y:
                        sq.state = "returning"
        elif sq.state == "engaging":
            if req.player_visible_carrier is not None:
                obj = {'x': sq.x, 'y': sq.y, 'pass_islands': True}
                tgt = {'x': req.player_visible_carrier.x, 'y': req.player_visible_carrier.y}
                trace = []
                avoid_prev = sess.last_pos_enemy_sq.get(sq.id) if prev_pos is not None else None
                _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, player_obs=req.player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
                sq.x, sq.y = obj['x'], obj['y']
                if audit_events is not None:
                    audit_events.append({"type": "move", "side": "enemy", "unit": "squadron", "id": sq.id, "from": [sq.x, sq.y], "target": [tgt['x'], tgt['y']], "trace": trace})
                if _hex_distance(sq.x, sq.y, tgt['x'], tgt['y']) <= 1:
                    dmg = _scaled_damage(getattr(sq, 'hp', SQUAD_MAX_HP), 25)
                    total_player_damage += dmg
                    aa = _scaled_aa(req.player_carrier_hp if req.player_carrier_hp is not None else CARRIER_MAX_HP, 20)
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
            _step_on_grid_towards(sess, obj, tgt, getattr(sq, 'speed', 10) or 10, stop_range=1, ignore_id=sq.id, player_obs=req.player_observation, debug_trace=trace, avoid_prev_pos=avoid_prev)
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
            _step_on_grid_towards(sess, obj, tgt, getattr(pc, 'speed', 2) or 2, stop_range=0, track_path=path_sweep, track_range=getattr(pc, 'vision', VISION_CARRIER) or VISION_CARRIER, debug_trace=trace, avoid_prev_pos=sess.last_pos_player_carrier)
            pc.x, pc.y = obj['x'], obj['y']
            sess.last_pos_player_carrier = prev_pos_car
            if audit_events is not None:
                audit_events.append({"type": "move", "side": "player", "unit": "carrier", "from": [pc.x, pc.y], "target": [tgt['x'], tgt['y']], "trace": trace})
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
