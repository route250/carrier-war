import unittest
from server.schemas import MatchCreateRequest, Position
from server.services.match import MatchStore

import unittest
from server.schemas import MatchCreateRequest, Position
from server.services.match import MatchStore


class TestCarrierMoveAdvanced(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore()
        req = MatchCreateRequest(mode="pvp", config=None, display_name="test")
        resp = self.store.create(req)
        self.match_id = resp.match_id
        self.match = self.store._matches[self.match_id]
        assert self.match.a_state is not None, "a_stateがNoneです"
        assert self.match.a_state.carrier is not None, "carrierがNoneです"
        self.carrier = self.match.a_state.carrier
        self.init_pos = Position(x=self.carrier.pos.x, y=self.carrier.pos.y)

    def test_no_order_no_move(self):
        # 1. オーダーなしで移動しない
        self.match.side_a.orders = None
        self.match.side_b.orders = None
        self.store._resolve_turn_minimal(self.match)
        self.assertEqual((self.carrier.pos.x, self.carrier.pos.y), (self.init_pos.x, self.init_pos.y))

    def test_far_order_moves(self):
        # 2. 十分離れた地点にオーダー→移動
        target = {'x': self.init_pos.x + 5, 'y': self.init_pos.y + 5}
        orders = {'carrier_target': target}
        self.match.side_a.orders = orders
        self.match.side_b.orders = orders
        self.store._resolve_turn_minimal(self.match)
        self.assertNotEqual((self.carrier.pos.x, self.carrier.pos.y), (self.init_pos.x, self.init_pos.y))

    def test_multi_step_to_target(self):
        # 3. オーダー無しで目標地点まで複数回移動
        target = {'x': self.init_pos.x + 6, 'y': self.init_pos.y + 6}
        orders = {'carrier_target': target}
        self.match.side_a.orders = orders
        self.match.side_b.orders = orders
        reached = False
        max_turns = 10
        for _ in range(max_turns):
            self.store._resolve_turn_minimal(self.match)
            if (self.carrier.pos.x, self.carrier.pos.y) == (target['x'], target['y']):
                reached = True
                break
        self.assertTrue(reached, f"キャリアが{max_turns}ターン以内に目標に到達しませんでした: pos={(self.carrier.pos.x,self.carrier.pos.y)}")

    def test_stop_at_target(self):
        # 4. 目標地点到達後は動かない
        target = {'x': self.init_pos.x + 4, 'y': self.init_pos.y + 4}
        orders = {'carrier_target': target}
        self.match.side_a.orders = orders
        self.match.side_b.orders = orders
        for _ in range(10):
            self.store._resolve_turn_minimal(self.match)
        pos = (self.carrier.pos.x, self.carrier.pos.y)
        self.assertEqual(pos, (target['x'], target['y']))
        # さらにターン進行しても動かない
        for _ in range(3):
            self.store._resolve_turn_minimal(self.match)
            self.assertEqual((self.carrier.pos.x, self.carrier.pos.y), pos)

    def test_change_target_midway(self):
        # 5. 十分離れた地点にオーダー→途中で新しいオーダー
        target1 = {'x': self.init_pos.x + 8, 'y': self.init_pos.y + 8}
        orders1 = {'carrier_target': target1}
        self.match.side_a.orders = orders1
        self.match.side_b.orders = orders1
        for _ in range(3):
            self.store._resolve_turn_minimal(self.match)
        pos_mid = (self.carrier.pos.x, self.carrier.pos.y)
        # 6. 途中で新しいオーダー
        target2 = {'x': self.init_pos.x + 2, 'y': self.init_pos.y + 2}
        orders2 = {'carrier_target': target2}
        self.match.side_a.orders = orders2
        self.match.side_b.orders = orders2
        for _ in range(10):
            self.store._resolve_turn_minimal(self.match)
            if (self.carrier.pos.x, self.carrier.pos.y) == (target2['x'], target2['y']):
                break
        self.assertEqual((self.carrier.pos.x, self.carrier.pos.y), (target2['x'], target2['y']))

    def test_squadron_launch_and_return(self):
        # 6. 航空機を発艦させ、目標到達後に戻ってくることを確認する
        # 目標は敵の空母位置（可能ならそのまま）に指定する
        # 目標は自空母から近い地点にする（到達→戻還をテストしやすくするため）
        target = {'x': self.init_pos.x + 1, 'y': self.init_pos.y}
        orders = {'launch_target': target}
        # 両者に同じオーダーを設定してターン解決を回す
        self.match.side_a.orders = orders
        self.match.side_b.orders = orders
        # 1ターン目で発艦するはず
        self.store._resolve_turn_minimal(self.match)
        sq = next((s for s in self.match.a_state.squadrons if s.state != 'base'), None)
        self.assertIsNotNone(sq, "発艦した航空機が存在しません")
        # ある程度ターンを進めて、最終的に基地に戻ることを確認（失われた場合は失敗）
        max_turns = 50
        returned = False
        for _ in range(max_turns):
            self.match.side_a.orders = None
            self.match.side_b.orders = None
            self.store._resolve_turn_minimal(self.match)
            if sq.state == 'base' and not sq.is_active():
                returned = True
                break
            if sq.state == 'lost':
                break
        self.assertTrue(returned, f"航空機が{max_turns}ターン内に基地に戻りませんでした (最終状態: {sq.state})")


if __name__ == '__main__':
    unittest.main()
