

from abc import ABC
import asyncio
from server.schemas import MatchJoinRequest, MatchJoinResponse, PlayerOrders, MatchStateResponse, MatchOrdersRequest, MatchOrdersResponse
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from server.services.match import MatchStore

"""AIの思考ルーチンを実装するための抽象クラスと具体的なクラス"""
class AIThreadABC(ABC):
    """AIの思考ルーチンを実装するための抽象クラス"""
    def __init__(self, store:'MatchStore', match_id: str):
        self.name = "AI(easy)"
        self.store: MatchStore = store
        self.match_id = match_id
        self.token = None
        self.side = None
        self.q = asyncio.Queue() # タイミング通知用のキュー（自身のイベントループ専用）
        self.stat:int = 0
        self.maparray:list[list[int]] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    def is_alive(self) -> bool:
        if self.stat == 1:
            return True
        if self.stat == 2:
            if self.token and self.side:
                return True
            self.stat = 9

        return False

    def stop(self):
        self.stat = 9
        try:
            if self.loop:
                self.loop.call_soon_threadsafe(self.q.put_nowait, None)
            else:
                # ループ未初期化の場合は無視（run()開始前）
                pass
        except Exception:
            pass

    def put_payload(self, payload:dict):
        if not self.is_alive():
            self.stop(); return
        try:
            if self.loop:
                self.loop.call_soon_threadsafe(self.q.put_nowait, payload)
            else:
                # ループ未初期化の極短時間は捨てる（次のbroadcastで受ける）
                pass
        except Exception:
            pass

    async def run(self):
        self.stat = 1
        # 自身のイベントループを保持
        self.loop = asyncio.get_running_loop()
        join_req:MatchJoinRequest = MatchJoinRequest(display_name=self.name)
        join_res: MatchJoinResponse = self.store.join(self.match_id, join_req)
        if not join_res.player_token:
            self.stat = 9
            return
        try:
            self.token = join_res.player_token
            self.side = join_res.side
            self.stat = 2

            snap = self.store.snapshot(self.match_id, token=self.token)
            maparray = snap.get("map")
            if maparray is not None:
                self.maparray = maparray
            while self.is_alive():
                # 次のターンを待つ
                payload = await self.q.get()
                if payload is None or not self.is_alive():
                    break
                # 終了していたら抜ける
                status = payload.get("status") # None, "active", "waiting", "over"
                result = payload.get("result") # None, "win", "lose", "draw"
                waiting_for = payload.get("waiting_for", "none") # "none", "orders", "you", "opponent"
                if result == "over" or result is not None:
                    break
                 # 自分のターンでなかったら、待つ
                if waiting_for != "orders" and waiting_for != "you":
                    continue
                # 思考して命令を出す
                if self.maparray:
                    payload["map"] = self.maparray
                await self.think(payload)
                # TODO: もし、命令を出せなかったら、対策としてエンプティーでオーダーを出すことにする
                pass
        finally:
            self.stat = 3
            # マッチから抜ける
            if self.token:
                self.store.leave(self.match_id, self.token)

    async def on_orders(self, orders: PlayerOrders) -> list[str]:
        if self.token is None:
            return ["no token. can not continue. you abort all process."]
        """思考ルーチンがオーダーを出して結果を受け取るための関数"""
        order_req = MatchOrdersRequest(player_token=self.token, player_orders=orders)
        order_res = self.store.submit_orders(self.match_id, order_req)
        if order_res.accepted:
            return []
        if order_res.logs:
            return order_res.logs
        return ["not accepted"]

    async def think(self, payload:dict) -> None:
        """思考ルーチンを実装するための抽象メソッド"""
        # AIの思考処理をここに実装
        # orderは、PlayerOrdersのインスタンスで away self.on_orders(orders) を呼び出して、メッセージがなければOK
        order = PlayerOrders()
        messages = await self.on_orders(order)
        if not messages:
            return
        # エラー対策

class AIThreadEasy(AIThreadABC):
    """簡単な思考ルーチン"""
    async def think(self, payload:dict) -> None:
        # 簡単な思考ルーチン
        order = PlayerOrders()
        messages = await self.on_orders(order)
        if not messages:
            return

class AIThreadMedium(AIThreadABC):
    """普通の思考ルーチン"""
    async def think(self, payload:dict) -> None:
        # 普通の思考ルーチン
        order = PlayerOrders()
        messages = await self.on_orders(order)
        if not messages:
            return

class AIThreadHard(AIThreadABC):
    """難しい思考ルーチン"""
    async def think(self, payload:dict) -> None:
        # 難しい思考ルーチン
        order = PlayerOrders()
        messages = await self.on_orders(order)
        if not messages:
            return
