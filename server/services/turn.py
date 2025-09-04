
from server.schemas import SessionStepRequest, PlayerOrders
from server.schemas import Position, UnitState, CarrierState, SquadronState, IntelPath, IntelReport
from server.schemas import SQUAD_MAX_HP, CARRIER_MAX_HP
from server.services.hexmap import HexArray
import random

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
    def __init__(self, hexmap: HexArray, units_list:list[list[UnitState]]):
        if len(units_list) == 0:
            raise ValueError("Units list and orders list must have the same length.")
        if len(units_list) != 2:
            raise ValueError("This game only supports 2 players.")
        if hexmap is None:
            raise ValueError("Map cannot be None.")

        self.turn:int = 1
        self.hexmap = hexmap
        self.units_list:list[UnitHolder] = []
        for side, bbb in zip(["A","B"], units_list):
            for unit in bbb:
                if isinstance(unit, CarrierState):
                    unit.target = self.get_start_position(unit.pos)
                self.units_list.append(UnitHolder(side, unit))
        self.intel: dict[str,IntelReport] = {"A":IntelReport(side="A",turn=0), "B":IntelReport(side="B",turn=0)}

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

    def get_carrier_by_side(self, side: str) -> CarrierState|None:
        for u in self.units_list:
            if u.side == side and isinstance(u.unit, CarrierState):
                return u.unit
        return None

    def get_squadrons_by_side(self, side: str) -> list[SquadronState]:
        return [u.unit for u in self.units_list if u.side == side and isinstance(u.unit, SquadronState)]

    def turn_forward(self, orders:list[PlayerOrders]) -> dict[str,IntelReport]:
        logs:dict[str,list[str]] = {}
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
                    if ec.unit.hp <= 0:
                        # 撃沈
                        logs.setdefault(u.side, []).append(f"{ec.unit.id}({ec.unit.pos.x},{ec.unit.pos.y}) was sunk by {u.unit.id}({u.unit.pos.x},{u.unit.pos.y})")
                        ec.unit.target = None
                        ec.unit.pos = Position.invalid()
                    # 編隊へAA適用
                    u.unit.hp = max(0, u.unit.hp - aa)
                    if u.unit.hp <= 0:
                        # 撃墜
                        logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) was shot down by AA")
                        u.unit.state = 'lost'
                        u.unit.pos = Position.invalid()
                        u.unit.target = None
                    else:
                        logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) finished attack and is returning")
                else:
                    logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) lost its target and is returning")
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
                                u.unit.state = 'base'
                                u.unit.pos = Position.invalid()
                                u.path.append(u.unit.pos)
                                u.unit.target = None
                                u.ticks = u.unit.speed
                                cu.ticks = cu.unit.speed # 着艦時には動けない
                                continue
                            u.unit.target = cu.unit.pos
                    ignore_land = isinstance(u.unit, SquadronState)
                    next_pos = next_step(self.hexmap, self.units_list, u.unit.pos, u.unit.target, ignore_land=ignore_land)
                    if next_pos is not None:
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
                        pass
                        # 航空部隊が進出中に敵空母を発見したら攻撃目標に設定
                        if isinstance(u.unit, SquadronState) and u.unit.state=='outbound':
                            if isinstance(enemy.unit, CarrierState):
                                u.unit.target = enemy.unit.pos

        # 判定フェーズ
        for u in self.units_list:
            if u.unit.is_active() and isinstance(u.unit, SquadronState) and u.unit.state=='outbound':
                # 攻撃中の航空部隊の攻撃処理に敵空母が居るか？
                ec = next( (cu for cu in self.units_list if cu.side != u.side and isinstance(cu.unit, CarrierState) and cu.unit.is_active() and u.unit.pos.hex_distance(cu.unit.pos) < 1.5), None)
                if ec:
                    logs.setdefault(u.side, []).append(f"{u.unit.id}({u.unit.pos.x},{u.unit.pos.y}) is attacking {ec.unit.id}({ec.unit.pos.x},{ec.unit.pos.y})")
                    u.unit.state = 'engaging'

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
        self.turn += 1
        return self.intel

    def is_over(self) -> bool:
        a_hp = next( (u.unit.hp for u in self.units_list if u.side == "A" and isinstance(u.unit, CarrierState)), 0)
        b_hp = next( (u.unit.hp for u in self.units_list if u.side == "B" and isinstance(u.unit, CarrierState)), 0)
        return a_hp <= 0 or b_hp <= 0