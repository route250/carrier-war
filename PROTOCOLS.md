# 基本事項

1. 自分のユニットの名前と座標とHPや状態は、ターンの最初にクライアントが受信できるようにする。
2. 対戦相手のユニットの座標とHPは、索敵で発見した時の座標、発見した時のHP、発見した時のターン数だけをクライアントが受信できる。


### 8. サーバ側の空母座標管理・移動・返信の流れ

1. サーバ側では、空母の座標は `CarrierState` クラスの `pos` 変数（型：`Position`）に記録される。
  - 各プレイヤーの状態は `Match.a_state`（A側）・`Match.b_state`（B側）に格納。
2. ターン進行時、`MatchStore.submit_orders()` が呼ばれ、両者のordersが揃うと `_resolve_turn_minimal(m)` が実行される。
3. `_resolve_turn_minimal()` 内で `_apply_carrier_move()` が呼ばれ、orders（移動先座標）に従い `CarrierState.pos` が更新される。
4. 移動後、`CarrierState.pos` は新しい座標に書き換えられ、ターン終了時に `m.turn` もインクリメントされる。
5. クライアントが `/v1/match/{match_id}/state` をGETすると、`MatchStore.state()` が呼ばれ、最新座標が `MatchStateResponse` の `a.carrier.x`, `y` などにセットされてJSONで返信される。

この流れで、サーバ側で座標が管理・更新され、API経由でブラウザに返信されます。
# PROTOCOLS.md


## 対人ゲームのmatch関係 通信プロトコル・フォーマット（2025/09/02時点）

### 1. 概要
- サーバーとクライアント間で、対戦状態（マッチ情報、ターン進行、ユニット座標など）をやり取りするAPI群。
- REST API（JSONフォーマット）＋一部SSE（イベント通知）

---

### 2. 主なエンドポイント

#### 2.1. マッチ作成
- **POST /v1/match/**
- リクエスト: `MatchCreateRequest`
- レスポンス: `MatchCreateResponse`

#### 2.2. マッチ一覧取得
- **GET /v1/match/**
- レスポンス: `MatchListResponse`

#### 2.3. マッチ参加
- **POST /v1/match/{match_id}/join**
- リクエスト: `MatchJoinRequest`
- レスポンス: `MatchJoinResponse`

#### 2.4. マッチ状態取得
- **GET /v1/match/{match_id}/state?token=...**
- レスポンス: `MatchStateResponse`

#### 2.5. 行動送信（ターン進行）
- **POST /v1/match/{match_id}/orders**
- リクエスト: `MatchOrdersRequest`
- レスポンス: `MatchOrdersResponse`

#### 2.6. マッチ離脱
- **POST /v1/match/{match_id}/leave?token=...**
- レスポンス: `{ "ok": true }`

#### 2.7. イベント通知（SSE）
- **GET /v1/match/{match_id}/events?token=...**
- レスポンス: SSEストリーム（`event: state\ndata: ...`）

---

### 3. 主なデータ構造

#### 3.1. MatchCreateRequest
```json
{
  "mode": "pvp",
  "config": { "difficulty": "normal" },
  "display_name": "ユーザー名"
}
```

#### 3.2. MatchCreateResponse
```json
{
  "match_id": "abc123",
  "player_token": "xxxx",
  "side": "A",
  "status": "waiting",
  "mode": "pvp",
  "config": { "difficulty": "normal" }
}
```

#### 3.3. MatchStateResponse
```json
{
  "match_id": "abc123",
  "status": "active",
  "mode": "pvp",
  "turn": 5,
  "your_side": "A",
  "waiting_for": "none",
  "map_w": 32,
  "map_h": 32,
  "a": { "carrier": { "x": 12, "y": 34, "hp": 100 }, ... },
  "b": { "carrier": { "x": 56, "y": 78, "hp": 100 }, ... }
}
```

#### 3.4. MatchOrdersRequest
```json
{
  "player_token": "xxxx",
  "player_orders": {
    "carrier_target": { "x": 20, "y": 40 },
    "launch_target": { "x": 25, "y": 45 }
  }
}
```

#### 3.5. MatchOrdersResponse
```json
{
  "accepted": true,
  "status": "active",
  "turn": 6
}
```

#### 3.6. MatchListResponse
```json
{
  "matches": [
    {
      "match_id": "abc123",
      "status": "active",
      "mode": "pvp",
      "has_open_slot": true,
      "created_at": 1690000000,
      "config": { "difficulty": "normal" }
    }
  ]
}
```

#### 3.7. Position
```json
{ "x": 12, "y": 34 }
```

---

### 4. 通信フロー例
1. マッチ作成（POST /v1/match/）
2. 参加（POST /v1/match/{match_id}/join）
3. 状態取得（GET /v1/match/{match_id}/state?token=...）
4. 行動送信（POST /v1/match/{match_id}/orders）
5. 状態取得 or SSEで更新（GET /v1/match/{match_id}/events?token=...）
6. 離脱（POST /v1/match/{match_id}/leave?token=...）

---

### 5. エラー処理
- HTTPステータスコード（400, 404など）とともに、
  ```json
  { "detail": "match not found" }
  ```
  などのJSONレスポンスが返る。

---


### 7. クライアントでの座標格納・描画の流れ

1. サーバから送られてきた空母の座標（`x`, `y`）は、APIレスポンス（`MatchStateResponse`の`a.carrier`または`b.carrier`）として取得される（`GET /v1/match/{match_id}/state`）。
2. クライアント側では `APP.matchState` という変数に格納される（`updateMatchState`等でAPIレスポンスを代入）。
3. 画面描画時は `renderMatchView()` 関数が呼ばれ、`APP.matchState.a.carrier.x`/`y` または `APP.matchState.b.carrier.x`/`y` を使って座標を取得。
4. `renderMatchView()` 内で `APP.matchHex.drawCarrier(x, y, ...)` により、canvas上に空母アイコンが描画される。

この流れで、サーバから送られた座標は `APP.matchState` に入り、`renderMatchView()` でcanvas上に描画されます。
