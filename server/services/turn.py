
from server.schemas import SessionStepRequest, PlayerOrders
from server.schemas import Position, UnitState, CarrierState, SquadronState, IntelPath, IntelReport
from server.schemas import SQUAD_MAX_HP, CARRIER_MAX_HP
from server.services.hexmap import HexArray
from server.utils.audit import match_write
import random
import os
import sys

# Debug flag: enable when running tests or when env var CARRIER_WAR_DEBUG is set
DEBUG = bool(os.getenv('CARRIER_WAR_DEBUG')) or ('unittest' in sys.modules) or ('PYTEST_CURRENT_TEST' in os.environ)

def _dbg(log_id: str | None, *args, **kwargs):
    """Debug helper: prints when DEBUG, always writes to match log.

    - print: 環境変数/テスト時のみ
    - file: `match_write` へ `{"type":"debug","msg":...}` を常に出力（best-effort）
    """
    if DEBUG:
        print(*args, **kwargs)
    try:
        msg = " ".join(str(a) for a in args)
        match_write(log_id, {"type": "debug", "msg": msg})
    except Exception:
        pass

class UnitHolder:
    def __init__(self, side, unit: UnitState):
        self.side = side
        self.unit:UnitState = unit
        self.ticks:int = 0
        self.next_time:int = 0
        #
        self.path:list[Position] = [unit.pos] if unit.is_active() else [] # 移動履歴
        self.intel:dict[int,Position] = {}  # 敵に発見された時刻と位置(敵側への報告用)

    def reset(self):
        self.ticks = 0
        self.next_time = 0
        self.path = [self.unit.pos] if self.unit.is_active() else []
        self.intel = {}

    def to_payload(self, side:str|None) -> dict|None:
        if side is None or side == self.side:
            result = {
                'id': self.unit.id,
                'hp': self.unit.hp,
                'max_hp': self.unit.max_hp,
            }
            if isinstance(self.unit, SquadronState):
                result.update({
                    'state': self.unit.state,
                })
            result.update({
                'x': self.unit.pos.x if self.unit.is_active() else None,
                'y': self.unit.pos.y if self.unit.is_active() else None,})
            if self.unit.is_active():
                if self.path:
                    result.update({
                        'x0': self.path[0].x,
                        'y0': self.path[0].y,
                    })
                if self.unit.target:
                    result.update({
                        'target': {'x': self.unit.target.x, 'y': self.unit.target.y}
                    })
            return result
        elif self.intel:
            poist_list = [p for t,p in sorted(self.intel.items())]
            first_seen = poist_list[0]
            last_seen = poist_list[-1]
            result = {
                'id': self.unit.id,
                'hp': self.unit.hp,
                'max_hp': self.unit.max_hp,
                'x': last_seen.x,
                'y': last_seen.y,
            }
            result.update({
                'x0': first_seen.x,
                'y0': first_seen.y,
            })
            return result


def next_step( hexmap:HexArray, units: list[UnitHolder], current: Position, target: Position, *, ignore_land:bool = False) -> Position|None:
    for pos in hexmap.neighbors_by_gradient(current, target, ignore_land=ignore_land):
        if all( not ou.unit.is_active() or pos != ou.unit.pos for ou in units):
            return pos
    return None

# 攻撃判定: 編隊からのダメージと、空母からの対空(AA)
def scaled_damage(hp: int, max_hp:int, base: int) -> int:
    hp = hp if hp is not None else max_hp
    scale = max(0.0, min(1.0, hp / float(max_hp)))
    variance = round(base * 0.2)
    raw = base + (0 if variance == 0 else random.randint(-variance, variance))
    return max(0, round(raw * scale))

class GameBord:
    def __init__(self, hexmap: HexArray, units_list:list[list[UnitState]], *, log_id: str | None = None):
        if len(units_list) == 0:
            raise ValueError("Units list and orders list must have the same length.")
        if len(units_list) != 2:
            raise ValueError("This game only supports 2 players.")
        if hexmap is None:
            raise ValueError("Map cannot be None.")

        self.turn:int = 1
        self.hexmap = hexmap
        self.units_list:list[UnitHolder] = []
        self.log_id: str | None = log_id
        for side, bbb in zip(["A","B"], units_list):
            for unit in bbb:
                if isinstance(unit, CarrierState):
                    unit.target = self.get_start_position(unit.pos)
                self.units_list.append(UnitHolder(side, unit))
        self.intel: dict[str,IntelReport] = {"A":IntelReport(side="A",turn=0), "B":IntelReport(side="B",turn=0)}
        _dbg(self.log_id, f"[GameBord] init W={self.W} H={self.H} units={len(self.units_list)}")
        # bootstrap log (best-effort)
        match_write(self.log_id, {
            "type": "match_bootstrap",
            "map_w": self.W,
            "map_h": self.H,
            "units": [
                {
                    "side": u.side,
                    "id": u.unit.id,
                    "type": ("carrier" if isinstance(u.unit, CarrierState) else "squadron"),
                    "pos": [u.unit.pos.x, u.unit.pos.y] if u.unit.is_active() else None,
                    "hp": u.unit.hp,
                }
                for u in self.units_list
            ],
        })

    @property
    def W(self) -> int:
        return self.hexmap.W
    @property
    def H(self) -> int:
        return self.hexmap.H
    def get_start_position(self, pos: Position) -> Position|None:
        hrange = int(self.hexmap.H / 3)+1
        hmin = max(0, pos.y - hrange)
        hmax = min(self.hexmap.H - 1, pos.y + hrange)
        hlist = list(range(hmin, hmax+1))
        wrange = int(self.hexmap.W / 3)+1
        wmin = max(0, pos.x - wrange)
        wmax = min(self.hexmap.W - 1, pos.x + wrange)

        while len(hlist) > 0:
            i = random.randint(0, len(hlist)-1)
            y = hlist.pop(i)
            wlist = list(range(wmin, wmax+1))
            while len(wlist) > 0:
                i = random.randint(0, len(wlist)-1)
                x = wlist.pop(i)
                if self.hexmap.get(x,y) == 0:
                    return Position(x=x, y=y)
        return None

    def get_map_array(self) -> list[list[int]]:
        return self.hexmap.copy_as_list()

    def to_payload(self, view_side:str|None=None) -> tuple[dict,dict]:
        side = view_side if view_side in ['A','B'] else 'A'
        my_carrier = None
        my_squadrons = []
        other_carrier = None
        other_squadrons = []
        for u in self.units_list:
            pd = u.to_payload(view_side)
            if pd is not None:
                if isinstance(u.unit, CarrierState):
                    if side == u.side:
                        my_carrier = pd
                    else:
                        other_carrier = pd
                elif isinstance(u.unit, SquadronState):
                    if side == u.side:
                        my_squadrons.append(pd)
                    else:
                        other_squadrons.append(pd)
        my_result = {}
        if my_carrier is not None:
            my_result['carrier'] = my_carrier
        if my_squadrons:
            my_result['squadrons'] = my_squadrons
        other_result = {}
        if other_carrier is not None:
            other_result['carrier'] = other_carrier
        if other_squadrons:
            other_result['squadrons'] = other_squadrons
        return my_result, other_result

    def _get_carrier_by_side(self, side: str) -> UnitHolder|None:
        for u in self.units_list:
            if u.side == side and isinstance(u.unit, CarrierState):
                return u
        return None

    def get_carrier_by_side(self, side: str) -> CarrierState|None:
        for u in self.units_list:
            if u.side == side and isinstance(u.unit, CarrierState):
                return u.unit
        return None

    def get_squadrons_by_side(self, side: str) -> list[SquadronState]:
        return [u.unit for u in self.units_list if u.side == side and isinstance(u.unit, SquadronState)]

    def get_intel_by_side(self, side: str) -> IntelReport:
        return self.intel.get(side, IntelReport(side=side, turn=0))

    def turn_forward(self, orders:list[PlayerOrders]) -> dict[str,IntelReport]:
        logs:dict[str,list[str]] = {}
        _dbg(self.log_id, f"[Turn {self.turn}] start")
        # file log: turn start
        match_write(self.log_id, {"type": "turn_start", "turn": self.turn})
        if len(orders) != 2:
            raise ValueError("Units list and orders list must have the same length.")

        # reset
        for u in self.units_list:
            u.reset()

        # orderのターゲット位置の妥当性チェックと設定
        for side, order in zip(["A","B"], orders):
            if order.carrier_target is not None:
                if not (0 <= order.carrier_target.x < self.hexmap.W and 0 <= order.carrier_target.y < self.hexmap.H):
                    raise ValueError(f"Carrier target {order.carrier_target} out of map bounds.")
                if self.hexmap.get(order.carrier_target.x, order.carrier_target.y) != 0:
                    raise ValueError(f"Carrier target {order.carrier_target} is not on sea.")
                for u in self.units_list:
                    if u.side == side and isinstance(u.unit, CarrierState):
                        u.unit.target = order.carrier_target
                        _dbg(self.log_id, f"[Turn {self.turn}] side {side} carrier target -> ({order.carrier_target.x},{order.carrier_target.y})")
                        match_write(self.log_id, {
                            "type": "order_carrier_target",
                            "turn": self.turn,
                            "side": side,
                            "target": [order.carrier_target.x, order.carrier_target.y],
                        })
                        break
        # 判定フェーズ
        for u in self.units_list:
            if u.unit.is_active() and isinstance(u.unit, SquadronState) and u.unit.state=='engaging':
                # 攻撃中の航空部隊の攻撃処理に敵空母が居るか？
                ec = next( (cu for cu in self.units_list if cu.side != u.side and isinstance(cu.unit, CarrierState) and cu.unit.is_active() and u.unit.pos.hex_distance(cu.unit.pos) < 1.5), None)
                if ec:
                    u.ticks = u.unit.speed  # 攻撃完了まで動けない
                    ec.ticks = ec.unit.speed  # 攻撃完了まで動けない

                    aa = scaled_damage(ec.unit.hp,ec.unit.max_hp, 20)
                    dmg = scaled_damage(u.unit.hp,u.unit.max_hp, 25)
                    # 空母へダメージ適用
                    ec.unit.hp = max(0, ec.unit.hp - dmg)
                    _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} attacks {ec.unit.id}: dmg={dmg}, AA={aa}")
                    match_write(self.log_id, {
                        "type": "attack",
                        "turn": self.turn,
                        "attacker": u.unit.id,
                        "defender": ec.unit.id,
                        "pos": [u.unit.pos.x, u.unit.pos.y],
                        "dmg_to_carrier": dmg,
                        "aa_to_attacker": aa,
                    })
                    if ec.unit.hp <= 0:
                        # 撃沈
                        logs.setdefault(u.side, []).append(f"{ec.unit.id}({ec.unit.pos.x},{ec.unit.pos.y}) was sunk by {u.unit.id}({u.unit.pos.x},{u.unit.pos.y})")
                        _dbg(self.log_id, f"[Turn {self.turn}] {ec.unit.id} sunk by {u.unit.id}")
                        ec.unit.target = None
                        ec.unit.pos = Position.invalid()
                        match_write(self.log_id, {"type": "sunk", "turn": self.turn, "unit": ec.unit.id, "by": u.unit.id})
                    # 編隊へAA適用
                    u.unit.hp = max(0, u.unit.hp - aa)
                    if u.unit.hp <= 0:
                        # 撃墜
                        logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) was shot down by AA")
                        _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} shot down by AA")
                        u.unit.state = 'lost'
                        u.unit.pos = Position.invalid()
                        u.unit.target = None
                        match_write(self.log_id, {"type": "shot_down", "turn": self.turn, "unit": u.unit.id})
                    else:
                        logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) finished attack and is returning")
                        _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} finished attack → returning")
                else:
                    logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) lost its target and is returning")
                    _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} lost target → returning")
                # 攻撃完了したら帰還状態に変更
                u.unit.state = 'returning'

        #
        tick_queue:dict[int,list[UnitHolder]] = {}
        # 全ユニットの次の行動時間を設定
        for u in self.units_list:
            if u.unit.is_active() and u.ticks < u.unit.speed:
                u.next_time = int( 1000 / u.unit.speed )
                tick_queue.setdefault(u.next_time, []).append(u)

        # 移動と索敵ループ
        while tick_queue:
            current_time = min(tick_queue.keys())
            current_units = tick_queue.pop(current_time)
            random.shuffle(current_units)
            # ユニットの移動フェーズ
            for u in current_units:
                if u.unit.target is not None and u.unit.pos != u.unit.target and u.ticks < u.unit.speed:
                    if isinstance(u.unit, SquadronState) and u.unit.state == 'returning':
                        # 帰還中は空母の位置を目標にする
                        cu = next((cu for cu in self.units_list if cu.side == u.side and isinstance(cu.unit, CarrierState)), None)
                        if cu is not None:
                            if cu.unit.pos.hex_distance(u.unit.pos) < 1.5:
                                # 空母に到達したら基地状態に変更
                                logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) returned to carrier {cu.unit.id}({cu.unit.pos.x},{cu.unit.pos.y})")
                                _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} returned to carrier {cu.unit.id}")
                                u.unit.state = 'base'
                                u.unit.pos = Position.invalid()
                                u.path.append(u.unit.pos)
                                u.unit.target = None
                                u.ticks = u.unit.speed
                                cu.ticks = cu.unit.speed # 着艦時には動けない
                                try:
                                    if self.log_id:
                                        match_write(self.log_id, {"type": "return", "turn": self.turn, "id": u.unit.id, "carrier": cu.unit.id})
                                except Exception:
                                    pass
                                continue
                            u.unit.target = cu.unit.pos
                    ignore_land = isinstance(u.unit, SquadronState)
                    next_pos = next_step(self.hexmap, self.units_list, u.unit.pos, u.unit.target, ignore_land=ignore_land)
                    if next_pos is not None:
                        if isinstance(u.unit, CarrierState):
                            _dbg(self.log_id, f"[Turn {self.turn}] carrier {u.unit.id} move {u.unit.pos.x},{u.unit.pos.y} -> {next_pos.x},{next_pos.y}")
                            match_write(self.log_id, {
                                "type": "move",
                                "turn": self.turn,
                                "unit": u.unit.id,
                                "from": [u.unit.pos.x, u.unit.pos.y],
                                "to": [next_pos.x, next_pos.y],
                            })
                        u.unit.pos = next_pos
                        u.path.append(u.unit.pos)
                        u.ticks += 1
                    if u.ticks < u.unit.speed:
                        u.next_time += int( 1000 / u.unit.speed )
                        tick_queue.setdefault(u.next_time, []).append(u)
            # 索敵フェーズ
            for u in self.units_list:
                for enemy in [ us for us in self.units_list if us is not u and us.side != u.side]:
                    if enemy.unit.is_active() and u.unit.can_see_enemy(enemy.unit):
                        enemy.intel[current_time] = enemy.unit.pos
                        logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) found {enemy.unit.id}({enemy.unit.pos.x},{enemy.unit.pos.y})")
                        _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} found {enemy.unit.id} at {enemy.unit.pos.x},{enemy.unit.pos.y}")
                        match_write(self.log_id, {
                            "type": "detect",
                            "turn": self.turn,
                            "by": u.unit.id,
                            "enemy": enemy.unit.id,
                            "pos": [enemy.unit.pos.x, enemy.unit.pos.y],
                        })
                        pass
                        # 航空部隊が進出中に敵空母を発見したら攻撃目標に設定
                        if isinstance(u.unit, SquadronState) and u.unit.state=='outbound':
                            if isinstance(enemy.unit, CarrierState):
                                u.unit.target = enemy.unit.pos
                                _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} switches target to enemy carrier {enemy.unit.id}")

        # 判定フェーズ
        for u in self.units_list:
            if u.unit.is_active() and isinstance(u.unit, SquadronState) and u.unit.state=='outbound':
                # 攻撃中の航空部隊の攻撃処理に敵空母が居るか？
                ec = next( (cu for cu in self.units_list if cu.side != u.side and isinstance(cu.unit, CarrierState) and cu.unit.is_active() and u.unit.pos.hex_distance(cu.unit.pos) < 1.5), None)
                if ec:
                    logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) is attacking {ec.unit.id}({ec.unit.pos.x},{ec.unit.pos.y})")
                    u.unit.state = 'engaging'
                    _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} starts attacking {ec.unit.id}")
                    match_write(self.log_id, {"type": "engage", "turn": self.turn, "attacker": u.unit.id, "defender": ec.unit.id})
                elif u.unit.pos == u.unit.target:
                    # 目標に到達したら帰還状態に変更
                    cu = next((cu for cu in self.units_list if cu.side == u.side and isinstance(cu.unit, CarrierState)), None)
                    if cu is not None:
                        u.unit.target = cu.unit.pos
                    logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) reached its target and is returning")
                    u.unit.state = 'returning'
                    _dbg(self.log_id, f"[Turn {self.turn}] {u.unit.id} reached target → returning")
        # 発艦処理
        for side, order in zip(["A","B"], orders):
            if order.launch_target is not None:
                if not (0 <= order.launch_target.x < self.hexmap.W and 0 <= order.launch_target.y < self.hexmap.H):
                    raise ValueError(f"Launch target {order.launch_target} out of map bounds.")
                if self.hexmap.get(order.launch_target.x, order.launch_target.y) != 0:
                    raise ValueError(f"Launch target {order.launch_target} is not on sea.")
                launched = False
                for u in self.units_list:
                    if u.side == side and isinstance(u.unit, SquadronState):
                        if u.unit.state == 'base':
                            # 空母の位置を取得
                            launch_pos = next((cu.unit.pos for cu in self.units_list if cu.side == side and isinstance(cu.unit, CarrierState)), None)
                            if launch_pos is not None:
                                # ターゲットに近い位置に発艦
                                pos = next_step(self.hexmap, self.units_list, launch_pos, order.launch_target,ignore_land=True)
                                if pos is not None:
                                    logs.setdefault(u.side, []).append(f"{u.unit.id}({launch_pos.x},{launch_pos.y}) launched to towards {order.launch_target}")
                                    _dbg(self.log_id, f"[Turn {self.turn}] side {side} {u.unit.id} launched toward {order.launch_target.x},{order.launch_target.y}")
                                    match_write(self.log_id, {
                                        "type": "launch",
                                        "turn": self.turn,
                                        "side": side,
                                        "id": u.unit.id,
                                        "from": [launch_pos.x, launch_pos.y],
                                        "target": [order.launch_target.x, order.launch_target.y],
                                    })
                                    u.unit.pos = pos
                                    u.path.append(u.unit.pos)
                                    u.unit.state = 'outbound'
                                    u.unit.target = order.launch_target
                                    launched = True
                                    break
                    if launched:
                        break
        # ----
        for i, side in enumerate(["A","B"]):
            report = IntelReport(side=side, turn=self.turn)
            report.logs = logs.get(side, [])
            # 自軍ユニット情報
            report.units = [u.unit for u in self.units_list if u.side == side]
            # 索敵結果
            for u in self.units_list:
                if u.side != side and u.unit.is_active() and u.intel:
                    poist_list = [p for t,p in sorted(u.intel.items())]
                    ir_path = IntelPath( side=u.side, unit_id=u.unit.id, turn=self.turn, p1=poist_list[0], p2=poist_list[-1])
                    report.intel[ir_path.unit_id] = ir_path

            # 古い情報を削除
            for it in self.intel.values():
                for ir_path in it.intel.values():
                    if ir_path.turn < self.turn - 3:
                        del it.intel[ir_path.unit_id]
            self.intel[side] = report
        # ターン終了サマリ
        a_car = self.get_carrier_by_side("A")
        b_car = self.get_carrier_by_side("B")
        _dbg(self.log_id,
            f"[Turn {self.turn}] end: A({a_car.pos.x if a_car else None},{a_car.pos.y if a_car else None}) HP={a_car.hp if a_car else None} / "
            f"B({b_car.pos.x if b_car else None},{b_car.pos.y if b_car else None}) HP={b_car.hp if b_car else None}"
        )
        match_write(self.log_id, {
            "type": "turn_end",
            "turn": self.turn,
            "a": {"pos": ([a_car.pos.x, a_car.pos.y] if a_car else None), "hp": (a_car.hp if a_car else None)},
            "b": {"pos": ([b_car.pos.x, b_car.pos.y] if b_car else None), "hp": (b_car.hp if b_car else None)},
        })
        self.turn += 1
        return self.intel

    def is_over(self) -> bool:
        a_hp = next( (u.unit.hp for u in self.units_list if u.side == "A" and isinstance(u.unit, CarrierState)), 0)
        b_hp = next( (u.unit.hp for u in self.units_list if u.side == "B" and isinstance(u.unit, CarrierState)), 0)
        return a_hp <= 0 or b_hp <= 0
