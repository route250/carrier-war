import sys,os
if __name__ == "__main__":
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from server.schemas import PlayerOrders
from server.schemas import Position, UnitState, CarrierState, SquadronState
from server.services.hexmap import HexArray

from server.services.turn import GameBord, IntelReport

from server.services.match import create_units

EMPTY_ORDER = [
    PlayerOrders(
        carrier_target=None,
        launch_target=None
    ),
    PlayerOrders(
        carrier_target=None,
        launch_target=None
    )
]

def gamex():

    hexmap = HexArray(5,5)
    hexmap.set_map([
        [0,0,0,0,0],
         [0,1,1,0,0],
        [0,1,1,1,0],
         [0,1,0,1,0],
        [0,0,0,0,0],
    ])

    units_list = [
        [
            CarrierState(side="A", id="C1", pos=Position(x=0,y=0)),
            SquadronState(side="A", id="S1")
        ],
        [
            CarrierState(side="B", id="E2", pos=Position(x=4,y=4)),
            SquadronState(side="B", id="ES2")
        ]
    ]
    board = GameBord(hexmap, units_list)



    result = board.turn_forward(EMPTY_ORDER)

    for side, report in result.items():
        print(f"--- Logs for side {side} ---")
        for entry in report.dump(board):
            print(entry)
        print()

    orders = [
        PlayerOrders(
            carrier_target=None,
            launch_target=Position(x=4,y=4)
        ),
        PlayerOrders(
            carrier_target=Position(x=0,y=0),
            launch_target=None
        )
    ]
    for _ in range(5):
        result = board.turn_forward(orders)
        orders = EMPTY_ORDER
        for side, report in result.items():
            print(f"--- Logs for side {side} ---")
            for entry in report.dump(board):
                print(entry)
            print()

def test_moving_step():
    # 30x30のマップを作成
    W = 7
    H = 7
    hexmap = HexArray(W,H)
    a_units = create_units("A", 0,0 )
    b_units = create_units("B", 0, H-1 )
    bord = GameBord(hexmap, [a_units, b_units], log_id='debug' )
    # マップが全て0であることを確認
    assert all(all(cell == 0 for cell in row) for row in hexmap.copy_as_list()), "マップが全て0ではありません"
    # 最初は航空部隊はbase状態のはず
    for u in a_units:
        if isinstance(u, SquadronState):
            assert u.state == 'base', "航空部隊が最初にbase状態ではありません"
    c1 = next(u for u in a_units if isinstance(u, CarrierState))
    c1.pos = Position(x=0,y=0) # GameBordの初期化で変わってしまうので、改めて位置を設定
    c1.speed = 1 # デバッグのために速度を1に設定
    c2 = next(u for u in b_units if isinstance(u, CarrierState))
    c2.pos = Position(x=0,y=H-1) # GameBordの初期化で変わってしまうので、改めて位置を設定

    target_pos = Position(x=W-1,y=H-1)

    orders = [
        PlayerOrders(
            carrier_target=target_pos,
            launch_target=None
        ),
        PlayerOrders(
            carrier_target=None,
            launch_target=None
        )
    ]
    moveing_path = [c1.pos]
    for i in range(1,35):
        assert i == bord.turn, f"ターン番号が不正です: {bord.turn} != {i}"
        result = bord.turn_forward(orders)
        orders = EMPTY_ORDER
        moveing_path.append(c1.pos)
        if c1.pos == target_pos:
            break

    actual_path = [ (0,0),(0,1),(1,2),(2,2),(2,3),(3,4),(4,4),(4,5),(5,5),(6,6)]
    err = 0
    for (x,y),pos in zip(actual_path,moveing_path):
        print(f"({x},{y})  ({pos.x},{pos.y})")
        if x != pos.x or y != pos.y:
            err += 1
    assert err == 0, f"移動経路が不正です"


def test_squadron_return():
    # 航空部隊が、敵を発見できない場合にちゃんと帰ってくるか？

    # 30x30のマップを作成
    hexmap = HexArray(30,30)
    a_units = create_units("A", 3,3 )
    b_units = create_units("B", 27,27 )
    board = GameBord(hexmap, [a_units, b_units], log_id='debug' )
    # マップが全て0であることを確認
    assert all(all(cell == 0 for cell in row) for row in hexmap.copy_as_list()), "マップが全て0ではありません"
    # 最初は航空部隊はbase状態のはず
    for u in a_units:
        if isinstance(u, SquadronState):
            assert u.state == 'base', "航空部隊が最初にbase状態ではありません"
    c1 = next(u for u in a_units if isinstance(u, CarrierState))
    c1.pos = Position(x=3,y=3) # GameBordの初期化で変わってしまうので、改めて位置を設定
    c2 = next(u for u in b_units if isinstance(u, CarrierState))
    c2.pos = Position(x=27,y=27) # GameBordの初期化で変わってしまうので、改めて位置を設定

    sq1 = next(u for u in a_units if isinstance(u, SquadronState))
    target_pos = Position(x=27,y=3)

    orders = [
        PlayerOrders(
            carrier_target=Position(x=3,y=27),
            launch_target=target_pos
        ),
        PlayerOrders(
            carrier_target=None,
            launch_target=None
        )
    ]
    a = 0
    for i in range(1,35):
        assert i == board.turn, f"ターン番号が不正です: {board.turn} != {i}"
        result = board.turn_forward(orders)
        orders = EMPTY_ORDER
        for side, report in result.items():
            print(f"--- Logs for side {side} ---")
            for entry in report.dump(board):
                print(entry)
            print()
        if a==0 and sq1.pos == target_pos:
            a = 1
        if a==1 and sq1.state == 'returning':
            a = 2
        if a == 0:
            # 目標に到達するまでは、outbound状態のはず
            assert sq1.state == 'outbound', "航空部隊がoutbound状態ではありません"
        elif a == 1:
            # 目標に到達したら、帰還状態に変わっているはず
            assert sq1.state == 'returning', "航空部隊がreturning状態ではありません"
        elif a == 2:
            if sq1.state == 'base':
                a = 3
                break
            else:
                # 帰還中はreturning状態のはず
                assert sq1.state == 'returning', "航空部隊がreturning状態ではありません"
    assert a == 3, "航空部隊が帰還していない"

if __name__ == "__main__":
    test_moving_step()
    # test_squadron_return()