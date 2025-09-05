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
    IntelReport,
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
    UnitState,
    CarrierState,
    SquadronState,
    Position,
    PlayerOrders,
)

from server.services.turn import GameBord

# Debug flag: enable when running tests or when env var CARRIER_WAR_DEBUG is set
DEBUG = bool(os.getenv('CARRIER_WAR_DEBUG')) or ('unittest' in sys.modules) or ('PYTEST_CURRENT_TEST' in os.environ)

def _dbg(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


@dataclass
class PlayerSlot:
    token: Optional[str] = None
    name: Optional[str] = None
    orders: PlayerOrders | None = None  # raw dict from PlayerOrders for now


@dataclass
class Match:
    match_id: str
    mode: MatchMode
    map: GameBord
    status: MatchStatus = "waiting"
    config: Optional[dict] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    side_a: PlayerSlot = field(default_factory=PlayerSlot)
    side_b: PlayerSlot = field(default_factory=PlayerSlot)
    lock: Lock = field(default_factory=Lock, repr=False)
    subscribers_map: dict[asyncio.Queue[str], Optional[str]] = field(default_factory=dict, repr=False)
    last_report: Optional[dict[str, IntelReport]] = None

    def has_open_slot(self) -> bool:
        return not self.side_a.token or not self.side_b.token

    def side_for_token(self, token: str) -> Optional[str]:
        if self.side_a.token == token:
            return "A"
        if self.side_b.token == token:
            return "B"
        return None

    def _resolve_turn_minimal(self) -> None:
        # Move carriers towards targets if provided
        orders = [self.side_a.orders or PlayerOrders(), self.side_b.orders or PlayerOrders()]
        self.last_report = self.map.turn_forward(orders)  # use existing turn logic to apply orders
        # Check game over condition (any carrier destroyed)
        try:
            if self.map.is_over():
                self.status = "over"
        except Exception:
            pass

    def build_state_payload(self, viewer_side: Optional[str] = None) -> dict:
        waiting_for = "none"
        if self.status == "active":
            a_has = self.side_a.orders is not None
            b_has = self.side_b.orders is not None
            if not a_has or not b_has:
                if viewer_side == "A" and not a_has:
                    waiting_for = "you"
                elif viewer_side == "B" and not b_has:
                    waiting_for = "you"
                else:
                    waiting_for = "opponent"
        aw = self.map.W
        ah = self.map.H
        my_units, other_units = self.map.to_payload(viewer_side)
        if viewer_side and viewer_side != 'A':
            a_units, b_units = other_units, my_units
        else:
            a_units, b_units = my_units, other_units

        a_carrier =self.map.get_carrier_by_side("A")
        b_carrier =self.map.get_carrier_by_side("B")

        result = None
        if self.status == "over":
            try:
                a_hp = a_carrier.hp if a_carrier else 0
                b_hp = b_carrier.hp if b_carrier else 0
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
        result_dict = {
            "type": "state",
            "match_id": self.match_id,
            "status": self.status,
            "turn": self.map.turn,
            "waiting_for": waiting_for,
            "map_w": aw,
            "map_h": ah,
            "a": a_units,
            "b": b_units,
        }
        # Attach per-viewer logs (previous turn) if available
        try:
            if viewer_side in ("A", "B") and self.last_report is not None:
                rep = self.last_report.get(viewer_side)
                if rep and getattr(rep, "logs", None):
                    # クライアント側で重複追加を避けるため、常に最新ターンのstateに含めるだけにする
                    result_dict["logs"] = list(rep.logs)
        except Exception:
            pass
        if result:
            result_dict["result"] = result
        return result_dict


    def _broadcast_state(self) -> None:
        try:
            if len(self.subscribers_map) == 0:
                return
            subs = dict(self.subscribers_map)
            for q,token in subs.items():
                try:
                    side = self.side_for_token(token) if token else None
                    payload = self.build_state_payload(viewer_side=side)
                    data = json.dumps(payload, ensure_ascii=False)
                    q.put_nowait(data)
                except Exception:
                    pass
        except Exception:
            pass

class MatchStore:
    def __init__(self) -> None:
        self._matches: Dict[str, Match] = {}
        self._lobby_subs: list[asyncio.Queue[str]] = []

    def create(self, req: MatchCreateRequest) -> MatchCreateResponse:
        from server.services.hexmap import HexArray, generate_connected_map as hex_generate_connected_map
        # Generate connected map and carve safe sea around spawn points
        mid = str(uuid.uuid4())
        W = 30; H = 30
        map = HexArray(W, H)
        hex_generate_connected_map(map, blobs=10)
        # Place carriers and ensure sea around them
        a_units = create_units("A", 3,3 )
        b_units = create_units("B", W-4, H-4 )
        bord = GameBord(map, [a_units, b_units], log_id=mid)
        m = Match(match_id=mid, mode=req.mode or "pvp", map=bord, config=(req.config.dict() if req.config else None))
        # creator occupies side A by default
        token = str(uuid.uuid4())
        m.side_a.token = token
        m.side_a.name = req.display_name
        self._matches[mid] = m

        # broadcast lobby list update
        self._broadcast_lobby_list()

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
            self._broadcast_lobby_list()
            # broadcast updated match state so creator gets immediately notified
            m._broadcast_state()
            return MatchJoinResponse(match_id=m.match_id, player_token=token, side=side, status=m.status)

    def state(self, match_id: str, token: Optional[str]) -> MatchStateResponse:
        m = self._matches[match_id]
        side = m.side_for_token(token) if token else None
        payload = m.build_state_payload(viewer_side=side)
        return MatchStateResponse(
            match_id=m.match_id,
            status=payload.get("status", m.status),
            mode=m.mode,
            turn=payload.get("turn", m.map.turn),
            your_side=side,
            waiting_for=payload.get("waiting_for", "none"),
            map_w=payload.get("map_w"),
            map_h=payload.get("map_h"),
            a=payload.get("a"),
            b=payload.get("b"),
        )

    # --- SSE Subscribe/Unsubscribe and broadcast ---
    def subscribe(self, match_id: str, token: Optional[str]) -> asyncio.Queue[str]:
        """ start sse session """
        m = self._matches[match_id]
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        with m.lock:
            m.subscribers_map[q] = token
        return q

    def snapshot(self, match_id: str, token: Optional[str] = None) -> dict:
        """ first data for sse session"""
        m = self._matches[match_id]
        side = m.side_for_token(token) if token else None
        payload = m.build_state_payload(viewer_side=side)
        payload["map"] = m.map.get_map_array()
        return payload

    def unsubscribe(self, match_id: str, q: asyncio.Queue[str]) -> None:
        """ end of sse session """
        m = self._matches.get(match_id)
        if not m:
            return
        with m.lock:
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
                        self._broadcast_lobby_list()
                        return
                    # broadcast lobby list and updated state to remaining subscribers
                    self._broadcast_lobby_list()

                    m._broadcast_state()

    # --- Lobby SSE ---
    def lobby_subscribe(self) -> asyncio.Queue[str]:
        """ start lobby sse session """
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._lobby_subs.append(q)
        return q

    def lobby_unsubscribe(self, q: asyncio.Queue[str]) -> None:
        try:
            self._lobby_subs.remove(q)
        except ValueError:
            pass

    def _broadcast_lobby_list(self) -> None:
        try:
            payload = {"type": "list", "matches": self.list().model_dump().get("matches", [])}
            data = json.dumps(payload, ensure_ascii=False)
            subs = list(self._lobby_subs)
            for q in subs:
                try:
                    q.put_nowait(data)
                except Exception:
                    pass
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
                self._broadcast_lobby_list()
                return
            # otherwise broadcast updates
            self._broadcast_lobby_list()

            m._broadcast_state()

    def submit_orders(self, match_id: str, req: MatchOrdersRequest) -> MatchOrdersResponse:
        m = self._matches[match_id]
        side = m.side_for_token(req.player_token)
        if side is None:
            raise KeyError("invalid token")
        with m.lock:
            # store raw orders for now
            if side == "A":
                m.side_a.orders = req.player_orders
            else:
                m.side_b.orders = req.player_orders
            # Resolve turn only when both sides submitted (ready)
            if m.status == "active" and (m.side_a.orders is not None and m.side_b.orders is not None):
                try:
                    m._resolve_turn_minimal()
                except Exception:
                    # even if resolution fails, advance to avoid deadlock
                    pass
                # clear orders for next turn
                m.side_a.orders = None
                m.side_b.orders = None
            m._broadcast_state()

        return MatchOrdersResponse(accepted=True, status=m.status, turn=m.map.turn)


store = MatchStore()

# ---------- Internal helpers ----------
def create_units(side:str, cx: int, cy: int) -> list[UnitState]:
    un:list[UnitState]=[]
    i=1
    carrier = CarrierState(id=f"{side}C{i}", side=side, pos=Position(x=cx, y=cy))
    un.append(carrier)
    for s in range(0, carrier.hangar):
        un.append(SquadronState(id=f"{side}SQ{s+1}", side=side))
    return un
