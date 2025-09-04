import pytest
from pathlib import Path
from server.services.hexmap import HexArray, generate_connected_map
from server.schemas import Position, INF


def test_get_and_getitem():
    h = HexArray(3, 2)
    h.set(2, 1, 5)
    pos = Position(x=2, y=1)
    assert h.get(2,1) == 5
    assert h[pos] == 5
    with pytest.raises(TypeError):
        _ = h[(1, 1)] # type: ignore


def test_distance_and_hex_distance():
    a = Position(x=0, y=0)
    b = Position(x=2, y=0)
    h = HexArray(5, 5)
    assert h.distance(a, b) == a.hex_distance(b)
    with pytest.raises(TypeError):
        h.distance((0, 0), b) # type: ignore


def test_gradient_field_and_path_basic():
    # 5x5 全て海のマップ
    h = HexArray(5, 5)
    goal = Position(x=2, y=2)
    dist = h.gradient_field(goal)
    # 目標地点は0
    assert dist[2][2] == 0
    # 距離は増加する
    assert dist[2][1] == 1 or dist[1][2] == 1
    # path from corner to center
    start = Position(x=0, y=0)
    path = h.gradient_path(start, goal)
    assert path[0] == start
    assert path[-1].hex_distance(goal) == 0


def test_validate_sea_connectivity():
    # 手で陸を作って海が分断するケース
    h = HexArray(5, 5)
    # 横に陸の壁を作る
    for x in range(h.W):
        h.set(x,2,1)
    assert not h.validate_sea_connectivity()
    h.set(2,2,0)
    assert h.validate_sea_connectivity()

def test_generate_connected_map():
    h = HexArray(5, 7)
    assert 5 == h.W
    assert 7 == h.H
    generate_connected_map(h, blobs=5, seed=42)
    assert h.validate_sea_connectivity()

def test_neighbors_by_gradient_ordering():
    h = HexArray(5, 5)
    # place a simple target
    goal = Position(x=4, y=2)
    start = Position(x=2, y=2)
    nbrs = h.neighbors_by_gradient(start, goal)
    # 6 neighbors returned (some may be out of bounds filtered)
    assert isinstance(nbrs, list)
    if nbrs:
        # best neighbor should be closer to goal than start
        assert nbrs[0].hex_distance(goal) <= start.hex_distance(goal)


def test_find_path_respects_obstacles():
    h = HexArray(5, 5)
    # place a wall blocking direct path
    for x in range(1, h.W-1):
        h.set(x,2,1)
    start = Position(x=0, y=2)
    goal = Position(x=4, y=2)
    # a path around the obstacle should exist (via neighboring rows)
    path = h.find_path(start, goal)
    assert path is not None
    assert path[0] == start
    # allow ignoring land (also should find a path)
    path2 = h.find_path(start, goal, ignore_land=True)
    assert path2 is not None
    assert path2[0] == start


def test_gradient_field_all_sea_explicit_map():
    # 7x5 map (w=7,h=5) 全て海 (0)
    W, H = 7, 5
    h = HexArray(W, H)
    # place goal near center
    goal = Position(x=3, y=2)
    dist = h.gradient_field(goal)
    # center is zero
    assert dist[2][3] == 0
    # check a few known hex distances: use Position.hex_distance for ground truth
    pairs = [((3,2),(3,2)), ((2,2),(3,2)), ((1,2),(3,2)), ((3,0),(3,2)), ((6,4),(3,2))]
    for (sx, sy), (gx, gy) in pairs:
        p = Position(x=sx, y=sy)
        expected = p.hex_distance(Position(x=gx, y=gy))
        assert dist[sy][sx] == expected


def test_gradient_field_single_land_obstacle():
    # 7x5 map with a single land tile that should be impassable when ignore_land=False
    W, H = 7, 5
    h = HexArray(W, H)
    # put a single land tile between start and goal
    h.set(4,2,1)
    goal = Position(x=5, y=2)
    # when not ignoring land, tiles on the land should be INF/unreachable for the wave origin
    dist = h.gradient_field(goal, ignore_land=False)
    # land tile remains INF (cannot stand on it)
    assert dist[2][4] == INF
    # neighboring sea tile distances reflect shortest hex distance avoiding land origins
    # compare with gradient_field(ignore_land=True) which treats land as sea
    dist_ignore = h.gradient_field(goal, ignore_land=True)
    assert dist_ignore[2][4] == 1  # adjacent when ignoring land
    # ensure goal remains zero in both cases
    assert dist[goal.y][goal.x] == 0
    assert dist_ignore[goal.y][goal.x] == 0


def test_write_svg_to_tmp():
    # create a small map, draw SVG and write to tmp_path
    h = HexArray(5, 5)
    # put some land for visual variety
    h.set(2,1,1)
    h.set(3,2,1)
    svg = h.draw(hex_size=16, show_coords=True)
    out = Path("tmp/map.svg")
    out.write_text(svg, encoding="utf-8")
    assert out.exists()
    assert out.stat().st_size > 0


def test_draw_with_values_sequential():
    # create small map and fill values with sequential integers
    W, H = 6, 4
    h = HexArray(W, H)
    # make a few land tiles for variety
    h.set(2,1,1)
    h.set(3,2,1)
    # prepare sequential values 0..W*H-1
    values = [[y * W + x for x in range(W)] for y in range(H)]
    svg = h.draw(hex_size=18, show_coords=True, values=values)
    out = Path("tmp/map_values.svg")
    out.write_text(svg, encoding="utf-8")
    assert out.exists()
    assert out.stat().st_size > 0
