import sys,os
if __name__ == "__main__":
    sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

from server.schemas import Position
from server.services.hexmap import HexArray
from server.services.session import Session
import server.services.session as session

def test():
    W = 4
    H = 4
    map = HexArray(W, H)
    assert map.m == [[0 for _ in range(W)] for __ in range(H)]
    print("Initial map:")
    map.dump()

    tmp_sess = Session(session_id="tmp", map=map.m)
    goal = Position(x=0, y=0)
    dist = session._distance_field_hex(
        tmp_sess,
        goal,
        pass_islands=True,
        ignore_id=None,
        player_obs=None,
        stop_range=0,
        avoid_prev_pos=None,
        consider_occupied=False,
    )
    assert dist is not None

    dstmap = HexArray(3,3)
    dstmap.m = dist
    print("Distance map:")
    dstmap.dump()


if __name__ == "__main__":
    test()