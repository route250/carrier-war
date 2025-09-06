import unittest

from server.schemas import MatchCreateRequest
from server.services.match import MatchStore
from server.schemas import MatchJoinRequest

class TestVisibilityIntel(unittest.TestCase):
    def setUp(self):
        self.store = MatchStore()
        # Create match and join to activate both sides
        resp = self.store.create(MatchCreateRequest(mode="pvp", config=None, display_name="A"))
        self.match_id = resp.match_id
        self.token_a = resp.player_token
        join = self.store.join(self.match_id, req=MatchJoinRequest(display_name="B"))
        self.token_b = join.player_token
        self.match = self.store._matches[self.match_id]
        # 盤面が初期化されていること
        assert self.match.map is not None

    def test_sideA_viewer(self):
        # No token -> viewer_side is None; both sides' carriers should be hidden in payload
        m = self.store._matches[self.match_id]
        assert m is not None, "Match not found"
        st = m.build_state_payload(viewer_side="A")
        # オーディエンスには units（自分）/intel（相手）ともに開示されること
        assert st.units.carrier is not None, "Carrier is not shown for side-A"
        assert st.intel.squadrons is None or len(st.intel.squadrons)==0, "Squadron intel is shown for side-A"

    def test_unknown_viewer_hides_both(self):
        # No token -> viewer_side is None; both sides' carriers should be hidden in payload
        m = self.store._matches[self.match_id]
        assert m is not None, "Match not found"
        st = m.build_state_payload(viewer_side=None)
        # オーディエンスには units（自分）/intel（相手）ともに開示されること
        assert st.units.carrier is not None, "Carrier is not shown for オーディエンス"
        assert st.intel.squadrons is not None and len(st.intel.squadrons)==2, "Squadron intel is not shown for オーディエンス"


if __name__ == '__main__':
    unittest.main()
