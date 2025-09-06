// === Config ===
const MAP_W = 30; // axial q range: 0..MAP_W-1
const MAP_H = 30; // axial r range: 0..MAP_H-1
// Hex layout (pointy-top axial)
const SQRT3 = Math.sqrt(3);
let HEX_SIZE = 10; // pixel radius of hex (computed at init to fit canvas)
let ORIGIN_X = 0, ORIGIN_Y = 0; // render offset to center the map

// Vision / Range constants
const VISION_CARRIER = 4;     // 敵味方空母の視界
const VISION_SQUADRON = 5;    // 敵味方航空機の視界
const SQUADRON_RANGE = 22;    // 航空機の航続距離（空母からの最大距離）

// === State ===
const SQUAD_MAX_HP = 40;
const CARRIER_MAX_HP = 100;
const APP = { view: 'entrance', username: '', match: null, matchSSE: null, matchHex: null, matchPending: { carrier_target: null, launch_target: null }, matchLoggedUpToTurn: 0, matchHover: null };
const state = {
  map: [], // 0=sea, 1=island
  turn: 1,
  mode: 'select', // select | move | launch
  carrier: { id: 'C1', x: 3, y: 3, hp: 100, speed: 2, vision: VISION_CARRIER, hangar: 2, target: null },
  enemy: { carrier: { id: 'E1', x: 26, y: 26, hp: 100, speed: 2, vision: VISION_CARRIER, hangar: 2 }, squadrons: [] },
  intel: { // player視点の可視・記憶
    carrier: { seen: false, x: null, y: null, ttl: 0 },
    squadrons: new Map(), // id -> {seen, x, y, ttl}
  },
  enemyIntel: { // 敵視点（AI用）
    carrier: { seen: false, x: null, y: null, ttl: 0 },
  },
  enemyAI: { patrolIx: 0, lastPatrolTurn: 0 },
  squadrons: [], // {id,hp,state:'base'|'outbound'|'engaging'|'returning'|'lost', x?,y?,target?}
  highlight: null,
  log: [],
  gameOver: false,
  // 今ターンの可視セル（自軍の現在位置視界＋移動経路でスイープした視界）
  turnVisible: new Set(), // key: "x,y"
};

// === DOM ===
const el = {
  // PvE用DOMは削除
  // Entrance/Lobby
  viewEntrance: document.getElementById('view-entrance'),
  viewLobby: document.getElementById('view-lobby'),
  // view-game は廃止
  viewMatch: document.getElementById('view-match'),
  usernameInput: document.getElementById('usernameInput'),
  enterLobby: document.getElementById('enterLobby'),
  lobbyUser: document.getElementById('lobbyUser'),
  startGameBtn: document.getElementById('startGameBtn'),
  // PvP lobby
  lobbyPvp: document.getElementById('lobby-pvp'),
  btnCreateMatch: document.getElementById('btnCreateMatch'),
  matchList: document.getElementById('matchList'),
  // Match room
  matchInfo: document.getElementById('matchInfo'),
  btnLeaveMatch: document.getElementById('btnLeaveMatch'),
  btnSubmitReady: document.getElementById('btnSubmitReady'),
  matchCanvas: document.getElementById('matchCanvas'),
  matchCarrierStatus: document.getElementById('matchCarrierStatus'),
  matchSquadronList: document.getElementById('matchSquadronList'),
  matchLog: document.getElementById('matchLog'),
  matchHint: document.getElementById('matchHint'),
  btnMatchModeMove: document.getElementById('btnMatchModeMove'),
  btnMatchModeLaunch: document.getElementById('btnMatchModeLaunch'),
};

// PvEキャンバスは廃止

// === App Init / Navigation ===
function showView(name) {
  APP.view = name;
  el.viewEntrance?.classList.toggle('hidden', name !== 'entrance');
  el.viewLobby?.classList.toggle('hidden', name !== 'lobby');
  // el.viewGame は廃止
  el.viewMatch?.classList.toggle('hidden', name !== 'match');
  if (name === 'lobby') {
    try { startLobbySSE(); } catch {}
  }
}

function initApp() {
  // Restore username if present
  try {
    const saved = localStorage.getItem('cw_username');
    if (saved) APP.username = saved;
  } catch {}
  if (APP.username && el.usernameInput) el.usernameInput.value = APP.username;
  showView('entrance');

  // Entrance events
  el.enterLobby?.addEventListener('click', () => {
    const name = (el.usernameInput?.value || '').trim();
    if (!name) { alert('ユーザ名を入力してください'); return; }
    APP.username = name;
    try { localStorage.setItem('cw_username', name); } catch {}
    if (el.lobbyUser) el.lobbyUser.textContent = `ユーザ: ${APP.username}`;
    showView('lobby');
  });
  el.usernameInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') el.enterLobby?.click();
  });

  // PvEロビーUIは廃止（常にPvPロビー）
  try { startLobbySSE(); } catch {}

  // PvP lobby actions
  el.btnCreateMatch?.addEventListener('click', createMatch);
  // CPU (PvE via match engine) quick buttons
  document.getElementById('btnCreateCpuEasy')?.addEventListener('click', () => createCpuMatch('easy'));
  document.getElementById('btnCreateCpuMedium')?.addEventListener('click', () => createCpuMatch('normal'));
  document.getElementById('btnCreateCpuHard')?.addEventListener('click', () => createCpuMatch('hard'));

  // Match room actions
  el.btnLeaveMatch?.addEventListener('click', leaveMatchToLobby);
  el.btnSubmitReady?.addEventListener('click', submitReady);
  el.matchCanvas?.addEventListener('click', onMatchCanvasClick);
  // PvP hover highlight
  el.matchCanvas?.addEventListener('mousemove', onMatchCanvasMove);
  el.matchCanvas?.addEventListener('mouseleave', onMatchCanvasLeave);
  // Match modes
  el.btnMatchModeMove?.addEventListener('click', () => setMatchMode('move'));
  el.btnMatchModeLaunch?.addEventListener('click', () => setMatchMode('launch'));
}

// 旧PvE（Single）フローは廃止

// === PvP (Match) ===
async function createMatch() {
  const name = APP.username || 'Player';
  try {
    const res = await fetch('/v1/match/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'pvp', display_name: name }) });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const json = await res.json();
    APP.match = { id: json.match_id, token: json.player_token, side: json.side };
    openMatchRoom();
  } catch (e) {
    alert('マッチ作成に失敗しました');
  }
}

async function createCpuMatch(difficulty = 'normal') {
  const name = APP.username || 'Player';
  try {
    const body = { mode: 'pve', display_name: name, config: { difficulty } };
    const res = await fetch('/v1/match/', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const json = await res.json();
    APP.match = { id: json.match_id, token: json.player_token, side: json.side };
    openMatchRoom();
  } catch (e) {
    alert('CPUマッチ作成に失敗しました');
  }
}

async function refreshMatchList() {
  try {
    const res = await fetch('/v1/match/');
    if (!res.ok) throw new Error(`status ${res.status}`);
    const json = await res.json();
    renderMatchList(json.matches || []);
  } catch (e) {
    if (el.matchList) el.matchList.textContent = '取得に失敗しました';
  }
}

function renderMatchList(matches) {
  if (!el.matchList) return;
  if (!matches.length) { el.matchList.classList.add('empty'); el.matchList.textContent = '参加待ちのマッチはありません'; return; }
  el.matchList.classList.remove('empty');
  el.matchList.innerHTML = matches.map(m => {
    const open = m.has_open_slot && m.status !== 'over';
    return `<div class="match-card">
      <div>
        <div class="mono">${m.match_id.slice(0,8)}</div>
        <div class="meta">${m.status} ・ turn? ・ ${open ? '募集中' : '満席'}</div>
      </div>
      <div>
        <button data-join="${m.match_id}" ${open ? '' : 'disabled'}>参加</button>
      </div>
    </div>`;
  }).join('');
  el.matchList.querySelectorAll('button[data-join]').forEach(btn => {
    btn.addEventListener('click', () => joinMatch(btn.getAttribute('data-join')));
  });
}

// Lobby SSE (PvP tab)
let LOBBY_SSE = null;
function startLobbySSE() {
  stopLobbySSE();
  try {
    const es = new EventSource('/v1/match/events');
    LOBBY_SSE = es;
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        // payload.matches should be an array when present — check explicitly
        if (payload && Array.isArray(payload.matches)) {
          renderMatchList(payload.matches);
        }
      } catch {}
    };
    es.addEventListener('list', (ev) => {
      try { const js = JSON.parse(ev.data); renderMatchList(js.matches || []); } catch {}
    });
    es.onerror = () => {
      handleSSEDisconnect('lobby');
    };
  } catch (e) {
    if (el.matchList) el.matchList.textContent = 'ロビーSSE初期化エラー';
  }
}
function stopLobbySSE() { try { if (LOBBY_SSE) LOBBY_SSE.close(); } catch {}; LOBBY_SSE = null; }

// Unified SSE disconnect handler: alert and return to entrance
function handleSSEDisconnect(context) {
  try { stopMatchSSE(); } catch {}
  try { stopLobbySSE(); } catch {}
  // Best-effort: inform server we left the match
  try {
    if (APP.match) {
      fetch(`/v1/match/${APP.match.id}/leave?token=${encodeURIComponent(APP.match.token)}`, { method: 'POST' }).catch(()=>{});
    }
  } catch {}
  APP.match = null;
  try { alert('通信がきれました'); } catch {}
  showView('entrance');
}

async function joinMatch(matchId) {
  const name = APP.username || 'Player';
  try {
    const res = await fetch(`/v1/match/${matchId}/join`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ display_name: name }) });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const json = await res.json();
    APP.match = { id: json.match_id, token: json.player_token, side: json.side };
    openMatchRoom();
  } catch (e) {
    alert('参加に失敗しました');
    refreshMatchList();
  }
}

function openMatchRoom() {
  // keep lobby SSE alive; open match SSE additionally
  showView('match');
  // 初期表示時点ではSSE未確立のため操作を一旦無効化
  try { updateMatchControls(); } catch {}
  // ログ表示を初期化
  try { clearMatchLog(); APP.matchLoggedUpToTurn = 0; } catch {}
  startMatchSSE();
}

function leaveMatchToLobby() {
  stopMatchSSE();
  // best-effort tell server we're leaving this match
  try {
    if (APP.match) {
      fetch(`/v1/match/${APP.match.id}/leave?token=${encodeURIComponent(APP.match.token)}`, { method: 'POST' }).catch(()=>{});
    }
  } catch {}
  APP.match = null;
  showView('lobby');
  // ensure lobby SSE is running persistently
  try { startLobbySSE(); } catch {}
}

async function submitReady() {
  if (!APP.match) return;
  // allow submit whenever match is active; server will resolve when both are present
  try {
    const s = APP.matchState || {};
    if (!(s && s.status === 'active')) return;
  } catch {}
  try {
    // send staged orders only (avoid overriding with empty {})
    const staged = {};
    if (APP.matchPending?.carrier_target) staged.carrier_target = APP.matchPending.carrier_target;
    if (APP.matchPending?.launch_target) staged.launch_target = APP.matchPending.launch_target;
    const res = await fetch(`/v1/match/${APP.match.id}/orders`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ player_token: APP.match.token, player_orders: staged }) });
    if (!res.ok) throw new Error(`status ${res.status}`);
    const js = await res.json();
    // バリデーション失敗時はサーバのlogsをUIへ表示し、保留オーダは維持
    if (js && js.accepted === false) {
      try {
        if (Array.isArray(js.logs)) {
          for (const m of js.logs) matchLogMsg(`[NG] ${m}`);
        } else {
          matchLogMsg('[NG] 注文が受理されませんでした');
        }
      } catch {}
      return;
    }
    // keep staged orders to allow resubmission/edit until resolve（必要に応じて上書き可能）
  } catch (e) {
    alert('送信に失敗しました');
  }
}
// === SSE for match ===
function startMatchSSE() {
  stopMatchSSE();
  if (!APP.match) return;
  try {
    const url = `/v1/match/${APP.match.id}/events?token=${encodeURIComponent(APP.match.token)}`;
    const es = new EventSource(url);
    APP.matchSSE = es;
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload && payload.type === 'state') {
          if (el.matchStatusLbl) el.matchStatusLbl.textContent = payload.status;
          if (el.matchTurnLbl) el.matchTurnLbl.textContent = payload.turn;
          if (el.matchWaitingLbl) el.matchWaitingLbl.textContent = payload.waiting_for;
          handleMatchStateUpdate(payload);
        }
      } catch {}
    };
    es.addEventListener('state', (ev) => {
      try {
        const js = JSON.parse(ev.data);
        if (el.matchStatusLbl) el.matchStatusLbl.textContent = js.status;
        if (el.matchTurnLbl) el.matchTurnLbl.textContent = js.turn;
        if (el.matchWaitingLbl) el.matchWaitingLbl.textContent = js.waiting_for;
        handleMatchStateUpdate(js);
      } catch {}
    });
    es.onerror = () => {
      handleSSEDisconnect('match');
    };
  if (el.matchSideLbl) el.matchSideLbl.textContent = APP.match.side;
    if (el.matchInfo) el.matchInfo.textContent = `side=${APP.match.side} / token=${APP.match.token.slice(0,8)}...`;
  } catch (e) {
    if (el.matchInfo) el.matchInfo.textContent = 'SSE初期化エラー';
  }
}

function stopMatchSSE() {
  try { if (APP.matchSSE) { APP.matchSSE.close(); } } catch {}
  APP.matchSSE = null;
}

// === Match board rendering (simple grid) ===
function renderMatchView() {
  const st = APP.matchState; const cv = el.matchCanvas; if (!st || !cv) return;
  const W = st.map_w || 30, H = st.map_h || 30;
  // keep map grid if provided in snapshot
  if (st.map) APP.matchMap = st.map;
  const getTile = (x,y) => {
    const m = APP.matchMap; if (!m) return 0; const row = m[y]; if (!row) return 0; const v = row[x]; return v||0;
  };
  if (!APP.matchHex || APP.matchHex.W !== W || APP.matchHex.H !== H || APP.matchHex.canvas !== cv) {
    APP.matchHex = makeHexRenderer(cv, W, H, getTile);
  }
  else { APP.matchHex.getTileFn = getTile; }
  const me = (st && st.units && st.units.carrier) || {};
  const op = (st && st.intel && st.intel.carrier) || {};
  APP.matchHex.renderBackground();
  // Visibility overlay (PvP): server-provided per-side turn_visible under a/b
  try {
    const mineObj = st && st.units;
    const visList = (mineObj && Array.isArray(mineObj.turn_visible)) ? mineObj.turn_visible : [];
    // Normalize to Set("x,y") accepting both "x,y" and "x=..,y=.." formats
    const visSet = new Set();
    for (const v of visList) {
      if (typeof v === 'string') {
        const m = v.match(/(-?\d+)\s*,\s*(-?\d+)/);
        if (m) { visSet.add(`${parseInt(m[1],10)},${parseInt(m[2],10)}`); continue; }
        const m2 = v.match(/x\s*=\s*(-?\d+)\s*,\s*y\s*=\s*(-?\d+)/i);
        if (m2) { visSet.add(`${parseInt(m2[1],10)},${parseInt(m2[2],10)}`); continue; }
      } else if (v && typeof v === 'object' && typeof v.x === 'number' && typeof v.y === 'number') {
        visSet.add(`${v.x},${v.y}`);
      }
    }
    if (visSet.size > 0 && APP.matchHex.renderVisibilityOverlay) {
      APP.matchHex.renderVisibilityOverlay(visSet);
    }
  } catch {}
  const mySide = (APP.match && APP.match.side) || 'A';
  // 自分と相手を固定色で描画（side依存なし）
  APP.matchHex.drawCarrier(me.x, me.y, getCss('--carrier')||'#4aa3ff', me.hp, 100);
  APP.matchHex.drawCarrier(op.x, op.y, getCss('--enemy')||'#ff6464', op.hp, 100);
  // pending move preview line
  const mine = me;
  const pending = APP.matchPending?.carrier_target;
  const srvTarget = mine && mine.target;
  if (mine && mine.x!=null && mine.y!=null) {
    if (pending && pending.x!=null && pending.y!=null && (pending.x !== mine.x || pending.y !== mine.y)) {
      // クライアント側の保留オーダーがあればそれを優先表示
      APP.matchHex.drawLine(mine.x, mine.y, pending.x, pending.y, 'rgba(106,212,255,0.5)');
    } else if (srvTarget && srvTarget.x!=null && srvTarget.y!=null && (srvTarget.x !== mine.x || srvTarget.y !== mine.y)) {
      // 保留が無ければサーバ提供のtargetで進路表示
      APP.matchHex.drawLine(mine.x, mine.y, srvTarget.x, srvTarget.y, 'rgba(106,212,255,0.35)');
    }
  }
  // draw my squadrons (own side only)
  const mySqs = st.units && st.units.squadrons;
  if (Array.isArray(mySqs)) {
    for (const s of mySqs) {
      if (!s || s.state === 'base' || s.state === 'lost') continue;
      if (s.x == null || s.y == null) continue;
      APP.matchHex.drawSquadron(s.x, s.y, getCss('--squad')||'#f2c14e', s.hp||40, 40);
      // 航空機: 保留オーダー（発艦）よりもサーバtargetを表示（保留はキャリア→目標で別線を描画）
      if (s.target && s.target.x!=null && s.target.y!=null) {
        APP.matchHex.drawLine(s.x, s.y, s.target.x, s.target.y, 'rgba(242,193,78,0.4)');
      }
    }
  }
  // draw enemy squadrons (visible/intel this turn): 赤＋菱形
  const oppSqs = st.intel && st.intel.squadrons;
  if (Array.isArray(oppSqs)) {
    for (const s of oppSqs) {
      // 相手側は to_payload() 由来で今ターン発見したもののみ x,y を持つ
      if (!s || s.x == null || s.y == null) continue;
      // 敵編隊の現在地（形: 菱形 / 色: 赤）
      if (APP.matchHex.drawDiamond) {
        APP.matchHex.drawDiamond(s.x, s.y, getCss('--enemy')||'#ff6464');
      } else {
        APP.matchHex.drawSquadron(s.x, s.y, getCss('--enemy')||'#ff6464', s.hp||40, 40);
      }
      // 索敵で得た移動痕跡（first -> last）
      if (s.x0 != null && s.y0 != null) {
        // 視認性向上のためやや濃い白線
        if (s.x0 !== s.x || s.y0 !== s.y) {
          APP.matchHex.drawLine(s.x0, s.y0, s.x, s.y, 'rgba(255,255,255,0.65)');
        }
      }
    }
  }
  // pending launch preview line (from carrier to target)
  const pendingLaunch = APP.matchPending?.launch_target;
  if (pendingLaunch && mine && mine.x!=null && mine.y!=null) {
    APP.matchHex.drawLine(mine.x, mine.y, pendingLaunch.x, pendingLaunch.y, 'rgba(242,193,78,0.5)');
  }
  // hover highlight（共通化）
  try { APP.matchHex.renderHoverOutline({ mode: MATCH_MODE, hover: APP.matchHover, carrier: mine, squadrons: st.units?.squadrons }); } catch {}
}

// Enable/disable controls based on match status and readiness
function updateMatchControls() {
  const s = APP.matchState || {};
  const status = s.status;
  const wait = s.waiting_for;
  const disableAll = !APP.match || !s || status !== 'active';
  // 編集/送信は「あなたの番」または「双方未提出（orders）」で可能
  const canSubmit = !disableAll && (wait === 'you' || wait === 'orders');
  const canEdit = !disableAll && (wait === 'you' || wait === 'orders');
  if (el.btnMatchModeMove) el.btnMatchModeMove.disabled = disableAll || !canEdit;
  if (el.btnMatchModeLaunch) el.btnMatchModeLaunch.disabled = disableAll || !canEdit;
  if (el.btnSubmitReady) el.btnSubmitReady.disabled = disableAll || !canSubmit;

  // 強調表示（activeクラス）は編集可能な時のみ付与、待機中や受付中は外す
  try {
    if (el.btnMatchModeMove) el.btnMatchModeMove.classList.toggle('active', !disableAll && canEdit && MATCH_MODE === 'move');
    if (el.btnMatchModeLaunch) el.btnMatchModeLaunch.classList.toggle('active', !disableAll && canEdit && MATCH_MODE === 'launch');
  } catch {}
}

function onMatchCanvasClick(ev) {
  if (!APP.match || !APP.matchState) return;
  if (!APP.matchHex) return;
  // respect editability
  const s = APP.matchState || {};
  const status = s.status;
  const canEdit = (status === 'active') && (s && (s.waiting_for === 'you' || s.waiting_for === 'orders'));
  const t = APP.matchHex.tileFromEvent(ev);
  if (!t) return;
  if (MATCH_MODE === 'move') {
    if (!canEdit) return;
    APP.matchPending = { ...(APP.matchPending||{}), carrier_target: { x: t.x, y: t.y } };
    renderMatchView(); updateMatchPanels();
  } else if (MATCH_MODE === 'launch') {
    if (!canEdit) return;
    APP.matchPending = { ...(APP.matchPending||{}), launch_target: { x: t.x, y: t.y } };
    renderMatchView(); updateMatchPanels();
  }
  // Update topbar summary: show side, match status and waiting info
  // topbar update moved to updateMatchPanels
}

function onMatchCanvasMove(ev) {
  if (!APP.match || !APP.matchHex) return;
  const t = APP.matchHex.tileFromEvent(ev);
  APP.matchHover = t;
  renderMatchView();
}

function onMatchCanvasLeave() {
  APP.matchHover = null;
  renderMatchView();
}

async function submitOrders(orders) {
  if (!APP.match) return;
  try {
    const res = await fetch(`/v1/match/${APP.match.id}/orders`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ player_token: APP.match.token, player_orders: orders||{} }) });
    if (!res.ok) throw new Error(`status ${res.status}`);
  } catch (e) {
    alert('注文送信に失敗しました');
  }
}

// === Match UI helpers ===
let MATCH_MODE = 'move';
function setMatchMode(m) {
  MATCH_MODE = m;
  if (el.btnMatchModeMove) el.btnMatchModeMove.classList.toggle('active', m === 'move');
  if (el.btnMatchModeLaunch) el.btnMatchModeLaunch.classList.toggle('active', m === 'launch');
  if (el.matchHint) {
    try {
      if (m === 'move') {
        const spd = APP.matchState?.units?.carrier?.speed;
        el.matchHint.textContent = (typeof spd === 'number')
          ? `目的地をクリック（移動速度: ${spd}）`
          : '目的地をクリック';
      } else if (m === 'launch') {
        const sqs = APP.matchState?.units?.squadrons || [];
        const bases = Array.isArray(sqs) ? sqs.filter(x => x && x.state === 'base' && (x.hp ?? 0) > 0) : [];
        const maxFuel = bases.length ? Math.max(...bases.map(x => (typeof x.fuel === 'number') ? x.fuel : 0)) : null;
        el.matchHint.textContent = (maxFuel != null)
          ? `目標地点をクリック（最大航続距離: ${maxFuel}。サーバ側で検証）`
          : '目標地点をクリック（発艦可能な編隊がありません）';
      }
    } catch { el.matchHint.textContent = (m === 'move') ? '目的地をクリック' : '目標地点をクリック'; }
  }
}

// 共通: マッチ状態の更新時にターン進行を検知して保留オーダーをクリア
function handleMatchStateUpdate(nextState) {
  try {
    const prevTurn = (APP.matchState && typeof APP.matchState.turn === 'number') ? APP.matchState.turn : null;
    const nextTurn = (nextState && typeof nextState.turn === 'number') ? nextState.turn : null;
    if (prevTurn != null && nextTurn != null && nextTurn > prevTurn) {
      // ターンが進んだので保留中のオーダーをクリア
      APP.matchPending = { carrier_target: null, launch_target: null };
    }
    // PvPログ: サーバstateに含まれるlogsを、同一ターンにつき1回だけ追記
    if (typeof nextTurn === 'number' && nextTurn > (APP.matchLoggedUpToTurn || 0)) {
      if (Array.isArray(nextState.logs)) {
        for (const m of nextState.logs) matchLogMsg(m);
      }
      APP.matchLoggedUpToTurn = nextTurn;
    }
  } catch {}
  APP.matchState = nextState;
  renderMatchView();
  updateMatchPanels();
  updateMatchControls();
}

function updateMatchPanels() {
  if (!APP.matchState || !el.matchCarrierStatus) return;
  const s = APP.matchState || {};
  const mineC = s.units?.carrier || {}, oppC = s.intel?.carrier || {};
  const mySide = (APP.match?.side === 'A') ? 'A' : 'B';
  const mine = mineC;
  const opp = oppC;
  // own squadrons for counts
  const mySqs = s.units && s.units.squadrons;
  const sqArr = Array.isArray(mySqs) ? mySqs : [];
  const baseAvail = sqArr.filter((x) => x && x.state === 'base' && (x.hp ?? 0) > 0);
  const totalSlots = sqArr.length; // hangar 相当
  const hpNow = (typeof mine.hp === 'number') ? mine.hp : '-';
  const hpMax = CARRIER_MAX_HP;
  const onboard = `${baseAvail.length} / ${totalSlots || '-'}`;
  const hpLine = `${hpNow} / ${hpMax}`;
  const spd = (typeof mine.speed === 'number') ? `${mine.speed}` : '-';
  const maxFuel = baseAvail.length ? Math.max(...baseAvail.map((x) => x && typeof x.fuel === 'number' ? x.fuel : 0)) : null;
  const fuelLine = (maxFuel != null) ? `${maxFuel}` : '-';
  el.matchCarrierStatus.innerHTML = `
    <div class="kv">
      <div>HP</div><div>${hpLine}</div>
      <div>速度</div><div>${spd}</div>
      <div>航空部隊</div><div>${onboard}</div>
      <div>出撃航続距離</div><div>${fuelLine}</div>
    </div>
  `;

  // Squadrons panel (own side only)
  if (el.matchSquadronList) {
    const arr = sqArr;
    if (arr.length === 0) {
      el.matchSquadronList.textContent = '編隊はありません';
    } else {
      const rows = arr.map((sq) => {
        const st = sq.state || '-';
        const hpNow = (typeof sq.hp === 'number') ? sq.hp : '-';
        const hpMax = SQUAD_MAX_HP;
        // 表示名は SQ番号のみ（C1SQ1 -> SQ1）
        let name = String(sq.id || '');
        const m = name.match(/(SQ\d+)$/);
        if (m) name = m[1];
        const spd = (typeof sq.speed === 'number') ? sq.speed : '-';
        const fuel = (typeof sq.fuel === 'number') ? sq.fuel : '-';
        const line1 = `<div class="kv"><div class="mono">${name}</div><div>${hpNow} / ${hpMax}</div></div>`;
        const line2 = `<div class="kv"><div>状態</div><div>${st}</div></div>`;
        const line3 = `<div class="kv"><div>速度</div><div>${spd}</div></div>`;
        const line4 = `<div class="kv"><div>燃料</div><div>${fuel}</div></div>`;
        return `${line1}${line2}${line3}${line4}`;
      }).join('');
      el.matchSquadronList.innerHTML = `<div class="list">${rows}</div>`;
    }
  }
  // Status text mapping
  try {
    const side = APP.match?.side ? APP.match.side : '-';
    let phase = '-';
    if (s.status === 'waiting') {
      phase = '参加受付中';
    } else if (s.status === 'active') {
      if (s.waiting_for === 'orders') phase = 'オーダー受付中';
      else if (s.waiting_for === 'you') phase = 'あなたのオーダ入力待ち';
      else if (s.waiting_for === 'opponent') phase = '相手のオーダ完了待ち';
      else phase = 'ターン解決中';
    } else if (s.status === 'over') {
      let result = s.result || null;
      if (!result) {
        const myHp = mine && typeof mine.hp === 'number' ? mine.hp : null;
        const opHp = opp && typeof opp.hp === 'number' ? opp.hp : null;
        if (myHp != null && opHp != null) {
          if (myHp <= 0 && opHp <= 0) result = 'draw';
          else if (myHp <= 0) result = 'lose';
          else if (opHp <= 0) result = 'win';
        }
      }
      phase = result === 'win' ? 'ゲーム終了（あなたの勝ち）'
           : result === 'lose' ? 'ゲーム終了（あなたの負け）'
           : 'ゲーム終了（引き分け）';
    }
    if (el.matchInfo) el.matchInfo.textContent = `あなたは ${side} 側 • ${phase}`;
  } catch (e) {}
}

// === PvP Log ===
function clearMatchLog() {
  if (!el.matchLog) return;
  el.matchLog.innerHTML = '';
}

function matchLogMsg(msg) {
  if (!el.matchLog) return;
  const ts = new Date().toLocaleTimeString('ja-JP', { hour12: false });
  const line = document.createElement('div');
  line.className = 'entry';
  line.innerHTML = `<span class="ts">[${ts}]</span>${escapeHtml(String(msg))}`;
  el.matchLog.appendChild(line);
  el.matchLog.scrollTop = el.matchLog.scrollHeight;
}

// 旧: drawHpBarRect は Hex レンダラ内のHP描画へ統合

// === Shared hex renderer (for PvP, non-invasive to PvE) ===
function makeHexRenderer(canvas, W, H, getTileFn) {
  // local renderer state
  const SQ3 = Math.sqrt(3);
  let HEX = 10, ORX = 0, ORY = 0;
  const ctx = canvas.getContext('2d');
  function compute() {
    const sizeByW = canvas.width / (SQ3 * (W + 0.5));
    HEX = Math.max(5, Math.floor(sizeByW));
    const mapPixelW = SQ3 * HEX * (W + 0.5);
    const mapPixelH = 1.5 * HEX * (H - 1) + 2 * HEX;
    canvas.width = Math.ceil(mapPixelW);
    canvas.height = Math.ceil(mapPixelH);
    ORX = HEX; ORY = HEX;
  }
  compute();
  function offsetToPixel(col, row) {
    const x = HEX * (SQ3 * (col + 0.5 * (row & 1))) + ORX;
    const y = HEX * (1.5 * row) + ORY; return [x, y];
  }
  function hexPolygon(cx, cy, size) {
    const pts = []; for (let i=0;i<6;i++){ const ang = Math.PI/180*(60*i-30); pts.push([cx + size*Math.cos(ang), cy + size*Math.sin(ang)]);} return pts;
  }
  function renderBackground() {
    ctx.clearRect(0,0,canvas.width, canvas.height);
    ctx.fillStyle = getCss('--water'); ctx.fillRect(0,0,canvas.width, canvas.height);
    for (let r=0;r<H;r++){
      for (let c=0;c<W;c++){
        const [px,py] = offsetToPixel(c,r); const poly = hexPolygon(px,py,HEX);
        ctx.beginPath(); ctx.moveTo(poly[0][0], poly[0][1]); for (let i=1;i<poly.length;i++) ctx.lineTo(poly[i][0], poly[i][1]); ctx.closePath();
        ctx.fillStyle = (getTileFn(c,r)===1) ? getCss('--island') : getCss('--water'); ctx.fill();
        ctx.strokeStyle = getCss('--grid'); ctx.lineWidth = 1; ctx.stroke();
      }
    }
  }
  // Visibility overlay: fill a soft highlight on tiles contained in visSet (keys: "x,y")
  function renderVisibilityOverlay(visSet, color='rgba(255,255,255,0.14)') {
    if (!visSet || visSet.size === 0) return;
    for (let r = 0; r < H; r++) {
      for (let c = 0; c < W; c++) {
        if (!visSet.has(`${c},${r}`)) continue;
        const [px, py] = offsetToPixel(c, r);
        const poly = hexPolygon(px, py, HEX);
        ctx.beginPath();
        ctx.moveTo(poly[0][0], poly[0][1]);
        for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0], poly[i][1]);
        ctx.closePath();
        ctx.fillStyle = color;
        ctx.fill();
      }
    }
  }
  function drawCarrier(x,y,color,hp,max){ if(x==null||y==null) return; const [cx0,cy0]=offsetToPixel(x,y); const cx=cx0-HEX, cy=cy0-HEX; ctx.strokeStyle='rgba(0,0,0,0.85)'; ctx.lineWidth=4; ctx.strokeRect(cx+3,cy+3,HEX*2-6,HEX*2-6); ctx.fillStyle=color; ctx.fillRect(cx+4,cy+4,HEX*2-8,HEX*2-8); ctx.strokeStyle='rgba(255,255,255,0.35)'; ctx.lineWidth=1.5; ctx.strokeRect(cx+4,cy+4,HEX*2-8,HEX*2-8); drawHp(cx0,cy0,hp,max,color==='red'?'#ff9a9a':'#6ad4ff'); }
  function drawCarrierStyled(x,y,color,{memory=false}={}){ if(x==null||y==null) return; const [cx0,cy0]=offsetToPixel(x,y); const cx=cx0-HEX, cy=cy0-HEX; ctx.save(); if (memory) ctx.globalAlpha = 0.55; ctx.strokeStyle='rgba(0,0,0,0.85)'; ctx.lineWidth=4; ctx.strokeRect(cx+3,cy+3,HEX*2-6,HEX*2-6); ctx.fillStyle=color; ctx.fillRect(cx+4,cy+4,HEX*2-8,HEX*2-8); ctx.strokeStyle='rgba(255,255,255,0.35)'; ctx.lineWidth=1.5; if (memory) ctx.setLineDash([4,3]); ctx.strokeRect(cx+4,cy+4,HEX*2-8,HEX*2-8); ctx.restore(); }
  function drawSquadron(x,y,color,hp,max){ if(x==null||y==null) return; const [px,py]=offsetToPixel(x,y); const r=Math.max(4, Math.round(HEX*0.6));
    ctx.beginPath(); ctx.arc(px, py, r+2, 0, Math.PI*2); ctx.strokeStyle='rgba(0,0,0,0.85)'; ctx.lineWidth=4; ctx.stroke();
    ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI*2); ctx.fillStyle=color; ctx.fill();
    ctx.strokeStyle='rgba(255,255,255,0.35)'; ctx.lineWidth=1.5; ctx.stroke();
    const w=r*1.6, h=3; const x0=Math.round(px - w/2), y0=Math.round(py - r - 6); const ratio=Math.max(0,Math.min(1,(hp||max)/max));
    ctx.fillStyle='rgba(0,0,0,0.6)'; ctx.fillRect(x0,y0,w,h); ctx.fillStyle='#f2c14e'; ctx.fillRect(x0,y0,Math.round(w*ratio),h);
  }
  function drawDiamond(x,y,color){ if(x==null||y==null) return; const [px,py]=offsetToPixel(x,y); const r=Math.max(4, Math.round(HEX*0.6));
    const pts=[[px,py-r],[px+r,py],[px,py+r],[px-r,py]];
    ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]); for (let i=1;i<pts.length;i++) ctx.lineTo(pts[i][0], pts[i][1]); ctx.closePath();
    // halo
    ctx.strokeStyle='rgba(0,0,0,0.85)'; ctx.lineWidth=4; ctx.stroke();
    // fill
    ctx.fillStyle=color; ctx.fill();
    // border
    ctx.strokeStyle='rgba(255,255,255,0.35)'; ctx.lineWidth=1.5; ctx.stroke();
  }
  function drawDiamondStyled(x,y,color,{memory=false}={}){ if(x==null||y==null) return; const [px,py]=offsetToPixel(x,y); const r=Math.max(4, Math.round(HEX*0.6)); const pts=[[px,py-r],[px+r,py],[px,py+r],[px-r,py]]; ctx.save(); if (memory) ctx.globalAlpha = 0.55; ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]); for (let i=1;i<pts.length;i++) ctx.lineTo(pts[i][0], pts[i][1]); ctx.closePath(); ctx.strokeStyle='rgba(0,0,0,0.85)'; ctx.lineWidth=4; ctx.stroke(); ctx.fillStyle=color; ctx.fill(); ctx.strokeStyle='rgba(255,255,255,0.35)'; ctx.lineWidth=1.5; if (memory) ctx.setLineDash([4,3]); ctx.stroke(); ctx.restore(); }
  function drawLine(x1,y1,x2,y2,color){ ctx.strokeStyle=color; ctx.lineWidth=2; ctx.beginPath(); const [sx,sy]=offsetToPixel(x1,y1); const [tx,ty]=offsetToPixel(x2,y2); ctx.moveTo(sx,sy); ctx.lineTo(tx,ty); ctx.stroke(); }
  function drawHp(px,py,hp,max,color){ if(hp==null||max==null) return; const w=HEX*1.6,h=4; const x=Math.round(px - w/2), y=Math.round(py - HEX + 3); const ratio=Math.max(0,Math.min(1,hp/max)); ctx.fillStyle='rgba(0,0,0,0.6)'; ctx.fillRect(x,y,w,h); ctx.fillStyle=color; ctx.fillRect(x,y,Math.round(w*ratio),h); }
  function drawHexOutline(c, r, color, width=2, dash=null) {
    if (c==null || r==null) return;
    const [px, py] = offsetToPixel(c, r);
    const poly = hexPolygon(px, py, HEX);
    ctx.save();
    if (Array.isArray(dash)) ctx.setLineDash(dash);
    ctx.beginPath();
    ctx.moveTo(poly[0][0], poly[0][1]);
    for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0], poly[i][1]);
    ctx.closePath();
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
    ctx.restore();
  }
  function tileFromEvent(e){ const rect=canvas.getBoundingClientRect(); const scaleX = canvas.width/rect.width, scaleY = canvas.height/rect.height; const mx=(e.clientX-rect.left)*scaleX, my=(e.clientY-rect.top)*scaleY; const [qf, rf] = pixelToAxial(mx,my); const { q, r } = axialRound(qf, rf); const off = axialToOffset(q,r); const c=off.col, rr=off.row; if (c<0||rr<0||c>=W||rr>=H) return null; return {x:c,y:rr}; }
  function pixelToAxial(px,py){ const x=(px-ORX)/HEX, y=(py-ORY)/HEX; const q=(Math.sqrt(3)/3)*x - (1/3)*y; const r=(2/3)*y; return [q,r]; }
  function axialToCube(q,r){ return { x:q, z:r, y:-q-r }; }
  function cubeToAxial(x,y,z){ return { q:x, r:z }; }
  function cubeRound(x,y,z){ let rx=Math.round(x), ry=Math.round(y), rz=Math.round(z); const dx=Math.abs(rx-x), dy=Math.abs(ry-y), dz=Math.abs(rz-z); if (dx>dy && dx>dz) rx = -ry - rz; else if (dy>dz) ry = -rx - rz; else rz = -rx - ry; return { x:rx, y:ry, z:rz }; }
  function axialRound(q,r){ const cr=cubeRound(q, -q - r, r); const ar=cubeToAxial(cr.x, cr.y, cr.z); return { q: ar.q, r: ar.r }; }
  function axialToOffset(q,r){ const col = q + ((r - (r & 1)) >> 1); const row = r; return { col, row };
  }
  // Range outline (ring of distance=range around center)
  function drawRangeOutline(cx, cy, range, color) {
    const pts = [];
    const [pcx, pcy] = offsetToPixel(cx, cy);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        if (hexDistance({ x, y }, { x: cx, y: cy }) === range) {
          const [px, py] = offsetToPixel(x, y);
          const ang = Math.atan2(py - pcy, px - pcx);
          pts.push({ px, py, ang });
        }
      }
    }
    if (pts.length < 6) return; // not enough to draw a ring
    pts.sort((a, b) => a.ang - b.ang);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(pts[0].px, pts[0].py);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].px, pts[i].py);
    ctx.closePath();
    ctx.stroke();
    ctx.restore();
  }

  // Unified hover outline with validity coloring
  function renderHoverOutline({ mode, hover, carrier, squadrons }) {
    if (!hover || hover.x == null || hover.y == null) return;
    const c = hover.x, r = hover.y;
    let color = '#ffffff';
    if (mode === 'launch') {
      const arr = Array.isArray(squadrons) ? squadrons : [];
      const bases = arr.filter(x => x && x.state === 'base' && (x.hp ?? 0) > 0);
      const hasAny = bases.length > 0;
      const maxFuel = hasAny ? Math.max(...bases.map(x => (typeof x.fuel === 'number') ? x.fuel : 0)) : 0;
      const onSea = (getTileFn(c, r) === 0);
      let outOfRange = true;
      if (typeof carrier?.x === 'number' && typeof carrier?.y === 'number' && hasAny) {
        const d = hexDistance({ x: c, y: r }, { x: carrier.x, y: carrier.y });
        outOfRange = !(d <= maxFuel);
      }
      if (!onSea || !hasAny || outOfRange) color = '#ff5c5c';
    } else if (mode === 'move') {
      if (getTileFn(c, r) === 1) color = '#ff5c5c';
    }
    drawHexOutline(c, r, color, 2);
  }

  return { canvas, W, H, renderBackground, renderVisibilityOverlay, drawCarrier, drawCarrierStyled, drawSquadron, drawDiamond, drawDiamondStyled, tileFromEvent, drawLine, drawHexOutline, drawRangeOutline, renderHoverOutline, getTileFn };
}

// === Panels ===
function updatePanels() {
  const c = state.carrier;
  el.carrierStatus.innerHTML = `
    <div class="kv">
      <div>HP</div><div>${c.hp}</div>
      <div>搭載枠</div><div>${c.hangar - countActiveSquadrons()} / ${c.hangar}</div>
    </div>
  `;

  if (state.squadrons.filter((s)=>s.state!=='lost').length === 0) {
    el.squadronList.textContent = '出撃中の編隊はありません';
  } else {
    el.squadronList.innerHTML = state.squadrons.map((s) => {
      return `<div class="kv">
        <div>ID</div><div class="mono">${s.id}</div>
        <div>状態</div><div>${labelSqState(s.state)}</div>
        <div>HP</div><div>${s.hp ?? SQUAD_MAX_HP}</div>
      </div>`;
    }).join('');
  }
}

function labelSqState(st) {
  switch (st) {
    case 'outbound': return '出撃中';
    case 'engaging': return '接敵';
    case 'attack': return '攻撃';
    case 'returning': return '帰還中';
    default: return st;
  }
}

function countActiveSquadrons() { return state.squadrons.filter((s)=>s.state!=='base' && s.state!=='lost').length; }
function countBaseAvailable() { return state.squadrons.filter((s)=>s.state==='base' && s.hp>0).length; }
function countActiveEnemySquadrons() { return state.enemy.squadrons.filter((s)=>s.state!=='base' && s.state!=='lost').length; }

// === Interaction ===
// === Utils (shared) ===

function hexDistance(a, b) {
  const aa = offsetToAxial(a.x, a.y); const bb = offsetToAxial(b.x, b.y);
  const ac = axialToCube(aa.q, aa.r); const bc = axialToCube(bb.q, bb.r);
  return Math.max(Math.abs(ac.x - bc.x), Math.abs(ac.y - bc.y), Math.abs(ac.z - bc.z));
}

function nextStepOnHexLine(from, to) {
  const A = offsetToAxial(from.x, from.y);
  const B = offsetToAxial(to.x, to.y);
  const Ac = axialToCube(A.q, A.r);
  const Bc = axialToCube(B.q, B.r);
  const N = Math.max(
    Math.abs(Ac.x - Bc.x),
    Math.abs(Ac.y - Bc.y),
    Math.abs(Ac.z - Bc.z)
  );
  if (N <= 0) return null;
  const t = 1 / N;
  const nx = Ac.x + (Bc.x - Ac.x) * t;
  const ny = Ac.y + (Bc.y - Ac.y) * t;
  const nz = Ac.z + (Bc.z - Ac.z) * t;
  const cr = cubeRound(nx, ny, nz);
  const ax = cubeToAxial(cr.x, cr.y, cr.z);
  const off = axialToOffset(ax.q, ax.r);
  return { x: off.col, y: off.row };
}

// 直線上で origin から target の方向に dist だけ進んだ地点（offset座標）を返す
function pointAtHexLineDistance(origin, target, dist) {
  let p = { x: origin.x, y: origin.y };
  for (let i = 0; i < dist; i++) {
    const n = nextStepOnHexLine(p, target);
    if (!n) break; p = n;
  }
  return p;
}

// 航続距離制限を超えていれば、制限内の最近縁点へ切り詰める
function clampTargetToRange(origin, target, maxRange) {
  const d = hexDistance(origin, target);
  if (d <= maxRange) return { x: target.x, y: target.y };
  return pointAtHexLineDistance(origin, target, maxRange);
}

function getCss(varName) { return getComputedStyle(document.documentElement).getPropertyValue(varName).trim(); }
function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

// 指定マスが他ユニットに占有されているか
function isOccupied(x, y, { ignore } = {}) {
  // carriers
  if (!(ignore && ignore.type === 'carrier' && ignore.side === 'player')) {
    if (state.carrier.x === x && state.carrier.y === y) return true;
  }
  if (!(ignore && ignore.type === 'carrier' && ignore.side === 'enemy')) {
    if (state.enemy.carrier.x === x && state.enemy.carrier.y === y) return true;
  }
  // squadrons (player)
  for (const s of state.squadrons) {
    if (ignore && ignore.type === 'squad' && ignore.id === s.id) continue;
    if (s.x === x && s.y === y) return true;
  }
  // squadrons (enemy)
  for (const s of state.enemy.squadrons) {
    if (ignore && ignore.type === 'squad' && ignore.id === s.id) continue;
    if (s.x === x && s.y === y) return true;
  }
  return false;
}

function isVisibleToPlayer(x, y) {
  // If server provided per-turn visibility, prefer it as authoritative.
  if (state.turnVisible && state.turnVisible.size > 0) {
    return isTurnVisible(x, y);
  }
  // Fallback: local visibility calculation (kept for backward compatibility / offline mode)
  if (hexDistance({ x, y }, state.carrier) <= state.carrier.vision) return true;
  for (const sq of state.squadrons) if (sq.state!=='base' && sq.state!=='lost' && hexDistance({ x, y }, sq) <= (sq.vision || VISION_SQUADRON)) return true;
  return false;
}

// Local-only visibility calculation (does NOT consult server-provided turnVisible).
function localIsVisibleToPlayer(x, y) {
  if (hexDistance({ x, y }, state.carrier) <= state.carrier.vision) return true;
  for (const sq of state.squadrons) {
    if (sq.state==='base' || sq.state==='lost') continue;
    if (hexDistance({ x, y }, sq) <= (sq.vision || VISION_SQUADRON)) return true;
  }
  return false;
}

// === Hex helpers ===
function computeHexMetrics() {
  // Choose hex size from current canvas width, then set canvas size to exact map pixel size.
  const W0 = el.canvas.width; // initial/intrinsic width
  const sizeByW = W0 / (SQRT3 * (MAP_W + 0.5));
  HEX_SIZE = Math.max(5, Math.floor(sizeByW));
  const mapPixelW = SQRT3 * HEX_SIZE * (MAP_W + 0.5);
  const mapPixelH = 1.5 * HEX_SIZE * (MAP_H - 1) + 2 * HEX_SIZE;
  el.canvas.width = Math.ceil(mapPixelW);
  el.canvas.height = Math.ceil(mapPixelH);
  // Pack map near top-left with minimal margin (one hex radius)
  ORIGIN_X = HEX_SIZE;
  ORIGIN_Y = HEX_SIZE;
}

function offsetToPixel(col, row) {
  const x = HEX_SIZE * (SQRT3 * (col + 0.5 * (row & 1))) + ORIGIN_X;
  const y = HEX_SIZE * (1.5 * row) + ORIGIN_Y;
  return [x, y];
}

function pixelToAxial(px, py) {
  const x = (px - ORIGIN_X) / HEX_SIZE;
  const y = (py - ORIGIN_Y) / HEX_SIZE;
  const q = (SQRT3 / 3) * x - (1 / 3) * y;
  const r = (2 / 3) * y;
  return [q, r];
}

function axialToCube(q, r) { return { x: q, z: r, y: -q - r }; }
function cubeToAxial(x, y, z) { return { q: x, r: z }; }
function cubeRound(x, y, z) {
  let rx = Math.round(x), ry = Math.round(y), rz = Math.round(z);
  const dx = Math.abs(rx - x), dy = Math.abs(ry - y), dz = Math.abs(rz - z);
  if (dx > dy && dx > dz) rx = -ry - rz; else if (dy > dz) ry = -rx - rz; else rz = -rx - ry;
  return { x: rx, y: ry, z: rz };
}
function axialRound(q, r) { const cr = cubeRound(q, -q - r, r); const ar = cubeToAxial(cr.x, cr.y, cr.z); return { q: ar.q, r: ar.r }; }

function hexPolygon(cx, cy, size) {
  const pts = [];
  for (let i = 0; i < 6; i++) {
    const angle = Math.PI / 180 * (60 * i - 30); // pointy-top
    pts.push([cx + size * Math.cos(angle), cy + size * Math.sin(angle)]);
  }
  return pts;
}

function offsetToAxial(col, row) {
  // pointy-top odd-r
  const q = col - ((row - (row & 1)) >> 1);
  const r = row;
  return { q, r };
}
function axialToOffset(q, r) {
  const col = q + ((r - (r & 1)) >> 1);
  const row = r;
  return { col, row };
}
function offsetNeighbors(c, r) {
  const odd = r & 1;
  const deltas = odd
    ? [[+1,0],[+1,-1],[0,-1],[-1,0],[0,+1],[+1,+1]]
    : [[+1,0],[0,-1],[-1,-1],[-1,0],[-1,+1],[0,+1]];
  return deltas.map(([dc, dr]) => ({ x: c + dc, y: r + dr }));
}

function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[c])); }

// start
window.addEventListener('DOMContentLoaded', initApp);
