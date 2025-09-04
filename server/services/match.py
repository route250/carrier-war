import time
import uuid
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional

from server.schemas import (
    Config,
    MatchCreateRequest,
    MatchCreateResponse,
    MatchJoinRequest,
    MatchJoinResponse,
    MatchListItem,
    MatchListResponse,
    MatchMode,
    MatchOrdersRequest,
    MatchOrdersResponse,
    MatchStateResponse,
    MatchStatus,
    PlayerState,
    CarrierState,
    SquadronState,
    Position,
    SideIntel,
    IntelMarker,
)
from types import SimpleNamespace
from server.services.session import (
    _find_path_hex,
    _gradient_full_path,
    _generate_connected_map,
    _carve_sea,
    _nearest_sea_tile,
    _find_free_adjacent,
    _scaled_damage,
    SQUADRON_RANGE,
)

# Debug flag: enable when running tests or when env var CARRIER_WAR_DEBUG is set
DEBUG = bool(os.getenv('CARRIER_WAR_DEBUG')) or ('unittest' in sys.modules) or ('PYTEST_CURRENT_TEST' in os.environ)

def _dbg(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


@dataclass
class PlayerSlot:
    token: Optional[str] = None
    name: Optional[str] = None
    orders: Optional[dict] = None  # raw dict from PlayerOrders for now


@dataclass
class Match:
    match_id: str
    mode: MatchMode
    status: MatchStatus = "waiting"
    config: Optional[dict] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    turn: int = 1
    # Minimal world state for PvP (carriers only for now)
    map: Optional[list] = None
    a_state: Optional[PlayerState] = None
    b_state: Optional[PlayerState] = None
    side_a: PlayerSlot = field(default_factory=PlayerSlot)
    side_b: PlayerSlot = field(default_factory=PlayerSlot)
    # Per-side intel memory (what A knows about B, and B about A)
    intel_a: SideIntel = field(default_factory=SideIntel)
    intel_b: SideIntel = field(default_factory=SideIntel)
    lock: Lock = field(default_factory=Lock, repr=False)
    subscribers: list[asyncio.Queue[str]] = field(default_factory=list, repr=False)
    subscribers_map: dict[asyncio.Queue[str], Optional[str]] = field(default_factory=dict, repr=False)

    def has_open_slot(self) -> bool:
        return not self.side_a.token or not self.side_b.token

    def side_for_token(self, token: str) -> Optional[str]:
        if self.side_a.token == token:
            return "A"
        if self.side_b.token == token:
            return "B"
        return None


class MatchStore:
    def __init__(self) -> None:
        self._matches: Dict[str, Match] = {}
        self._lobby_subs: list[asyncio.Queue[str]] = []

    def create(self, req: MatchCreateRequest) -> MatchCreateResponse:
        mid = str(uuid.uuid4())
        m = Match(match_id=mid, mode=req.mode or "pvp", config=(req.config.dict() if req.config else None))
        # creator occupies side A by default
        token = str(uuid.uuid4())
        m.side_a.token = token
        m.side_a.name = req.display_name
        self._matches[mid] = m
        # initialize minimal world state
        self._init_world(m)
        # broadcast lobby list update
        try:
            self._broadcast_lobby_list()
        except Exception:
            pass
        return MatchCreateResponse(
            match_id=mid,
            player_token=token,
            side="A",
            status=m.status,
            mode=m.mode,
            config=req.config,
        )

    def list(self) -> MatchListResponse:
        items = []
        for m in self._matches.values():
            items.append(
                MatchListItem(
                    match_id=m.match_id,
                    status=m.status,
                    mode=m.mode,
                    has_open_slot=m.has_open_slot(),
                    created_at=m.created_at,
                    config=Config(**m.config) if m.config else None,
                )
            )
        return MatchListResponse(matches=items)

    # --- internal: initialize world for a match ---
    def _init_world(self, m: Match) -> None:
        # Generate connected map and carve safe sea around spawn points
        W = 30; H = 30
        m.map = _generate_connected_map(W, H)
        # Place carriers and ensure sea around them
        m.a_state = _new_player_state("A", 3, 3)
        m.b_state = _new_player_state("B", W-4, H-4)
        _carve_sea(m.map, m.a_state.carrier.pos, 2)
        _carve_sea(m.map, m.b_state.carrier.pos, 2)
        m.turn = 1
        # reset intel memory
        m.intel_a = SideIntel()
        m.intel_b = SideIntel()

    # --- internal: resolve a minimal turn using carrier move orders only ---
    def _resolve_turn_minimal(self, m: Match) -> None:
        if not (m.a_state and m.b_state and m.map is not None):
            self._init_world(m)
        # Move carriers towards targets if provided
        self._apply_carrier_move(m, side='A', orders=m.side_a.orders)
        self._apply_carrier_move(m, side='B', orders=m.side_b.orders)
        # Apply launch orders (spawn one squadron if available)
        self._apply_launch_order(m, side='A', orders=m.side_a.orders)
        self._apply_launch_order(m, side='B', orders=m.side_b.orders)
        # Progress squadrons and resolve minimal engagement/damage
        self._progress_squadrons(m)
        # Update per-side intel (visibility + decay)
        try:
            self._update_intel(m)
        except Exception:
            pass
        # Check game over condition (any carrier destroyed)
        try:
            a_hp = m.a_state.carrier.hp if m.a_state else 0
            b_hp = m.b_state.carrier.hp if m.b_state else 0
            if a_hp <= 0 or b_hp <= 0:
                m.status = "over"
        except Exception:
            pass
        m.turn += 1

    def _apply_carrier_move(self, m: Match, *, side: str, orders: Optional[dict]) -> None:
        if not orders:
            return
        me = m.a_state if side == 'A' else m.b_state
        op = m.b_state if side == 'A' else m.a_state
        if me is None or op is None or m.map is None:
            return
        tgt = orders.get('carrier_target') if isinstance(orders, dict) else None
        if not tgt or not isinstance(tgt, dict):
            return
        try:
            goal = Position(x=int(tgt['x']), y=int(tgt['y']))
        except Exception:
            return
        # If requested tile is land, clamp to nearest sea tile (match session behaviour)
        try:
            g_tile = m.map[goal.y][goal.x]
        except Exception:
            g_tile = None
        if g_tile is not None and g_tile != 0:
            adj = _nearest_sea_tile(sess_view, goal)
            if adj is not None:
                goal = adj
        start = me.carrier.pos
        sess_view = _make_session_view(m.map, me, op)
        # Ignore occupancy by own carrier so start tile is passable
        path = _find_path_hex(
            sess_view,
            start,
            goal,
            pass_islands=False,
            ignore_id=me.carrier.id,
            player_obs=None,
            stop_range=0,
            avoid_prev_pos=me.last_pos_carrier,
        )
        # Fallback: if A* returned None (start not passable or other), try gradient wavefront path
        if path is None:
            try:
                path = _gradient_full_path(
                    sess_view,
                    start,
                    goal,
                    pass_islands=False,
                    stop_range=0,
                )
            except Exception:
                path = None
        # Debug: log path info to help diagnose test failures
        try:
            plen = len(path) if path is not None else 0
        except Exception:
            plen = 0
        # inspect map tile values around start/goal
        try:
            start_tile = sess_view.map[start.y][start.x]
        except Exception:
            start_tile = None
        try:
            goal_tile = sess_view.map[goal.y][goal.x]
        except Exception:
            goal_tile = None
        _dbg(f"[DEBUG]_apply_carrier_move side={side} start={start.x},{start.y} tile={start_tile} goal={goal.x},{goal.y} tile={goal_tile} path_len={plen} last_pos={me.last_pos_carrier}")
        if not path or len(path) <= 1:
            _dbg(f"[DEBUG]_apply_carrier_move no movement (path empty or length<=1): path={path}")
            return
        steps = max(1, min(me.carrier.speed or 1, len(path) - 1))
        new_pos = path[steps]
        _dbg(f"[DEBUG]_apply_carrier_move steps={steps} new_pos={new_pos.x},{new_pos.y}")
        me.last_pos_carrier = start
        me.carrier.pos = new_pos

    def _apply_launch_order(self, m: Match, *, side: str, orders: Optional[dict]) -> None:
        if not orders:
            return
        me = m.a_state if side == 'A' else m.b_state
        op = m.b_state if side == 'A' else m.a_state
        if me is None or op is None or m.map is None:
            return
        tgt = orders.get('launch_target') if isinstance(orders, dict) else None
        if not tgt or not isinstance(tgt, dict):
            return
        # find one squadron at base with HP>0
        sq = next((s for s in me.squadrons if s.state == 'base' and (s.hp or 0) > 0), None)
        if not sq:
            return
        try:
            requested = Position(x=int(tgt['x']), y=int(tgt['y']))
        except Exception:
            return
        # Clamp to aircraft range from carrier using air path (pass_islands=True)
        carrier_pos = me.carrier.pos
        sess_view = _make_session_view(m.map, me, op)
        path = _find_path_hex(
            sess_view,
            carrier_pos,
            requested,
            pass_islands=True,
            ignore_id=None,
            player_obs=None,
            stop_range=0,
            avoid_prev_pos=None,
        )
        if path and len(path) >= 2:
            idx = min(SQUADRON_RANGE, len(path) - 1)
            tgt_pos = path[idx]
        else:
            tgt_pos = requested
        # spawn adjacent to carrier, prefer away from target
        spawn = _find_free_adjacent(sess_view, carrier_pos, prefer_away_from=tgt_pos)
        if not spawn:
            return
        _dbg(f"[DEBUG]_apply_launch_order side={side} launching sq={sq.id} spawn={spawn.x},{spawn.y} tgt={tgt_pos.x},{tgt_pos.y}")
        sq.pos = spawn
        sq.target = tgt_pos
        sq.state = 'outbound'
        _dbg(f"[DEBUG]_apply_launch_order sq={sq.id} state now={sq.state} pos={sq.pos.x},{sq.pos.y} target={sq.target.x},{sq.target.y}")

    def _progress_squadrons(self, m: Match) -> None:
        if not (m.a_state and m.b_state and m.map is not None):
            return
        meA, meB = m.a_state, m.b_state
        sessA = _make_session_view(m.map, meA, meB)
        sessB = _make_session_view(m.map, meB, meA)
        # Move function using A* (air pass terrain)
        def move_towards(sess_view, unit, target):
            if target is None:
                return unit.pos
            path = _find_path_hex(
                sess_view,
                unit.pos,
                target,
                pass_islands=True,
                ignore_id=getattr(unit, 'id', None),
                player_obs=None,
                stop_range=0,
                avoid_prev_pos=None,
            )
            if not path or len(path) <= 1:
                return unit.pos
            steps = max(1, min(unit.speed or 1, len(path) - 1))
            return path[steps]

        # progress and collect damage
        dmg_to_A = 0
        dmg_to_B = 0
        # snapshot last-turn positions for squadrons (may be empty)
        lastA = dict(meA.last_pos_squadrons) if hasattr(meA, 'last_pos_squadrons') else {}
        lastB = dict(meB.last_pos_squadrons) if hasattr(meB, 'last_pos_squadrons') else {}

        # friendly squadrons
        for sq in list(meA.squadrons):
            if not sq.is_active():
                continue
            _dbg(f"[DEBUG]_progress_squadrons A sq={sq.id} state={sq.state} pos={(sq.pos.x if sq.pos else None)},{(sq.pos.y if sq.pos else None)} target={(sq.target.x if sq.target else None)},{(sq.target.y if sq.target else None)} hp={sq.hp}")
            if sq.state in ('outbound', 'engaging') and sq.target is not None:
                new_pos = move_towards(sessA, sq, sq.target)
                sq.pos = new_pos
                if new_pos.hex_distance(sq.target) == 0:
                    sq.state = 'engaging'
            if sq.state == 'engaging':
                # attack if within 1 hex of enemy carrier, then set to return
                if sq.hex_distance(meB.carrier.pos) <= 1:
                    dmg_to_B += _scaled_damage(sq.hp, base=18)
                    sq.target = meA.carrier.pos
                    sq.state = 'returning'
                else:
                    prev = lastA.get(sq.id)
                    if sq.target is not None and sq.pos == sq.target and prev is not None and prev == sq.pos:
                        sq.target = meA.carrier.pos
                        sq.state = 'returning'
            if sq.state == 'returning':
                sq.target = meA.carrier.pos
                new_pos = move_towards(sessA, sq, sq.target)
                sq.pos = new_pos
                if new_pos.hex_distance(meA.carrier.pos) <= 1:
                    # land
                    sq.state = 'base'
                    sq.pos = Position.invalid()
                    sq.target = None

        # enemy squadrons
        for sq in list(meB.squadrons):
            if not sq.is_active():
                continue
            _dbg(f"[DEBUG]_progress_squadrons B sq={sq.id} state={sq.state} pos={(sq.pos.x if sq.pos else None)},{(sq.pos.y if sq.pos else None)} target={(sq.target.x if sq.target else None)},{(sq.target.y if sq.target else None)} hp={sq.hp}")
            if sq.state in ('outbound', 'engaging') and sq.target is not None:
                new_pos = move_towards(sessB, sq, sq.target)
                sq.pos = new_pos
                if new_pos.hex_distance(sq.target) == 0:
                    sq.state = 'engaging'
            if sq.state == 'engaging':
                if sq.hex_distance(meA.carrier.pos) <= 1:
                    dmg_to_A += _scaled_damage(sq.hp, base=18)
                    sq.target = meB.carrier.pos
                    sq.state = 'returning'
                else:
                    prev = lastB.get(sq.id)
                    if sq.target is not None and sq.pos == sq.target and prev is not None and prev == sq.pos:
                        sq.target = meB.carrier.pos
                        sq.state = 'returning'
            if sq.state == 'returning':
                sq.target = meB.carrier.pos
                new_pos = move_towards(sessB, sq, sq.target)
                sq.pos = new_pos
                if new_pos.hex_distance(meB.carrier.pos) <= 1:
                    sq.state = 'base'
                    sq.pos = Position.invalid()
                    sq.target = None

        # apply damage
        if dmg_to_A:
            meA.carrier.hp = max(0, (meA.carrier.hp or 0) - int(dmg_to_A))
        if dmg_to_B:
            meB.carrier.hp = max(0, (meB.carrier.hp or 0) - int(dmg_to_B))
        # debug: final squadron states
        _dbg("[DEBUG]_progress_squadrons final states A:", [(s.id, s.state, (s.pos.x if s.pos else None, s.pos.y if s.pos else None)) for s in meA.squadrons])
        _dbg("[DEBUG]_progress_squadrons final states B:", [(s.id, s.state, (s.pos.x if s.pos else None, s.pos.y if s.pos else None)) for s in meB.squadrons])
        # update last positions for squadrons
        for s in meA.squadrons:
            if s.pos is not None and s.pos.x >= 0 and s.pos.y >= 0:
                meA.last_pos_squadrons[s.id] = s.pos
        for s in meB.squadrons:
            if s.pos is not None and s.pos.x >= 0 and s.pos.y >= 0:
                meB.last_pos_squadrons[s.id] = s.pos

    def join(self, match_id: str, req: MatchJoinRequest) -> MatchJoinResponse:
        m = self._matches[match_id]
        with m.lock:
            if not m.side_b.token:
                side = "B"
                token = str(uuid.uuid4())
                m.side_b.token = token
                m.side_b.name = req.display_name
            elif not m.side_a.token:
                side = "A"
                token = str(uuid.uuid4())
                m.side_a.token = token
                m.side_a.name = req.display_name
            else:
                # already full
                raise KeyError("match full")
            # if both present, activate
            if m.side_a.token and m.side_b.token:
                m.status = "active"
            # broadcast lobby list update
            try:
                self._broadcast_lobby_list()
            except Exception:
                pass
            return MatchJoinResponse(match_id=m.match_id, player_token=token, side=side, status=m.status)

    def state(self, match_id: str, token: Optional[str]) -> MatchStateResponse:
        m = self._matches[match_id]
        side = m.side_for_token(token) if token else None
        payload = self._build_state_payload(m, viewer_side=side)
        return MatchStateResponse(
            match_id=m.match_id,
            status=payload.get("status", m.status),
            mode=m.mode,
            turn=payload.get("turn", m.turn),
            your_side=side,
            waiting_for=payload.get("waiting_for", "none"),
            map_w=payload.get("map_w"),
            map_h=payload.get("map_h"),
            a=payload.get("a"),
            b=payload.get("b"),
        )

    def snapshot(self, match_id: str, token: Optional[str] = None) -> dict:
        m = self._matches[match_id]
        side = m.side_for_token(token) if token else None
        payload = self._build_state_payload(m, viewer_side=side)
        payload["map"] = m.map
        return payload

    # --- SSE Subscribe/Unsubscribe and broadcast ---
    def subscribe(self, match_id: str, token: Optional[str]) -> asyncio.Queue[str]:
        m = self._matches[match_id]
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        with m.lock:
            m.subscribers.append(q)
            m.subscribers_map[q] = token
        return q

    def unsubscribe(self, match_id: str, q: asyncio.Queue[str]) -> None:
        m = self._matches.get(match_id)
        if not m:
            return
        with m.lock:
            # remove subscriber
            try:
                m.subscribers.remove(q)
            except ValueError:
                pass
            token = m.subscribers_map.pop(q, None)
            # If this subscriber was tied to a player token and no other
            # subscriptions remain for that token, consider that player left
            if token:
                remaining = [tok for tok in m.subscribers_map.values() if tok == token]
                if not remaining:
                    # clear player's slot
                    if m.side_a.token == token:
                        m.side_a = PlayerSlot()  # reset
                    elif m.side_b.token == token:
                        m.side_b = PlayerSlot()
                    # update status
                    if not (m.side_a.token and m.side_b.token):
                        if m.status != "over":
                            m.status = "waiting"
                    # if no players remain, delete match entirely
                    if not m.side_a.token and not m.side_b.token:
                        # delete and broadcast lobby list, then return
                        try:
                            del self._matches[m.match_id]
                        except Exception:
                            pass
                        try:
                            self._broadcast_lobby_list()
                        except Exception:
                            pass
                        return
                    # broadcast lobby list and updated state to remaining subscribers
                    try:
                        self._broadcast_lobby_list()
                    except Exception:
                        pass
                    try:
                        self._broadcast_state(m)
                    except Exception:
                        pass

    def _broadcast(self, m: Match, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        subs = list(m.subscribers)
        for q in subs:
            try:
                q.put_nowait(data)
            except Exception:
                pass

    # --- Lobby SSE ---
    def lobby_subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._lobby_subs.append(q)
        return q

    def lobby_unsubscribe(self, q: asyncio.Queue[str]) -> None:
        try:
            self._lobby_subs.remove(q)
        except ValueError:
            pass

    def _broadcast_lobby_list(self) -> None:
        payload = {"type": "list", "matches": self.list().model_dump().get("matches", [])}
        data = json.dumps(payload, ensure_ascii=False)
        subs = list(self._lobby_subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except Exception:
                pass

    def leave(self, match_id: str, token: str) -> None:
        m = self._matches.get(match_id)
        if not m:
            raise KeyError("match not found")
        with m.lock:
            changed = False
            if m.side_a.token == token:
                m.side_a = PlayerSlot(); changed = True
            if m.side_b.token == token:
                m.side_b = PlayerSlot(); changed = True
            if not changed:
                return
            if not (m.side_a.token and m.side_b.token) and m.status != "over":
                m.status = "waiting"
            # if no players remain, delete match
            if not m.side_a.token and not m.side_b.token:
                try:
                    del self._matches[m.match_id]
                except Exception:
                    pass
                try:
                    self._broadcast_lobby_list()
                except Exception:
                    pass
                return
            # otherwise broadcast updates
            try:
                self._broadcast_lobby_list()
            except Exception:
                pass
            try:
                self._broadcast_state(m)
            except Exception:
                pass

    def _broadcast_state(self, m: Match) -> None:
        subs = list(m.subscribers)
        for q in subs:
            try:
                token = m.subscribers_map.get(q)
                side = m.side_for_token(token) if token else None
                payload = self._build_state_payload(m, viewer_side=side)
                data = json.dumps(payload, ensure_ascii=False)
                q.put_nowait(data)
            except Exception:
                pass

    def _build_state_payload(self, m: Match, *, viewer_side: Optional[str] = None) -> dict:
        waiting_for = "none"
        if m.status == "active":
            a_has = m.side_a.orders is not None
            b_has = m.side_b.orders is not None
            if not a_has or not b_has:
                if viewer_side == "A" and not a_has:
                    waiting_for = "you"
                elif viewer_side == "B" and not b_has:
                    waiting_for = "you"
                else:
                    waiting_for = "opponent"
        aw = len(m.map[0]) if m.map else 30
        ah = len(m.map) if m.map else 30
        a_car_full = {"x": (m.a_state.carrier.pos.x if m.a_state else None), "y": (m.a_state.carrier.pos.y if m.a_state else None), "hp": (m.a_state.carrier.hp if m.a_state else None)}
        b_car_full = {"x": (m.b_state.carrier.pos.x if m.b_state else None), "y": (m.b_state.carrier.pos.y if m.b_state else None), "hp": (m.b_state.carrier.hp if m.b_state else None)}
        # Gate opponent info based on intel (3ターン以内の目標捕捉のみ表示)
        SHOW_TURNS = 3
        a_car = dict(a_car_full)
        b_car = dict(b_car_full)
        if viewer_side == 'A':
            mark = m.intel_a.carrier
            if not (mark and isinstance(mark.ttl, int) and mark.ttl > 0 and mark.ttl <= SHOW_TURNS):
                # hide opponent carrier
                b_car = {"x": None, "y": None, "hp": None}
        elif viewer_side == 'B':
            mark = m.intel_b.carrier
            if not (mark and isinstance(mark.ttl, int) and mark.ttl > 0 and mark.ttl <= SHOW_TURNS):
                a_car = {"x": None, "y": None, "hp": None}
        else:
            # viewer unknown, hide both opponents
            a_car = {"x": None, "y": None, "hp": None}
            b_car = {"x": None, "y": None, "hp": None}
        a_sq = _squad_light_list(m.a_state) if (viewer_side == 'A') else None
        b_sq = _squad_light_list(m.b_state) if (viewer_side == 'B') else None
        # per-viewer result when over
        result = None
        if m.status == "over":
            try:
                a_hp = m.a_state.carrier.hp if m.a_state else 0
                b_hp = m.b_state.carrier.hp if m.b_state else 0
                if a_hp <= 0 and b_hp <= 0:
                    result = "draw"
                elif viewer_side == "A":
                    result = "lose" if a_hp <= 0 and b_hp > 0 else ("win" if b_hp <= 0 and a_hp > 0 else "draw")
                elif viewer_side == "B":
                    result = "lose" if b_hp <= 0 and a_hp > 0 else ("win" if a_hp <= 0 and b_hp > 0 else "draw")
                else:
                    result = "draw"
            except Exception:
                result = None
        return {
            "type": "state",
            "match_id": m.match_id,
            "status": m.status,
            "turn": m.turn,
            "waiting_for": waiting_for,
            **({"result": result} if result is not None else {}),
            "map_w": aw,
            "map_h": ah,
            "a": {k: v for k, v in {"carrier": a_car, "squadrons": a_sq}.items() if v is not None},
            "b": {k: v for k, v in {"carrier": b_car, "squadrons": b_sq}.items() if v is not None},
        }

    def _update_intel(self, m: Match) -> None:
        """Update per-side intel memory based on current visibility. TTL decays per turn.
        A side remembers opponent carrier position for 3ターン after last seen.
        """
        if not (m.a_state and m.b_state):
            return
        ttl_reset = 3
        # helper to test visibility of op carrier against me's units
        def sees(me: PlayerState, op: PlayerState) -> bool:
            try:
                if me.carrier and me.carrier.hex_distance(op.carrier.pos) <= (me.carrier.vision or 0):
                    return True
                for sq in me.squadrons:
                    try:
                        if sq.is_active() and sq.hex_distance(op.carrier.pos) <= (sq.vision or 0):
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            return False
        # A's intel about B
        if sees(m.a_state, m.b_state):
            m.intel_a.carrier = IntelMarker(seen=True, pos=m.b_state.carrier.pos, ttl=ttl_reset)
        else:
            if m.intel_a.carrier is not None:
                cur = m.intel_a.carrier
                m.intel_a.carrier = IntelMarker(seen=cur.seen, pos=cur.pos, ttl=max(0, (cur.ttl or 0) - 1))
        # B's intel about A
        if sees(m.b_state, m.a_state):
            m.intel_b.carrier = IntelMarker(seen=True, pos=m.a_state.carrier.pos, ttl=ttl_reset)
        else:
            if m.intel_b.carrier is not None:
                cur = m.intel_b.carrier
                m.intel_b.carrier = IntelMarker(seen=cur.seen, pos=cur.pos, ttl=max(0, (cur.ttl or 0) - 1))

    def submit_orders(self, match_id: str, req: MatchOrdersRequest) -> MatchOrdersResponse:
        m = self._matches[match_id]
        side = m.side_for_token(req.player_token)
        if side is None:
            raise KeyError("invalid token")
        with m.lock:
            # store raw orders for now
            if side == "A":
                m.side_a.orders = (req.player_orders.dict() if req.player_orders is not None else {})
            else:
                m.side_b.orders = (req.player_orders.dict() if req.player_orders is not None else {})
            # Resolve turn only when both sides submitted (ready)
            if m.status == "active" and (m.side_a.orders is not None and m.side_b.orders is not None):
                try:
                    self._resolve_turn_minimal(m)
                except Exception:
                    # even if resolution fails, advance to avoid deadlock
                    m.turn += 1
                # clear orders for next turn
                m.side_a.orders = None
                m.side_b.orders = None
                try:
                    self._broadcast_state(m)
                except Exception:
                    pass
            else:
                # one side submitted; broadcast waiting state
                try:
                    self._broadcast_state(m)
                except Exception:
                    pass
        return MatchOrdersResponse(accepted=True, status=m.status, turn=m.turn)


store = MatchStore()

# ---------- Internal helpers ----------
def _new_player_state(side:str, cx: int, cy: int) -> PlayerState:
    id_prefix = "C1" if side == "A" else "E1"
    carrier = CarrierState(id=f"{id_prefix}", side=side, pos=Position(x=cx, y=cy), hp=100, speed=2, vision=4)
    squadrons = [SquadronState(id=f"{id_prefix}SQ{i+1}", side=side, pos=Position.invalid(), state='base', hp=40, speed=4, vision=3) for i in range(carrier.hangar)]
    return PlayerState(side=side,carrier=carrier, squadrons=squadrons)


def _make_session_view(map_grid: list, me: PlayerState, op: PlayerState):
    # Create a lightweight object with attributes expected by pathfinding helpers
    return SimpleNamespace(map=map_grid, player_state=me, enemy_state=op)


def _squad_light_list(ps: Optional[PlayerState]) -> Optional[list[dict]]:
    if ps is None:
        return None
    out: list[dict] = []
    for s in ps.squadrons:
        item = {
            "id": s.id,
            "hp": s.hp,
            "state": s.state,
            "x": (s.pos.x if s.is_active() else None),
            "y": (s.pos.y if s.is_active() else None),
        }
        out.append(item)
    return out
