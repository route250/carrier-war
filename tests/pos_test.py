
import sys,os
sys.path.append(os.path.dirname(os.path.join(os.path.dirname(__file__))))
from server.schemas import Position

def test_pos():
    poslist = [Position(x=1, y=2), Position(x=3, y=4)]
    posset = set(poslist)

    pos = poslist[0]

    if pos in poslist:
        print("Position found:", pos)
    
    pos1 = Position( x=pos.x+100, y=pos.y)

    if pos1 not in poslist:
        print("Position not found:", pos1)

    print(f"Position 1: {pos1}")
    pos2 = pos1.model_copy()
    print(f"Position 2: {pos2}")
    pos1.x=999
    print(f"Position 2: {pos2}")

if __name__ == "__main__":
    test_pos()