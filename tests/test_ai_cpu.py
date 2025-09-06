import unittest
import asyncio

from server.services.match import MatchStore
from server.schemas import MatchCreateRequest, MatchJoinRequest
from server.services.ai_cpu import CarrierBotMedium


class TestAICarrierBotMedium(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore()
        # PvPでマッチ作成（テスト内で手動でAIを参加させる）
        resp = self.store.create(MatchCreateRequest(mode="pvp", config=None, display_name="HUMAN"))
        self.match_id = resp.match_id
        self.match = self.store._matches[self.match_id]
        # AIボットを手動参加させる（スレッドは起動しない）
        self.bot = CarrierBotMedium(store=self.store, match_id=self.match_id)
        join = self.store.join(self.match_id, MatchJoinRequest(display_name=self.bot.name))
        self.bot.token = join.player_token
        self.bot.side = join.side

        # 初回の地形取得を確実化
        snap = self.store.snapshot(self.match_id, self.bot.token)
        # 直接thinkでもmapは取得されるが、ここで一度呼んでおく

        # 初期座標など
        self.b0 = self.match.map.get_carrier_by_side("B")
        self.a0 = self.match.map.get_carrier_by_side("A")
        assert self.b0 is not None and self.a0 is not None
        self.b_init = (self.b0.pos.x, self.b0.pos.y)
        self.a_hp0 = self.a0.hp

    def _ai_step(self):
        # AI視点のstateを作って渡す → 提出 → サーバ側を1ターン進める（人間側はオーダー無し）
        payload = self.match.build_state_payload(viewer_side=self.bot.side)
        asyncio.run(self.bot.think(payload))
        # 人間側は未提出のまま。テストでは直接ターンを進める。
        self.match._resolve_turn_minimal()
        # 次ターンへ向けてクリア
        self.match.side_a.orders = None
        self.match.side_b.orders = None

    def test_bot_launches_and_carrier_moves(self):
        launched = False
        moved = False

        for _ in range(25):
            self._ai_step()
            # B側のいずれかの編隊が発艦しているか
            for u in self.match.map.get_squadrons_by_side("B"):
                if u.state != "base" and u.state != "lost":
                    launched = True
                    break
            # B空母がどこかで動いたか
            b_now = self.match.map.get_carrier_by_side("B")
            if b_now and (b_now.pos.x, b_now.pos.y) != self.b_init:
                moved = True
            if launched and moved:
                break

        self.assertTrue(launched, "CPUが発艦（攻撃行動）しませんでした")
        self.assertTrue(moved, "CPUの空母が移動しませんでした")


if __name__ == '__main__':
    unittest.main()

