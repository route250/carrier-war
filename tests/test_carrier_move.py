import unittest
from server.schemas import MatchCreateRequest
from server.services.match import MatchStore

class TestCarrierMove(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore()
        req = MatchCreateRequest(mode="pvp", config=None, display_name="test")
        resp = self.store.create(req)
        self.match_id = resp.match_id
        self.match = self.store._matches[self.match_id]
        # a_stateが初期化されるまで待つ
        assert self.match.a_state is not None
        self.init_x = self.match.a_state.carrier.pos.x
        self.init_y = self.match.a_state.carrier.pos.y

    def test_apply_carrier_move(self):
        # オーダー作成（空母を右下に移動）
        target = {'x': self.init_x + 2, 'y': self.init_y + 2}
        orders = {'carrier_target': target}
        self.match.side_a.orders = orders
        self.match.side_b.orders = orders  # 両方揃えないと進行しない
        # ターン進行
        self.store._resolve_turn_minimal(self.match)
        # carrierがNoneでないことを確認
        assert self.match.a_state is not None, "a_stateがNoneです"
        assert hasattr(self.match.a_state, "carrier"), "a_stateにcarrier属性がありません"
        assert self.match.a_state.carrier is not None, "carrierがNoneです"
        new_x = self.match.a_state.carrier.pos.x
        new_y = self.match.a_state.carrier.pos.y
        self.assertNotEqual((self.init_x, self.init_y), (new_x, new_y), "座標が更新されていません")
        print(f"初期座標: ({self.init_x},{self.init_y}) → 新座標: ({new_x},{new_y})")

if __name__ == '__main__':
    unittest.main()
