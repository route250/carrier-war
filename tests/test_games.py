import sys,os
if __name__ == "__main__":
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from server.schemas import SessionStepRequest, PlayerOrders
from server.schemas import Position, UnitState, CarrierState, SquadronState, IntelPath, IntelReport
from server.services.hexmap import HexArray

from server.services.turn import GameBord

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

    eorders = [
        PlayerOrders(
            carrier_target=None,
            launch_target=None
        ),
        PlayerOrders(
            carrier_target=None,
            launch_target=None
        )
    ]

    # result = board.turn_forward(eorders)

    # for side, report in result.items():
    #     print(f"--- Logs for side {side} ---")
    #     for entry in report.dump():
    #         print(entry)
    #     print()

    orders = [
        PlayerOrders(
            carrier_target=None,
            launch_target=Position(x=4,y=4)
        ),
        PlayerOrders(
            carrier_target=None,
            launch_target=None
        )
    ]
    for _ in range(5):
        result = board.turn_forward(orders)
        orders = eorders
        for side, report in result.items():
            print(f"--- Logs for side {side} ---")
            for entry in report.dump():
                print(entry)
            print()

if __name__ == "__main__":
    gamex()