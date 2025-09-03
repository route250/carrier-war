Title: Match UI Controls and Readiness Protocol

Goal
- Disable/enable match-room buttons correctly based on server state.
- Do not advance turns until both players submit.
- Keep map rendering reliable on entry via SSE snapshot.

Server Protocol
- Status semantics:
  - status: "waiting" → one or both slots empty; no planning/editing; no turn advance.
  - status: "active" → both slots filled; per-turn submission gates UI.
  - status: "over" → game finished; all actions disabled.
- Readiness model:
  - Not submitted: orders is None.
  - Submitted/hold: orders is an object (possibly {}).
  - Turn resolves only when both sides have submitted (both not None).
- Personalization:
  - waiting_for:
    - "you": it’s your turn to submit; enable editing+submit.
    - "opponent": you already submitted; wait for opponent; disable actions.
    - "none": non-actionable state (over) or post-resolution transient.
- SSE payload:
  - Initial event: event: state; includes personalized snapshot with map.
  - Ongoing updates: type: "state"; same fields, map usually omitted.
- Endpoints:
  - GET /v1/match/{id}/events?token=… → personalized SSE (initial + updates)
  - GET /v1/match/{id}/state?token=… → polling fallback
  - POST /v1/match/{id}/orders → store orders or {} (hold); resolve only when both sides submitted
  - POST /v1/match/{id}/leave?token=… → leave match; server auto-deletes empty matches

Buttons And Rules
- Targets:
  - #btnMatchModeMove（空母を移動）
  - #btnMatchModeLaunch（打撃隊を出撃）
  - #btnSubmitReady（準備完了）
  - #btnMatchModeSelect（選択）…常に参照用に有効
- Enable rules:
  - Precondition: status === "active" のときのみ操作可能性あり
  - waiting_for === "you": Move/Launch/Ready を有効
  - waiting_for が "opponent" または "none": Move/Launch/Ready を無効
  - status が "waiting" または "over": Move/Launch/Ready を無効
- Canvas clicks:
  - Move/Launch モード時、編集不可なら無視（Select は常に可）
- Optional stricter rule（必要なら適用）:
  - Ready は何かステージングされるまで無効（サーバは {} をホールドとして許可）

Client Logic
- SSE/State ingestion:
  - 受信ペイロードを APP.matchState に反映
  - renderMatchView() → map_w/h, a/b.carrier, map（あれば）描画
  - updateMatchPanels() → サイドバー
  - updateMatchControls() → ボタンの disabled を設定
- Controls computation:
  - status = s.status; wait = s.waiting_for
  - disableAll = !APP.match || !s || status !== 'active'
  - canEdit = !disableAll && wait === 'you'
  - canSubmit = canEdit（または canEdit && hasStagedOrders）
  - Apply: Move.disabled=!canEdit, Launch.disabled=!canEdit, Ready.disabled=!canSubmit
- Staged orders:
  - APP.matchPending に保持し、送信後にクリア

Edge Cases
- エントリー時: 初回SSEで map を含むスナップショットを受信し、即描画
- 片方離脱: status→waiting またはマッチ削除。SSE切断時はクライアントが入口に戻る

