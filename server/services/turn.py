
from server.schemas import SessionStepRequest, PlayerOrders
from server.schemas import Position, UnitState, CarrierState, SquadronState
from server.services.hexmap import HexArray


def validate_order( map: HexArray, units_list:list[UnitState], order:PlayerOrders) -> list[str]:
    errors = []
    # 妥当性チェック
    return errors

def create_path( map: HexArray, current: Position, target:Position ) -> list[Position]:
    # ここに経路生成のロジックを実装
    return []

def turn_forward( map: HexArray, units_list:list[list[UnitState]], orders:list[PlayerOrders]):
    if len(units_list) != len(orders):
        raise ValueError("Units list and orders list must have the same length.")
    if len(units_list) != 2:
        raise ValueError("This game only supports 2 players.")
    if map is None:
        raise ValueError("Map cannot be None.")
    
    errors_list = [ [] for _ in range(len(orders)) ]
    # orderの妥当性チェック
    for side, (units, order) in enumerate(zip(units_list, orders)):
        errors_list[side].extend(validate_order(map, units, order))
    if any(errors_list):
        return {"status": "error", "errors": errors_list}
    # ここでターンを進める処理を実装

    # 全ユニットの移動経路を計算
    max_step = 0
    for side, (units, order) in enumerate(zip(units_list, orders)):
        sqo=False
        for unit in units:
            if unit.is_active():
                if isinstance(unit, CarrierState):
                    if order.carrier_target is not None:
                        unit.target = order.carrier_target

                elif isinstance(unit, SquadronState):
                    if unit.state == 'base' and not sqo and order.launch_target is not None:
                        sqo = True
                        unit.target = order.launch_target
                        unit.state =  "outbound"
                max_step = max(max_step, unit.speed) if unit.target is not None else max_step
    founds_list = [[] for _ in range(len(units_list))]
    for step in range(max_step):

        # 索敵フェーズ
        for side, (units, founds) in enumerate(zip(units_list,founds_list)):
            for unit in units:
                for enemy_units in [ us for us in units_list if us is not unit]:
                    for enemy in enemy_units:
                        if enemy.is_active() and unit.can_see_enemy(enemy):
                            # 敵が生きてて索敵範囲内なら発見
                            founds.append(f"{unit.id}({unit.pos.x},{unit.pos.y}) found {enemy.id}({enemy.pos.x},{enemy.pos.y})")
                            # 航空部隊が進出中に敵空母を発見したら攻撃目標に設定
                            if isinstance(unit, SquadronState) and unit.state=='outbound': 
                                if isinstance(unit, CarrierState):
                                    unit.target = enemy.pos

                sqo=False
                for unit in units:
                    if unit.is_active():
                        if isinstance(unit, CarrierState):
                            if order.carrier_target is not None:
                                unit.target = order.carrier_target

                        elif isinstance(unit, SquadronState):
                            if unit.state == 'base' and not sqo and order.launch_target is not None:
                                sqo = True
                                unit.target = order.launch_target
                                unit.state =  "outbound"
                        max_step = max(max_step, unit.speed) if unit.target is not None else max_step
        errors_list[side].extend(validate_order(map, units, order))
    paths_list = [ unit for units in units_list for unit in units]
    speed_max = max(unit.speed for units in units_list for unit in units)
    
    return {"status": "success"}
