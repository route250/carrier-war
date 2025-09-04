# 進行管理 / 計画（PvP対応）

このドキュメントは、ユーザ間対戦（PvP）機能の導入に向けた計画と進行状況を管理します。段階的に実装し、既存の対AI（PvE）機能と共存させます。

## ゴール
- 対AI（PvE）を維持したまま、ユーザ間対戦（PvP）の骨格を追加する。
- 通知は Server-Sent Events (SSE) に統一する（WSは採用しない）。
- 複数同時接続に安全な構造（セッション/マッチ分離、直列化）を用意する。

## スコープ（段階）
- Phase 0: 計画・合意（本ドキュメント作成、AGENT.md更新）
- Phase 1: 新規マッチAPIの骨格追加（モデル・ストア・ルータの最小実装）
- Phase 2: クライアント：ロビーのPvPタブ、一覧/作成/参加（SSEで状態反映）
- Phase 3: 注文収集→同時適用→ターン解決フロー（2フェーズ、サイド別可視）
- Phase 4: 認証/認可（軽量トークン）
- Phase 5: 安定化・SSE拡張・外部ストア検討

## 設計項目（要件）
- API/Schema
  - Match: `match_id`, `mode: 'pve'|'pvp'`, `status: 'waiting'|'active'|'over'`, `config`（difficultyなど）, `created_at`。
  - Player: `player_id`, `player_token`, `display_name`, `side: 'A'|'B'`。
  - Endpoints（最小）:
    - `POST /v1/match`（作成: 返り値に`match_id`, 自分の`player_token`）
    - `GET /v1/match`（参加待ち一覧）
    - `POST /v1/match/{id}/join`（参加: もう片方のサイドに割当）
    - `GET /v1/match/{id}/state`（自分視点の状態を返却）
    - `POST /v1/match/{id}/orders`（自分のターン注文を送信）
  - 通知: `GET /v1/match/{id}/events`（SSE）
- サーバロジック
  - 2フェーズ（submit/resolve）：両者の注文が揃ったら同時適用→ターン進行。
  - PvE/PvP分岐：PvPはAI計画不要。PvEは現行AIを利用。
  - 視界・インテル：サイド別に計算し、クライアントには自分視点のみ返却。
- ストア/並行性
  - `MatchStore`: `match_id -> Match` をメモリ保持（初期）。
  - 直列化：マッチ単位のロックで`orders/resolve`を保護。
  - 将来：ワーカー増に備え外部ストアへ移行可能な抽象化。
- 認証/認可
  - `player_token` を各APIで検証し、サイドと権限を紐付け。
- クライアント
  - ロビー: PvE/PvP切替、マッチ一覧、作成、参加。
  - ゲーム: 「準備完了/取消」ボタン、相手待ち表示、解決結果の取得。
- 終了/離脱
  - タイムアウト規約、退室、再接続（トークン）。

## タスク（チェックリスト）
- [x] Phase 1: サーバ
  - [x] `server/schemas.py` に Match/Player/Orders スキーマ追加
  - [x] `server/services/match.py`（新規）: Match, MatchStore, ロック
  - [x] `server/routers/match_router.py`（新規）: エンドポイント
- [ ] Phase 2: クライアント
  - [x] ロビーに PvE/PvP 切替UI、PvP一覧/作成/参加
  - [x] PvPゲームフローの最低限UI（準備完了、相手待ち）
  - [ ] PvPゲーム画面（キャンバス）実装タスク
    - [x] レンダラ抽象化（第一段）: 共有ヘックスレンダラ `makeHexRenderer()` を追加し、PvP簡易表示で流用
    - [x] サーバSSE拡張: サイド別スナップショット（carrier座標/HP、map_w/h、turn/status）
    - [x] クライアント状態ブリッジ: PvPスナップショット→簡易レンダラstateへ反映
    - [x] ビュー構成: `#view-match`にキャンバス/サイドバー/ログ領域を追加
    - [x] SSEハンドラ: 受信スナップショットで`matchState`更新→`renderMatchView()`
    - [x] 自軍編隊の描画（最小: マーカー+HPバー）
    - [x] 自軍編隊ステータスのサイドバー表示（id/state/HP/座標）
    - [x] 入力/注文: PvPでは
      - [x] モード切替（select/move/launch）UIの追加
      - [x] クリックで移動先をステージング→準備完了で送信
      - [x] 移動プレビュー線の表示
    - [x] ログ/ヒント: PvPでもヒント表示（ログは今後拡張）
    - [ ] エラー処理（低優先度）: SSE切断時の明示エラー、注文失敗時の通知
    - [ ] 受入基準: 2ブラウザで参加→キャンバス表示→移動注文→ターン解決で位置更新がSSEで反映
  - [ ] PvPゲーム画面（キャンバス）表示：既存PvE描画を流用し、マッチSSE/ステートに接続
- [x] ロビーの自動更新（SSE）
    - [x] サーバ: ロビーSSE `/v1/match/events` を追加（マッチ一覧ストリーム）
    - [x] サーバ: マッチ作成/参加/離脱時にロビーへブロードキャスト
    - [x] クライアント: PvPタブ表示時にSSE接続、一覧を自動更新（`startLobbySSE()` 実装済み）
- [ ] Phase 3: 解決ロジック
  - [x] 注文収集/同時適用/ターン進行（両者提出時に解決、carrier移動を適用）
  - [ ] サイド別可視/インテル返却
  - [x] 打撃隊（launch）の最小実装（発艦→移動→ダメージ）
  - [x] 勝敗判定とゲーム終了（PvP）: いずれかの空母HP<=0で`status: over`に遷移し、SSEペイロードに`result: 'win'|'lose'|'draw'`（視点別）を付与
- [ ] Phase 4: 認証（低優先度）
  - [ ] 軽量トークン発行/検証（メモリ）
- [ ] Phase 5: 安定化（低優先度）
  - [ ] SSE通知の拡張/テスト整備、外部ストアの選定
- [x] SSE通知の最小導入（/v1/match/{id}/events, クライアントEventSource）
  - [x] ポーリングフォールバックを撤廃（SSE接続エラー時はエラー表示）

## メモ/決定事項の記録
- PvPは新規`/v1/match`系APIで提供し、既存`/v1/session`はPvE用として維持。
- APIベースパスは`/v1/match`に統一（PROTOCOLS.mdも更新）。
- まずは単一ワーカー運用を前提。将来`--workers > 1`の場合は外部ストアへ。
 - 通知はSSEに統一。ロビー（マッチ一覧）もSSE対応予定。
- 不具合修正（UI）: マッチ入室直後にSSE未確立の状態でMove/Launch/Readyが有効化されて見える問題を修正。
  - 対応: `openMatchRoom()`で`updateMatchControls()`を先行実行し、初期状態では操作を無効化。
  - 影響範囲: クライアントのみ。PvE画面には影響なし。

- 不具合修正（サーバ軽微）
  - `server/services/session.py` の `_validate_sea_connectivity` における `dist[y][x]` の未定義参照を `dist[spos.y][spos.x]` に修正。
  - 影響: マップ連結性検証時の到達数カウントの正確性向上（例外は元々握っているため致命ではない）。

- 不具合修正（サーバ/描画）
  - `server/services/hexmap.py` の `draw()` が pointy-top の odd-q レイアウト相当になっており、クライアント（`static/main.js`）の pointy-top odd-r と不一致だった。
  - 修正内容: client と同じ式に統一。
    - 中心座標: `cx = sqrt(3)*r*(col + 0.5*(row&1)) + r`, `cy = 1.5*r*row + r`
    - SVGサイズ: `width = sqrt(3)*r*(W + 0.5)`, `height = 1.5*r*(H - 1) + 2*r`
    - 六角形頂点角度: `60*i - 30`（pointy-top）で client と一致
  - 影響: サーバ生成SVGの六角グリッドがクライアントの表示と完全一致。座標ラベル表示も正しい位置に。

- ロビーSSEとマッチSSEの仕様（最小）
  - マッチSSE: 接続直後に`event: state`で初期スナップショットをpush、その後は更新差分を`data:`で配信。15秒間隔のハートビート（コメント行）を送出。
  - ロビーSSE: 接続直後に`event: list`で一覧をpush。作成/参加/離脱でロビーにブロードキャスト。
  - PvPスナップショット内容: 自軍の`carrier`に加え、自軍`scuadrons`の最小情報（id,hp,state,x,y）を返却。敵編隊は未開示（今後インテル経由）。
  - `waiting_for`: you/opponent/none を返却し、クライアントの操作可否を制御。

- 次アクション（優先度順）
  - サイド別可視/インテル返却（PvP向け）
  - 打撃隊（launch/engage/return）の最小解決ロジックを`server/services/match.py`に追加
  - SSE切断/注文失敗時の明確なエラー表示（クライアント）（低優先度）
  - タイムアウト/離脱・再接続（トークン再利用）の運用整理（低優先度）
  - 認証（軽量トークン発行/検証）の段階導入（低優先度）
  - 可視/インテルのサーバテスト追加（tests/test_visibility_server.py）

- 仕様整合（重要）
  - ターン解決は「両者の注文が揃った時」に実行（片側提出のみでは自動進行しない）。
  - 可視/インテルはサイド別にゲーティング。敵データは可視時および記憶TTL内のみ返却。
  - PvP UIのステータス表示の多言語対応と細分化（現在は日本語で4段階）

## 合意事項（UI/フロー）
- PvPの注文は両プレイヤー同時受付（ready方式）。両者の注文が揃った時点で同時適用→ターン進行。
- 画面ステータスは以下の4段階で表示（`static/main.js` 内`updateMatchPanels()`）：
  1. 参加受付中（相手の参加待ち）
  2. オーダー受付中（あなたの入力待ち）
  3. 相手のオーダ完了まち
  4. ゲーム終了（あなたの勝ち/負け/引き分け）

## 運用/デバッグ
- 起動: `./start_server.sh start`
- ログ: `./start_server.sh logs -f`
- 手動テスト方針: ブラウザ2枚で同一マッチに参加→入替で注文→解決確認。
 - PvPマッチ監査ログ: `logs/matches/<timestamp>_<match_id>.log` にJSONLで出力（`server/services/turn.py`）。
   - 記録例: `turn_start`, `order_carrier_target`, `launch`, `engage`, `attack`, `shot_down`, `sunk`, `return`, `move`(空母のみ), `turn_end`。
