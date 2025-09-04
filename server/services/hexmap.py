
from typing import overload
from server.schemas import INF, Position

def generate_connected_map(map: 'HexArray', blobs: int = 10, seed: int|None = None) -> None:
    """
    海・陸のblobをランダム配置し、全海タイルが到達可能な地形を生成する。
    hexarray.mを書き換える。
    """
    import random
    r = random.Random(seed)
    W, H = map.W, map.H
    for _attempt in range(60):
        new_map = [[0 for _ in range(W)] for __ in range(H)]
        for _ in range(blobs):
            cx = r.randint(2, max(2, W - 3))
            cy = r.randint(2, max(2, H - 3))
            rad = r.randint(1, 3)
            for dy in range(-rad, rad + 1):
                for dx in range(-rad, rad + 1):
                    if dx * dx + dy * dy <= rad * rad:
                        x = max(0, min(W - 1, cx + dx))
                        y = max(0, min(H - 1, cy + dy))
                        new_map[y][x] = 1
        map.set_map(new_map)
        if map.validate_sea_connectivity():
            return

class HexArray:
    """
    ヘックスマップを2次元配列で表現するクラス。
    各セルは整数値を持ち、0は海、非0は陸を表す。
    """
    def __init__(self, width: int, height: int):
        self.__map = [[0 for _ in range(width)] for __ in range(height)]
        self.__W = width
        self.__H = height

    def set_map(self, values: list[list[int]]):
        if not isinstance(values, list) or not all(isinstance(row, list) for row in values):
            raise ValueError("m must be a 2D list")
        H = len(values)
        W = len(values[0]) if H > 0 else 0
        if any(len(row) != W for row in values):
            raise ValueError("All rows in m must have the same length")
        self.__map = values
        self.__W = W
        self.__H = H

    @property
    def W(self) -> int:
        return self.__W

    @property
    def H(self) -> int:
        return self.__H

    @property
    def shape(self) -> tuple[int, int]:
        return (self.W, self.H)

    def copy_as_list(self) -> list[list[int]]:
        return [row[:] for row in self.__map]

    def get(self, x:int, y:int,) -> int:
        """指定された位置のセルの値を取得する。"""
        if not (0 <= x < self.W and 0 <= y < self.H):
            raise IndexError("Coordinates out of bounds")
        return self.__map[y][x]

    def set(self, x:int, y:int, value:int) -> None:
        if not (0 <= x < self.W and 0 <= y < self.H):
            raise IndexError("Coordinates out of bounds")
        self.__map[y][x] = value

    def __getitem__(self, pos: Position) -> int:
        if not isinstance(pos, Position):
            raise TypeError(f"HexArray indices must be Position, not {type(pos).__name__}")
        if not (0 <= pos.x < self.W and 0 <= pos.y < self.H):
            raise IndexError("Coordinates out of bounds")
        return self.__map[pos.y][pos.x]

    def __setitem__(self, pos:Position, value:int ):
        """指定された位置のセルの値を設定する。"""
        if not isinstance(pos, Position):
            raise TypeError(f"pos must be Position, not {type(pos).__name__}")
        if not (0 <= pos.x < self.W and 0 <= pos.y < self.H):
            raise IndexError("Coordinates out of bounds")
        self.__map[pos.y][pos.x] = value

    def gradient_field(self, goal: Position, ignore_land: bool = False, stop_range: int = 0) -> list:
        """
        グラデーション波形の距離フィールドを計算して2次元リストで返す。
        goal: 目標位置
        ignore_land: Trueなら陸地を無視
        stop_range: ゴール判定範囲
        """
        W = self.W
        H = self.H
        dist = [[INF for _ in range(W)] for __ in range(H)]
        def passable(pos: Position):
            if not (0 <= pos.x < W and 0 <= pos.y < H):
                return False
            if not ignore_land and self.__map[pos.y][pos.x] != 0:
                return False
            return True
        gx, gy = goal.x, goal.y
        from collections import deque
        q = deque()
        R = max(0, int(stop_range))
        for y in range(max(0, gy - (R + 2)), min(H, gy + (R + 3))):
            for x in range(max(0, gx - (R + 2)), min(W, gx + (R + 3))):
                xy = Position.new(x, y)
                if goal.hex_distance(xy) <= R and passable(xy):
                    dist[y][x] = 0
                    q.append(xy)
        if not q:
            if passable(goal):
                dist[goal.y][goal.x] = 0
                q.append(goal)
            else:
                return dist
        while q:
            cp = q.popleft()
            cd = dist[cp.y][cp.x]
            for np in cp.offset_neighbors():
                if not passable(np):
                    continue
                nd = cd + 1
                if dist[np.y][np.x] > nd:
                    dist[np.y][np.x] = nd
                    q.append(np)
        return dist

    def validate_sea_connectivity(self) -> bool:
        """
        全ての海タイルが互いに到達可能か検証する。戻り値: (ok, sea_total, sea_reached)
        """
        # 海の任意の一点を探す
        sea_pos = None
        for y in range(self.H):
            for x in range(self.W):
                if self.__map[y][x] == 0:
                    sea_pos = Position(x=x, y=y)
        if sea_pos is None:
            # 海が存在しない場合はダメ
            return False
        # 距離フィールドを計算
        dist = self.gradient_field(sea_pos, ignore_land=False, stop_range=0)
        # 全ての海タイルが到達可能か確認
        for y in range(self.H):
            for x in range(self.W):
                if self.__map[y][x] == 0 and dist[y][x] == INF:
                    return False
        return True

    def gradient_path(self, start: Position, goal: Position, ignore_land: bool = False, max_steps: int = 5000) -> list:
        """
        グラデーション波形距離フィールドを使ってstartからgoalまでのパスを復元する。
        neighbors_by_gradientで進行方向を選択。
        Positionのみで処理し、範囲外はneighbors_by_gradientで除外済み前提。
        """
        if self.W == 0 or self.H == 0:
            return [start]
        dist = self.gradient_field(goal, ignore_land=ignore_land, stop_range=0)
        pos = start
        path = [pos]
        steps = 0
        while steps < max_steps:
            dcur = dist[pos.y][pos.x]
            if dcur == 0 or dcur >= INF:
                break
            nbrs = self.neighbors_by_gradient(pos, goal, ignore_land=ignore_land)
            if not nbrs:
                break
            next_pos = nbrs[0]
            path.append(next_pos)
            pos = next_pos
            steps += 1
        return path

    def neighbors_by_gradient(self, start: Position, goal: Position, ignore_land: bool = False) -> list[Position]:
        """
        startの周囲6方向のPositionを、goalへの距離が近い順に並べて返す。
        距離が同じ場合はgoal方向との角度差（絶対値）が小さい順で優先。
        """
        import math
        dist = self.gradient_field(goal, ignore_land=ignore_land, stop_range=0)
        base_angle = start.angle_to(goal)
        neighbors = []
        for npos in start.offset_neighbors():
            if 0 <= npos.x < self.W and 0 <= npos.y < self.H:
                d = dist[npos.y][npos.x]
                a = npos.angle_to(goal)
                delta = abs((a - base_angle + math.pi) % (2 * math.pi) - math.pi)
                neighbors.append((d, delta, npos))
        neighbors.sort()
        return [npos for _, _, npos in neighbors]

    def distance(self, start: Position, goal: Position, ignore_land: bool = False) -> int:
        """
        起点と目標をPositionで受け取り、距離をintで返す。
        ignore_land=Trueなら陸地を無視（現状は未実装、必要なら地形判定を追加）
        """
        if not isinstance(start, Position) or not isinstance(goal, Position):
            raise TypeError("start/goal must be Position")
        return start.hex_distance(goal)

    def find_path(self, start: Position, goal: Position, ignore_land: bool = False, stop_range: int = 0, max_expand: int = 4000) -> list|None:
        """
        できたら使わないようにする!
        A*によるパス探索。地形のみ考慮。ignore_land=Trueなら陸地を無視。
        stop_range: ゴール判定範囲
        """
        W = self.W
        H = self.H
        if W == 0 or H == 0:
            return None
        def in_bounds(x, y):
            return 0 <= x < W and 0 <= y < H
        def passable(pos: Position):
            if not in_bounds(pos.x, pos.y):
                return False
            if not ignore_land and self.__map[pos.y][pos.x] != 0:
                return False
            return True
        if not passable(start):
            return None
        if start.hex_distance(goal) <= max(0, stop_range):
            return [start]
        import heapq
        open_heap = []
        heapq.heappush(open_heap, (0 + start.hex_distance(goal), 0, start))
        came_from = {start: None}
        g_score = {start: 0}
        closed = set()
        expands = 0
        while open_heap and expands < max_expand:
            f, g, pos = heapq.heappop(open_heap)
            if pos in closed:
                continue
            closed.add(pos)
            expands += 1
            if pos.hex_distance(goal) <= max(0, stop_range):
                path = [pos]
                cur = pos
                while cur and came_from[cur] is not None:
                    cur = came_from[cur]
                    if cur:
                        path.append(cur)
                path.reverse()
                return path
            for npos in pos.offset_neighbors():
                if not passable(npos):
                    continue
                tentative = g + 1
                if tentative < g_score.get(npos, 1e9):
                    g_score[npos] = tentative
                    came_from[npos] = pos
                    h = npos.hex_distance(goal)
                    heapq.heappush(open_heap, (tentative + h, tentative, npos))
        return None

    def dump(self):
        """キャラクタベースのヘックスマップをプリントする。
        偶数/奇数行をインデントして六角形グリッドの視覚的なズレを表現します。
        0 を海 (.)、非0 を陸 (#) として表示します。
        """
        yy = "  "
        for x, col in enumerate(self.__map[0]):
            yy += f" {x:2d}"
        print(yy)
        for y, row in enumerate(self.__map):
            # 奇数行を少しインデント（見やすさ向上）
            yy = f"{y:2d}: "
            prefix = "  " if y % 2 == 1 else ""
            chars = []
            for cell in row:
                chars.append(f"{cell}")
            print(yy + prefix + "  ".join(chars))

    def draw(self, *,
            hex_size: int = 20, show_coords: bool = True, values: list[list[int]]|None = None,
            sea_color: str = "#9dd3ff", land_color: str = "#c98f4b", stroke_color: str = "#444444",
        ) -> str:
        """
        SVG 文字列でマップを描画して返す。
        hex_size: 六角形の半径(px)
        sea_color, land_color, stroke_color: CSS カラー文字列
        show_coords: 各セル中央に座標を表示するか

        レイアウトは pointy-top のオフセット行 (odd-r) を採用。
        static/main.js と同一の座標系/計算式（odd-r offset + axial ベース）に合わせる。
        """
        import math

        # helper: compute polygon points for a hex at pixel center (cx, cy)
        def hex_corners(cx, cy, r):
            pts = []
            for i in range(6):
                angle = math.pi / 180 * (60 * i - 30)  # pointy top
                x = cx + r * math.cos(angle)
                y = cy + r * math.sin(angle)
                pts.append((x, y))
            return pts

        rows = self.H
        cols = self.W
        r = float(hex_size)
        SQRT3 = math.sqrt(3.0)
        ORIGIN_X = r
        ORIGIN_Y = r
        svg_width = SQRT3 * r * (cols + 0.5)
        svg_height = 1.5 * r * (rows - 1) + 2 * r

        parts = []
        parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_width:.0f}" height="{svg_height:.0f}" viewBox="0 0 {svg_width:.2f} {svg_height:.2f}">')
        parts.append(f'<rect width="100%" height="100%" fill="white"/>')

        # label font scaled to hex size
        label_font = max(6, int(r / 3))

        for y in range(rows):
            for x in range(cols):
                # compute center for odd-r pointy-top offset (client parity/式に完全一致)
                cx = r * (SQRT3 * (x + 0.5 * (y & 1))) + ORIGIN_X
                cy = r * (1.5 * y) + ORIGIN_Y

                pts = hex_corners(cx, cy, r)
                pts_str = " ".join(f'{px:.2f},{py:.2f}' for px, py in pts)
                cell = self.__map[y][x]
                color = sea_color if cell == 0 else land_color
                parts.append(f'<polygon points="{pts_str}" fill="{color}" stroke="{stroke_color}" stroke-width="1"/>')
                # If values provided, validate shape and render value inside hex (centered).
                if values is not None:
                    try:
                        v = values[y][x]
                    except Exception:
                        raise ValueError("values must be a 2D list with same shape as self.map")
                    # render value at hex center
                    value_y = cy + (r*0.2)
                    parts.append(
                        f'<text x="{cx:.2f}" y="{value_y:.2f}" font-size="{label_font}" text-anchor="middle" fill="#111" font-family="monospace" pointer-events="none" dominant-baseline="middle">{v}</text>'
                    )
                if show_coords:
                    # place coordinate label slightly below the top edge of the hex
                    # top edge y is approximately cy - r
                    # move it down a bit so it sits just under the edge (0.55..0.7 of r)
                    top_y = cy - r
                    label_y = top_y + (r * 0.6)
                    # small font and vertically centered via dominant-baseline
                    parts.append(
                        f'<text x="{cx:.2f}" y="{label_y:.2f}" font-size="{label_font}" text-anchor="middle" fill="#111" font-family="monospace" pointer-events="none" dominant-baseline="middle">{x},{y}</text>'
                    )

        parts.append('</svg>')
        return "\n".join(parts)
