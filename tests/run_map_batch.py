#!/usr/bin/env python3
import sys,os
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import argparse
import json
from pathlib import Path
from typing import Tuple

from server.services.session import Session, PlayerState, CarrierState, EnemyMemory, EnemyAIState
from server.services.session import _distance_field_hex, _offset_neighbors


def gradient_path(sess: Session, start: Tuple[int, int], goal: Tuple[int, int], *, pass_islands: bool, stop_range: int = 0, max_steps: int = 2000):
    dist = _distance_field_hex(
        sess,
        goal,
        pass_islands=pass_islands,
        ignore_id=None,
        player_obs=None,
        stop_range=stop_range,
        avoid_prev_pos=None,
    )
    if dist is None:
        return []
    W = len(sess.map[0]) if sess.map else 0
    H = len(sess.map)
    cx, cy = start
    path = [start]
    INF = 10 ** 9
    steps = 0
    while steps < max_steps:
        steps += 1
        if not (0 <= cx < W and 0 <= cy < H):
            break
        if dist[cy][cx] <= max(0, stop_range):
            break
        nbrs = []
        for nx, ny in _offset_neighbors(cx, cy):
            if 0 <= nx < W and 0 <= ny < H:
                nbrs.append((dist[ny][nx], nx, ny))
        nbrs.sort(key=lambda t: t[0])
        moved = False
        for dv, nx, ny in nbrs:
            if dv < dist[cy][cx] and dv < INF:
                cx, cy = nx, ny
                path.append((cx, cy))
                moved = True
                break
        if not moved:
            break
    return path


def run_map_file(path: Path, stop_range: int = 0) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return {"file": str(path), "error": "empty"}
    # first line must be type: map
    try:
        head = json.loads(lines[0])
    except Exception as e:
        return {"file": str(path), "error": f"bad json head: {e}"}
    if head.get("type") != "map":
        return {"file": str(path), "error": "no map head"}

    game_map = head["map"]
    # Build a neutral session (no units needed for path calc)
    sess = Session(
        session_id="test",
        map=game_map,
        rand_seed=None,
        config=None,
    )
    passed = 0
    total = 0
    for line in lines[1:]:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "move":
            continue
        if rec.get("side") not in ("player", "enemy"):
            continue

        sx, sy = rec.get("from", [None, None])
        gx, gy = rec.get("to", [None, None])
        if None in (sx, sy, gx, gy):
            continue
        total += 1
        # carriers avoid islands
        x0 = x1 = sx
        y0 = y1 = sy
        step = 0
        for i in range(40):
            x0, y0 = x1, y1
            from_to = gradient_path(sess, (x0, y0), (gx, gy), pass_islands=False, stop_range=stop_range)
            if not from_to:
                break
            step += 1
            (x1,y1) = from_to[1]
            print(f"step:{step:03d} current:{x0,y0} pass:{from_to} next:{x1,y1}")
            if x1 == gx and y1 == gy:
                break
        if x1 == gx and y1 == gy:
            print(f"Success 到達した!")
            passed += 1
        else:
            print(f"ERROR: 到達できなかった！")
    return {"file": str(path), "total": total, "passed": passed}


def main():

    dir = "tests/data/run_path"
    stop_range = 0

    d = Path(dir)
    maps = sorted(d.glob("*.map"))
    if not maps:
        print("No .map files found in", d)
        return

    total_cmds = 0
    total_pass = 0
    for p in maps:
        res = run_map_file(p, stop_range=stop_range)
        total_cmds += res.get("total", 0)
        total_pass += res.get("passed", 0)
        print(f"{p.name}: {res.get('passed')}/{res.get('total')} commands PASS")
    print(f"Summary: {total_pass}/{total_cmds} PASS")


if __name__ == "__main__":
    main()
