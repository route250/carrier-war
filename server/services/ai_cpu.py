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

from typing import List, Optional

from server.services.ai_thread import AIThreadABC
from server.services.ai import plan_orders
from server.schemas import (
    PlayerOrders,
    Position,
    PlayerState,
    CarrierState,
    SquadronState,
    EnemyMemory,
    PlayerObservation,
    SquadronLight,
    PlanRequest,
    PlanResponse,
    CarrierOrder,
    SquadronOrder,
    Config,
)


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

    async def think(self, payload: dict) -> None:  # type: ignore[override]
        # 1) 地形マップを確保（初回のみ）
        if self._map is None:
            try:
                snap = self.store.snapshot(self.match_id, self.token)
                self._map = snap.get("map")
            except Exception:
                self._map = None
        if not self._map:
            # マップが無ければ安全策として何も出さない
            await self.on_orders(PlayerOrders())
            return

        # 2) state payload から自軍（AI側）状態を復元
        enemy_state = self._payload_to_player_state(payload)
        if enemy_state is None:
            # 復元できない場合はノーオーダー
            await self.on_orders(PlayerOrders())
            return

        # 3) PlayerObservation（任意）: 可視編隊のみ最小反映（なければ None でOK）
        player_obs = self._payload_to_player_observation(payload)

        # 4) 既存AIへ入力してオーダーを算出
        req = PlanRequest(
            turn=int(payload.get("turn", 1)),
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
        await self.on_orders(orders)

    # --- helpers ---
    def _payload_to_player_state(self, payload: dict) -> Optional[PlayerState]:
        try:
            units = payload.get("units", {})
            carr = units.get("carrier")
            if not carr:
                return None
            cx = carr.get("x")
            cy = carr.get("y")
            if cx is None or cy is None:
                return None
            carrier = CarrierState(
                id=carr.get("id") or "C",
                side=self.side or "B",
                pos=Position(x=int(cx), y=int(cy)),
                hp=int(carr.get("hp")) if carr.get("hp") is not None else CarrierState().hp,
                max_hp=int(carr.get("max_hp")) if carr.get("max_hp") is not None else CarrierState().max_hp,
                speed=int(carr.get("speed")) if carr.get("speed") is not None else CarrierState().speed,
                fuel=int(carr.get("fuel")) if carr.get("fuel") is not None else CarrierState().fuel,
                vision=int(carr.get("vision")) if carr.get("vision") is not None else CarrierState().vision,
            )

            sq_list = []
            for sq in units.get("squadrons", []) or []:
                pos_x = sq.get("x")
                pos_y = sq.get("y")
                squad = SquadronState(
                    id=sq.get("id") or "SQ",
                    side=self.side or "B",
                    hp=int(sq.get("hp")) if sq.get("hp") is not None else SquadronState().hp,
                    max_hp=int(sq.get("max_hp")) if sq.get("max_hp") is not None else SquadronState().max_hp,
                    speed=int(sq.get("speed")) if sq.get("speed") is not None else SquadronState().speed,
                    fuel=int(sq.get("fuel")) if sq.get("fuel") is not None else SquadronState().fuel,
                    vision=int(sq.get("vision")) if sq.get("vision") is not None else SquadronState().vision,
                    state=str(sq.get("state") or "base"),
                )
                if pos_x is not None and pos_y is not None:
                    squad.pos = Position(x=int(pos_x), y=int(pos_y))
                sq_list.append(squad)

            return PlayerState(side=self.side or "B", carrier=carrier, squadrons=sq_list)
        except Exception:
            return None

    def _payload_to_player_observation(self, payload: dict) -> Optional[PlayerObservation]:
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
