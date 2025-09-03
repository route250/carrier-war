


class HexArray:

    def __init__( self, width:int, height: int ):
        self.m = [[0 for _ in range(width)] for __ in range(height)]
    
    def dump(self):
        """キャラクタベースのヘックスマップをプリントする。
        偶数/奇数行をインデントして六角形グリッドの視覚的なズレを表現します。
        0 を海 (.)、非0 を陸 (#) として表示します。
        """
        yy = "  "
        for x, col in enumerate(self.m[0]):
            yy += f" {x:2d}"
        print(yy)
        for y, row in enumerate(self.m):
            # 奇数行を少しインデント（見やすさ向上）
            yy = f"{y:2d}: "
            prefix = "  " if y % 2 == 1 else ""
            chars = []
            for cell in row:
                chars.append(f"{cell}")
            print(yy + prefix + "  ".join(chars))
