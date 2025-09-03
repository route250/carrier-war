import unittest

from server.schemas import MatchCreateRequest, Position
from server.services.match import MatchStore


class TestVisibilityIntel(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore()
        # Create match and join to activate both sides
        resp = self.store.create(MatchCreateRequest(mode="pvp", config=None, display_name="A"))
        self.match_id = resp.match_id
        self.token_a = resp.player_token
        join = self.store.join(self.match_id, req=type("_", (), {"display_name": "B"})())
        self.token_b = join.player_token
        self.match = self.store._matches[self.match_id]
        assert self.match.a_state and self.match.b_state and self.match.map is not None

    def _first_sea_neighbor_of_a(self):
        m = self.match
        a = m.a_state.carrier.pos
        W = len(m.map[0]); H = len(m.map)
        for p in a.offset_neighbors():
            if 0 <= p.x < W and 0 <= p.y < H and m.map[p.y][p.x] == 0:
                return p
        # fallback to own position if no neighbor found (should not happen due to carve_sea)
        return a

    def test_hidden_when_not_seen(self):
        # Place B far (initial) and update intel
        m = self.match
        # ensure B is far away at default spawn
        # update intel once (not seen)
        self.store._update_intel(m)
        st_a = self.store.state(self.match_id, self.token_a)
        # viewer A should not see B carrier
        self.assertIsNotNone(st_a.a)
        self.assertIsNotNone(st_a.b)
        self.assertIsNone(st_a.b.get("carrier", {}).get("x"))
        self.assertIsNone(st_a.b.get("carrier", {}).get("y"))

    def test_seen_when_in_vision(self):
        m = self.match
        # Move B into A's vision range (neighbor hex)
        near = self._first_sea_neighbor_of_a()
        m.b_state.carrier.pos = Position(x=near.x, y=near.y)
        # Update intel; A should see B now
        self.store._update_intel(m)
        st_a = self.store.state(self.match_id, self.token_a)
        bx = st_a.b.get("carrier", {}).get("x")
        by = st_a.b.get("carrier", {}).get("y")
        self.assertEqual((bx, by), (near.x, near.y))
        # And B (being next to A) should also see A
        st_b = self.store.state(self.match_id, self.token_b)
        ax = st_b.a.get("carrier", {}).get("x")
        ay = st_b.a.get("carrier", {}).get("y")
        self.assertIsNotNone(ax)
        self.assertIsNotNone(ay)

    def test_ttl_decay_hides_after_out_of_sight(self):
        m = self.match
        # First, make B visible to A
        near = self._first_sea_neighbor_of_a()
        m.b_state.carrier.pos = Position(x=near.x, y=near.y)
        self.store._update_intel(m)  # seen -> ttl reset (3)
        # Move B far away again (original spawn area is far)
        far = Position(x=len(m.map[0]) - 4, y=len(m.map) - 4)
        m.b_state.carrier.pos = far
        # Decay intel for 3 turns without seeing
        for _ in range(3):
            self.store._update_intel(m)
        st_a = self.store.state(self.match_id, self.token_a)
        self.assertIsNone(st_a.b.get("carrier", {}).get("x"))
        self.assertIsNone(st_a.b.get("carrier", {}).get("y"))

    def test_unknown_viewer_hides_both(self):
        # No token -> viewer_side is None; both sides' carriers should be hidden in payload
        st = self.store.state(self.match_id, token=None)
        self.assertIsNone(st.a.get("carrier", {}).get("x"))
        self.assertIsNone(st.b.get("carrier", {}).get("x"))


if __name__ == '__main__':
    unittest.main()

