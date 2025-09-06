

from abc import ABC
from queue import Queue
from server.schemas import MatchJoinRequest, MatchJoinResponse, MatchStatePayload, PlayerOrders, MatchStateResponse, MatchOrdersRequest, MatchOrdersResponse
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
        self.q: Queue[MatchStatePayload|None] = Queue() # タイミング通知用のキュー（自身のイベントループ専用）
        self.stat:int = 0
        self.maparray:list[list[int]] = []

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
        self.q.queue.clear()
        self.q.put_nowait(None)

    def put_payload(self, payload:MatchStatePayload):
        if not self.is_alive():
            self.stop(); return
        self.q.put_nowait(payload)

    def run(self):
        self.stat = 1
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
            maparray = snap.map
            if maparray is not None:
                self.maparray = maparray
            while self.is_alive():
                # 次のターンを待つ
                payload = self.q.get()
                if payload is None or not self.is_alive():
                    break
                # 終了していたら抜ける
                status = payload.status # None, "active", "waiting", "over"
                result = payload.result # None, "win", "lose", "draw"
                waiting_for = payload.waiting_for or "none" # "none", "orders", "you", "opponent"
                if result == "over" or result is not None:
                    break
                 # 自分のターンでなかったら、待つ
                if waiting_for != "orders" and waiting_for != "you":
                    continue
                # 思考して命令を出す
                if self.maparray:
                    payload.map = self.maparray
                self.think(payload)
                # TODO: もし、命令を出せなかったら、対策としてエンプティーでオーダーを出すことにする
                pass
        finally:
            self.stat = 3
            # マッチから抜ける
            if self.token:
                self.store.leave(self.match_id, self.token)

    def on_orders(self, orders: PlayerOrders) -> list[str]:
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

    def think(self, payload:MatchStatePayload) -> None:
        """思考ルーチンを実装するための抽象メソッド"""
        # AIの思考処理をここに実装
        # orderは、PlayerOrdersのインスタンスで away self.on_orders(orders) を呼び出して、メッセージがなければOK
        order = PlayerOrders()
        messages = self.on_orders(order)
        if not messages:
            return
        # エラー対策

class AIThreadEasy(AIThreadABC):
    """簡単な思考ルーチン"""
    def think(self, payload:MatchStatePayload) -> None:
        # 簡単な思考ルーチン
        order = PlayerOrders()
        messages = self.on_orders(order)
        if not messages:
            return

class AIThreadMedium(AIThreadABC):
    """普通の思考ルーチン"""
    def think(self, payload:MatchStatePayload) -> None:
        # 普通の思考ルーチン
        order = PlayerOrders()
        messages = self.on_orders(order)
        if not messages:
            return

class AIThreadHard(AIThreadABC):
    """難しい思考ルーチン"""
    def think(self, payload:MatchStatePayload) -> None:
        # 難しい思考ルーチン
        order = PlayerOrders()
        messages = self.on_orders(order)
        if not messages:
            return
