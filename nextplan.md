nearestSea(x, y)
理由：目的地補正（島→最寄り海マス）はサーバでバリデート/補正すべき。クライアントだけで補正すると不整合が生じる可能性がある。
isOccupied(x, y, {ignore})
理由：占有判定は衝突・スポーン競合の権威ある判定でありサーバ側で確定すべき。
findFreeAdjacent(cx, cy, { preferAwayFrom })
理由：発艦スポーン位置決定（隣接空きマス探索）はサーバで決定すべき（複数クライアント/同時要求を正しく扱うため）。
isVisibleToPlayer(x, y) と Turn-visibility 関連（isTurnVisible, visibilityKey, clearTurnVisibility, state.turnVisible の扱い）
理由：可視性（fog of war）とプレイヤーへの「記憶」はサーバが計算して配信するほうが一貫性が保たれる（現在、サーバは plan.turn_visible を返しているため、クライアントの再計算は冗長または不整合源となり得る）。
距離・直線関連のユーティリティ（hexDistance, nextStepOnHexLine, pointAtHexLineDistance, clampTargetToRange, および座標変換 helpers: offsetToAxial, axialToOffset, axialToCube, cubeToAxial, cubeRound, axialRound）
理由：UI用にクライアントで残す価値はあるが、航続距離チェック・射程切り詰め・移動経路決定など権威あるゲーム解決はサーバで行うべき。サーバが既に距離チェック結果（例：発艦可能/不可能、切り詰め後座標）を返すならクライアント側の重複実装は削減可能。
nearestSea と clampTargetToRange の組み合わせで行っている「ターゲット補正」処理全般
理由：プレイヤーからの命令を受けてサーバ側で命令検証・補正・拒否を行うべき（チート防止と同期のため）。