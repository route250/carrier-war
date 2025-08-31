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
  canvas: document.getElementById('mapCanvas'),
  hint: document.getElementById('hint'),
  carrierStatus: document.getElementById('carrierStatus'),
  squadronList: document.getElementById('squadronList'),
  log: document.getElementById('log'),
  btnNextTurn: document.getElementById('btnNextTurn'),
  btnModeSelect: document.getElementById('btnModeSelect'),
  btnModeMove: document.getElementById('btnModeMove'),
  btnModeLaunch: document.getElementById('btnModeLaunch'),
  btnRestart: document.getElementById('btnRestart'),
  btnNewMap: document.getElementById('btnNewMap'),
};

const ctx = el.canvas.getContext('2d');

// === Init ===
function init() {
  bindUI();
  computeHexMetrics();
  // サーバセッションを作成（サーバ側でマップ生成）
  ensureSession()
    .then((sid)=>{ if (sid) { logMsg(`作戦開始: ターン${state.turn} (session: ${sid})`); renderAll(); } })
    .catch((e)=>{ logMsg(`セッション初期化エラー: ${e && e.message ? e.message : e}`); });
  // 作戦開始ログは ensureSession 完了時に出す
  setHint();
}

function bindUI() {
  el.canvas.addEventListener('mousemove', onMouseMove);
  el.canvas.addEventListener('mouseleave', () => (state.highlight = null, renderAll()));
  el.canvas.addEventListener('click', onMapClick);

  el.btnNextTurn.addEventListener('click', () => {
    nextTurn();
  });

  const modeButtons = [el.btnModeSelect, el.btnModeMove, el.btnModeLaunch];
  modeButtons.forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));

  el.btnRestart.addEventListener('click', () => restartGame('restart'));
  el.btnNewMap.addEventListener('click', () => restartGame('newmap'));
}

function setMode(mode) {
  state.mode = mode;
  state.highlight = null;
  document.querySelectorAll('[data-mode]').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));
  setHint();
  renderAll();
}

function setHint() {
  const m = state.mode;
  const text = m === 'move'
    ? '目的地をクリック（毎ターン自動移動）'
    : m === 'launch'
      ? `目標地点をクリック（航続${SQUADRON_RANGE}以内）`
      : 'ユニット状況を確認できます';
  el.hint.textContent = text;
}

// === Rendering ===
function renderAll() {
  renderMap();
  renderUnits();
  renderEnemyIntel();
  updatePanels();
}

function renderMap() {
  const w = el.canvas.width, h = el.canvas.height;
  ctx.clearRect(0, 0, w, h);

  // background water
  ctx.fillStyle = getCss('--water');
  ctx.fillRect(0, 0, w, h);

  // draw hex tiles (offset coords -> axial -> pixel)
  for (let r = 0; r < MAP_H; r++) {
    for (let c = 0; c < MAP_W; c++) {
      const [px, py] = offsetToPixel(c, r);
      const poly = hexPolygon(px, py, HEX_SIZE);
      ctx.beginPath();
      ctx.moveTo(poly[0][0], poly[0][1]);
      for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0], poly[i][1]);
      ctx.closePath();
      ctx.fillStyle = state.map[r][c] === 1 ? getCss('--island') : getCss('--water');
      ctx.fill();
      ctx.strokeStyle = getCss('--grid');
      ctx.lineWidth = 1;
      ctx.stroke();
      // 自軍視界（今ターン）：少し明るくオーバーレイ
      if (isTurnVisible(c, r)) {
        ctx.fillStyle = 'rgba(255,255,255,0.14)';
        ctx.fill();
      }
    }
  }

  // highlight hex
  if (state.highlight) {
    const { x: c, y: r } = state.highlight;
    const [px, py] = offsetToPixel(c, r);
    const poly = hexPolygon(px, py, HEX_SIZE);
    ctx.beginPath();
    ctx.moveTo(poly[0][0], poly[0][1]);
    for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0], poly[i][1]);
    ctx.closePath();
    let color = '#ffffff';
    if (state.mode === 'launch') {
      const d = hexDistance({ x: c, y: r }, state.carrier);
      if (d > SQUADRON_RANGE) color = '#ff5c5c';
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

function renderUnits() {
  // carrier
  drawRectTile(state.carrier.x, state.carrier.y, getCss('--carrier'));
  drawHpBarTile(state.carrier.x, state.carrier.y, state.carrier.hp, CARRIER_MAX_HP, '#6ad4ff');
  if (state.carrier.target) {
    drawLineTile(state.carrier.x, state.carrier.y, state.carrier.target.x, state.carrier.target.y, 'rgba(106,212,255,0.35)');
  }

  // launch mode: dashed range outline removed (hover color indicates validity)
  // pending launch preview: show planned strike line (from spawn or carrier to target)
  if (typeof PENDING_LAUNCH === 'object' && PENDING_LAUNCH && typeof PENDING_LAUNCH.x === 'number' && typeof PENDING_LAUNCH.y === 'number') {
    const t = { x: PENDING_LAUNCH.x, y: PENDING_LAUNCH.y };
    let from = findFreeAdjacent(state.carrier.x, state.carrier.y, { preferAwayFrom: t }) || { x: state.carrier.x, y: state.carrier.y };
    drawLineTile(from.x, from.y, t.x, t.y, 'rgba(242,193,78,0.5)');
  }

  // squadrons
  for (const sq of state.squadrons) {
    if (sq.state === 'base' || sq.state === 'lost') continue;
    drawCircleTile(sq.x, sq.y, getCss('--squad'));
    drawHpBarTile(sq.x, sq.y, sq.hp ?? SQUAD_MAX_HP, SQUAD_MAX_HP, '#f2c14e');
    if (sq.state === 'outbound') {
      drawLineTile(sq.x, sq.y, sq.target.x, sq.target.y, 'rgba(242,193,78,0.35)');
    }
  }
}

function renderEnemyIntel() {
  // 敵空母（現在可視なら現在地、不可視なら記憶を表示）
  const c = state.enemy.carrier;
  const ic = state.intel.carrier;
  if (isVisibleToPlayer(c.x, c.y)) {
    drawRectTile(c.x, c.y, getCss('--enemy'));
    drawHpBarTile(c.x, c.y, c.hp, CARRIER_MAX_HP, '#ff9a9a');
  } else if (ic.ttl > 0) {
    drawRectTileStyled(ic.x, ic.y, getCss('--enemy'), { memory: true });
  }

  // 敵編隊（ひし形）
  for (const es of state.enemy.squadrons) {
    const m = state.intel.squadrons.get(es.id);
    if (isVisibleToPlayer(es.x, es.y)) {
      drawDiamondTile(es.x, es.y, getCss('--enemy-squad'));
    } else if (m && m.ttl > 0) {
      drawDiamondTileStyled(m.x, m.y, getCss('--enemy-squad'), { memory: true });
    }
  }
}

function drawRectTile(x, y, color) {
  const [cx0, cy0] = offsetToPixel(x, y);
  const cx = cx0 - HEX_SIZE, cy = cy0 - HEX_SIZE;
  // halo
  ctx.strokeStyle = 'rgba(0,0,0,0.85)';
  ctx.lineWidth = 4;
  ctx.strokeRect(cx + 3, cy + 3, HEX_SIZE * 2 - 6, HEX_SIZE * 2 - 6);
  // fill
  ctx.fillStyle = color;
  ctx.fillRect(cx + 4, cy + 4, HEX_SIZE * 2 - 8, HEX_SIZE * 2 - 8);
  // border
  ctx.strokeStyle = 'rgba(255,255,255,0.35)';
  ctx.lineWidth = 1.5;
  ctx.strokeRect(cx + 4, cy + 4, HEX_SIZE * 2 - 8, HEX_SIZE * 2 - 8);
}
function drawCircleTile(x, y, color) {
  const [px, py] = offsetToPixel(x, y); const r = HEX_SIZE * 0.6;
  // halo
  ctx.beginPath(); ctx.arc(px, py, r + 2, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(0,0,0,0.85)'; ctx.lineWidth = 4; ctx.stroke();
  // fill
  ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.fill();
  // border
  ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1.5; ctx.stroke();
}
function drawDiamondTile(x, y, color) {
  const [cx, cy] = offsetToPixel(x, y); const r = HEX_SIZE * 0.6;
  const pts = [ [cx, cy - r], [cx + r, cy], [cx, cy + r], [cx - r, cy] ];
  // halo
  ctx.beginPath(); ctx.moveTo(...pts[0]); for (let i = 1; i < pts.length; i++) ctx.lineTo(...pts[i]); ctx.closePath();
  ctx.strokeStyle = 'rgba(0,0,0,0.85)'; ctx.lineWidth = 4; ctx.stroke();
  // fill
  ctx.fillStyle = color; ctx.fill();
  // border
  ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1.5; ctx.stroke();
}

function drawRectTileStyled(x, y, color, { memory } = {}) {
  const [c0x, c0y] = offsetToPixel(x, y);
  const cx = c0x - HEX_SIZE, cy = c0y - HEX_SIZE;
  ctx.save();
  if (memory) ctx.globalAlpha = 0.55;
  // halo
  ctx.strokeStyle = 'rgba(0,0,0,0.85)'; ctx.lineWidth = 4;
  ctx.strokeRect(cx + 3, cy + 3, HEX_SIZE * 2 - 6, HEX_SIZE * 2 - 6);
  // fill
  ctx.fillStyle = color; ctx.fillRect(cx + 4, cy + 4, HEX_SIZE * 2 - 8, HEX_SIZE * 2 - 8);
  // border dashed for memory
  ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1.5;
  if (memory) ctx.setLineDash([4, 3]);
  ctx.strokeRect(cx + 4, cy + 4, HEX_SIZE * 2 - 8, HEX_SIZE * 2 - 8);
  ctx.restore();
}

function drawDiamondTileStyled(x, y, color, { memory } = {}) {
  const [cx, cy] = offsetToPixel(x, y); const r = HEX_SIZE * 0.6;
  const pts = [ [cx, cy - r], [cx + r, cy], [cx, cy + r], [cx - r, cy] ];
  ctx.save();
  if (memory) ctx.globalAlpha = 0.55;
  // halo
  ctx.beginPath(); ctx.moveTo(...pts[0]); for (let i = 1; i < pts.length; i++) ctx.lineTo(...pts[i]); ctx.closePath();
  ctx.strokeStyle = 'rgba(0,0,0,0.85)'; ctx.lineWidth = 4; ctx.stroke();
  // fill
  ctx.fillStyle = color; ctx.fill();
  // border dashed for memory
  ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1.5; if (memory) ctx.setLineDash([4, 3]);
  ctx.stroke();
  ctx.restore();
}

function drawHpBarTile(x, y, hp, max, color) {
  const [px, py] = offsetToPixel(x, y);
  const w = HEX_SIZE * 1.6, h = 4;
  const cx = Math.round(px - w / 2), cy = Math.round(py - HEX_SIZE + 3);
  const ratio = Math.max(0, Math.min(1, hp / max));
  // bg
  ctx.fillStyle = 'rgba(0,0,0,0.6)';
  ctx.fillRect(cx, cy, w, h);
  // fg
  ctx.fillStyle = color;
  ctx.fillRect(cx, cy, Math.round(w * ratio), h);
}
function drawLineTile(x1, y1, x2, y2, color) {
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  const [sx, sy] = offsetToPixel(x1, y1);
  const [tx, ty] = offsetToPixel(x2, y2);
  ctx.moveTo(sx, sy);
  ctx.lineTo(tx, ty);
  ctx.stroke();
}

// 指定中心からhex距離=rangeのタイル中心を結んでアウトラインを描画
function drawRangeOutline(cx, cy, range, color) {
  const pts = [];
  const [pcx, pcy] = offsetToPixel(cx, cy);
  for (let y = 0; y < MAP_H; y++) {
    for (let x = 0; x < MAP_W; x++) {
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
function onMouseMove(e) {
  const t = tileFromEvent(e);
  if (!t) return;
  state.highlight = t;
  renderAll();
}

function onMapClick(e) {
  if (state.gameOver) return;
  const t = tileFromEvent(e);
  if (!t) return;

  if (state.mode === 'move') {
    setCarrierDestination(t.x, t.y);
  } else if (state.mode === 'launch') {
    tryLaunchStrike(t.x, t.y);
  }
}

function setCarrierDestination(x, y) {
  // 目的地は海に制限。島をクリックしたら最寄りの海に補正。
  let dest = { x, y };
  if (state.map[y][x] === 1) {
    dest = nearestSea(x, y);
    if (state.map[dest.y][dest.x] === 1) { logMsg('適切な海マスが見つかりません'); return; }
  }
  state.carrier.target = dest;
  if (state.carrier.x === dest.x && state.carrier.y === dest.y) {
    logMsg('空母は既に目的地に到達しています');
  } else {
    logMsg(`空母の目的地を(${dest.x}, ${dest.y})に設定`);
  }
  renderAll();
}

function tryLaunchStrike(tx, ty) {
    // セッション運用時はサーバへ発艦指示のみ送る（次のターンで処理）
    if (countBaseAvailable() <= 0) { logMsg('搭載機がありません'); return; }
    if (hexDistance({ x: tx, y: ty }, state.carrier) > SQUADRON_RANGE) { logMsg(`航続距離外です（最大${SQUADRON_RANGE}）`); return; }
    PENDING_LAUNCH = { x: tx, y: ty };
    logMsg(`発艦指示を登録（${tx}, ${ty}） 次のターンで出撃`);
    renderAll();
    return;
}

// === Turn ===
async function nextTurn() {
  if (state.gameOver) return;
  // avoid double-click during async call
  try { el.btnNextTurn.disabled = true; } catch {}

  // このターンで移動した経路を収集（自軍のみ）
  const pathSweep = [];// array of {x,y,range}

  // 2) 敵AI：サーバでターン解決（セッション）またはAIプランのみ取得
  try {
    const req = buildSessionStepRequest();
    const plan = await callSessionStep(req);
    logMsg(`ターン${state.turn}`);
    enemyTurnFromPlan(plan);
  } catch (e) {
    logMsg(`AIプラン取得エラー: ${e && e.message ? e.message : e}`);
    // サーバーが応答しない場合はここでターン処理を中止する（フォールバックを行わない）
    logMsg('サーバー未応答のためターン処理を中止します');
    return;
  }

  renderAll();

  try { el.btnNextTurn.disabled = false; } catch {}
}

// === Utils ===
function tileFromEvent(e) {
  // Scale client coords to canvas internal coords to handle CSS/devicePixelRatio
  const rect = el.canvas.getBoundingClientRect();
  const scaleX = el.canvas.width / rect.width;
  const scaleY = el.canvas.height / rect.height;
  const mx = (e.clientX - rect.left) * scaleX;
  const my = (e.clientY - rect.top) * scaleY;
  const [qf, rf] = pixelToAxial(mx, my);
  const { q, r } = axialRound(qf, rf);
  const off = axialToOffset(q, r);
  const c = off.col, rr = off.row;
  if (c < 0 || rr < 0 || c >= MAP_W || rr >= MAP_H) return null;
  return { x: c, y: rr };
}

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

// 空母周辺の空きマスを探索（8近傍）。preferAwayFromがあれば遠ざかる方向を優先。
function findFreeAdjacent(cx, cy, { preferAwayFrom } = {}) {
  const candidates = offsetNeighbors(cx, cy).filter(p => p.x >= 0 && p.y >= 0 && p.x < MAP_W && p.y < MAP_H && state.map[p.y][p.x] === 0 && !isOccupied(p.x, p.y));
  if (candidates.length === 0) return null;
  if (preferAwayFrom) {
    candidates.sort((a, b) => hexDistance(preferAwayFrom, b) - hexDistance(preferAwayFrom, a));
  }
  return candidates[0];
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
// === Session (stateful) ===
let SESSION_ID = null;

async function ensureSession() {
  if (SESSION_ID) return SESSION_ID;
  // Do not synthesize initial squadrons on the client; server returns authoritative initial state.
  // Send minimal player/enemy carrier info. Server will populate squadrons and map.
  const res = await fetch('/v1/session/', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    // mapは送らず、サーバ側で生成
    body: JSON.stringify({ })
  });
  if (!res.ok) throw new Error(`session create failed: ${res.status}`);
  const data = await res.json();
  if( typeof data?.session_id !== 'string') {
    throw new Error(`invalid session_id: ${JSON.stringify(data?.session_id)}`);
  }
  if( typeof data?.turn !== 'number' || data.turn !== 1 ) {
    throw new Error(`invalid turn: ${JSON.stringify(data?.turn)}`);
  }
  if( !Array.isArray(data?.map) ) {
    throw new Error(`invalid map: ${JSON.stringify(data?.map)}`);
  }
  SESSION_ID = data.session_id;
  state.session_id = data.session_id;
  state.turn = data.turn;
  state.map = data.map;
  state.carrier = { ...state.carrier, ...data.player_state.carrier };
  state.squadrons = data.player_state.squadrons;
  state.enemy.carrier = { ...state.enemy.carrier, ...data.enemy_state.carrier };

  return SESSION_ID;
}

function buildSessionStepRequest() {
  const player_orders = buildPlayerOrders();
  return {
    player_orders,
    config: { difficulty: 'normal', time_ms: 50 },
  };
}

let PENDING_LAUNCH = null;
function buildPlayerOrders() {
  const orders = {};
  if (state.carrier.target) orders.carrier_target = { x: state.carrier.target.x, y: state.carrier.target.y };
  if (PENDING_LAUNCH) orders.launch_target = { x: PENDING_LAUNCH.x, y: PENDING_LAUNCH.y };
  PENDING_LAUNCH = null; // consume
  return orders;
}

async function callSessionStep(body) {
  await ensureSession();
  const res = await fetch(`/v1/session/${SESSION_ID}/step`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    const txt = await res.text().catch(()=> '');
    throw new Error(`status ${res.status} ${txt}`);
  }
  const json = await res.json();
  if( typeof json?.session_id !== 'string' || state.session_id !== json.session_id) {
    throw new Error(`invalid session_id: ${JSON.stringify(json?.session_id)}`);
  }
  if( typeof json?.turn !== 'number' || json.turn <= state.turn ) {
    throw new Error(`invalid turn: ${JSON.stringify(json?.turn)}`);
  }
  state.turn = json.turn;
  return json;
}

function enemyTurnFromPlan(plan) {
    if (!plan || !plan.enemy_state) {
        logMsg('サーバが権威ある状態を返しませんでした。クライアントはローカル解決を行いません。');
    }

    const ec = state.enemy.carrier;
    // apply enemy carrier
    state.enemy.carrier = { ...state.enemy.carrier, ...plan.enemy_state.carrier };
    // apply enemy squadrons
    state.enemy.squadrons = (plan.enemy_state.squadrons || []).map((s) => ({ id: s.id, state: s.state, hp: s.hp ?? SQUAD_MAX_HP, x: s.x ?? undefined, y: s.y ?? undefined, target: s.target ? { x: s.target.x, y: s.target.y } : undefined, speed: s.speed ?? 10, vision: s.vision ?? VISION_SQUADRON }));
    // apply player carrier and squadrons (authoritative)
    if (plan.player_state) {
        state.carrier = { ...state.carrier, ...plan.player_state.carrier };
        state.squadrons = (plan.player_state.squadrons || []).map((s) => ({ id: s.id, state: s.state, hp: s.hp ?? SQUAD_MAX_HP, x: s.x ?? undefined, y: s.y ?? undefined, target: s.target ? { x: s.target.x, y: s.target.y } : undefined, speed: s.speed ?? 10, vision: s.vision ?? VISION_SQUADRON }));
    }
    // memory
    if (plan.enemy_memory_out && plan.enemy_memory_out.carrier_last_seen) {
        state.enemyIntel.carrier = { ...plan.enemy_memory_out.carrier_last_seen };
    }
    if (plan.enemy_memory_out && plan.enemy_memory_out.enemy_ai) {
        const ai = plan.enemy_memory_out.enemy_ai;
        state.enemyAI.patrolIx = ai.patrol_ix | 0;
        state.enemyAI.lastPatrolTurn = ai.last_patrol_turn | 0;
    }
    // player intel from server
    if (plan.player_intel) {
        if (plan.player_intel.carrier) {
        state.intel.carrier = { ...plan.player_intel.carrier };
        }
        if (Array.isArray(plan.player_intel.squadrons)) {
        const mp = new Map();
        for (const item of plan.player_intel.squadrons) {
            if (item && item.id && item.marker) {
            mp.set(item.id, { ...item.marker });
            }
        }
        state.intel.squadrons = mp;
        }
    }
  // server-computed visibility
  if (!Array.isArray(plan.turn_visible)) {
    throw new Error('server did not return plan.turn_visible - authoritative turn visibility is required');
  }
  // Validate server visibility against local computation for all cells to detect inconsistencies
  const srvSet = new Set(plan.turn_visible);
  for (let y = 0; y < MAP_H; y++) {
    for (let x = 0; x < MAP_W; x++) {
      const key = visibilityKey(x, y);
      const srvHas = srvSet.has(key);
      const localHas = !!localIsVisibleToPlayer(x, y);
      if (srvHas !== localHas) {
        //throw new Error(`visibility mismatch at ${key}: server=${srvHas} client=${localHas}`);
        //logMsg(`visibility mismatch at ${key}: server=${srvHas} client=${localHas}`);
      }
    }
  }
  state.turnVisible = new Set(plan.turn_visible);
    // game status
    if (plan.game_status && plan.game_status.over && !state.gameOver) {
        const res = plan.game_status.result || 'draw';
        const msg = plan.game_status.message || (res === 'win' ? '勝利' : res === 'lose' ? '敗北' : '引き分け');
        finishGame(res, msg);
    }
    // logs
    if (Array.isArray(plan.logs)) for (const m of plan.logs) logMsg(m);
    return;

}

function nearestSea(x, y) {
  x = clamp(x, 0, MAP_W - 1); y = clamp(y, 0, MAP_H - 1);
  if (state.map[y][x] === 0) return { x, y };
  // 同心円状に最寄り海タイルを探す（半径最大6）
  for (let r = 1; r <= 6; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        const nx = clamp(x + dx, 0, MAP_W - 1);
        const ny = clamp(y + dy, 0, MAP_H - 1);
        if (state.map[ny][nx] === 0) return { x: nx, y: ny };
      }
    }
  }
  // どうしても見つからなければ元座標
  return { x, y };
}

// === Turn Visibility (player) ===
function visibilityKey(x, y) { return `${x},${y}`; }
function isTurnVisible(x, y) { return !!state.turnVisible && state.turnVisible.has(visibilityKey(x, y)); }
function clearTurnVisibility() { state.turnVisible = new Set(); }

function finishGame(result, message) {
  state.gameOver = true;
  logMsg(message);
  disableControls();
  setTimeout(() => alert(message), 10);
}

function disableControls() {
  document.querySelectorAll('button').forEach((b) => b.disabled = true);
}

function enableControls() {
  document.querySelectorAll('button').forEach((b) => b.disabled = false);
}

function clearLog() {
  state.log = [];
  el.log.innerHTML = '';
}

function restartGame(kind) {
  // reset state fields (keep same objects to avoid re-binding)
  state.turn = 1;
  state.mode = 'select';
  state.carrier = { id: 'C1', x: 3, y: 3, hp: 100, speed: 2, vision: VISION_CARRIER, hangar: 2, target: null };
  state.enemy = { carrier: { id: 'E1', x: 26, y: 26, hp: 100, speed: 2, vision: VISION_CARRIER, hangar: 2 }, squadrons: [] };
  state.intel = { carrier: { seen: false, x: null, y: null, ttl: 0 }, squadrons: new Map() };
  state.enemyIntel = { carrier: { seen: false, x: null, y: null, ttl: 0 } };
  state.enemyAI = { patrolIx: 0, lastPatrolTurn: 0 };
  state.squadrons = Array.from({ length: state.carrier.hangar }, (_, i) => ({ id: `SQ${i + 1}`, hp: SQUAD_MAX_HP, state: 'base' }));
  state.enemy.squadrons = Array.from({ length: state.enemy.carrier.hangar }, (_, i) => ({ id: `ESQ${i + 1}`, hp: SQUAD_MAX_HP, state: 'base' }));
  state.highlight = null;
  state.gameOver = false;

  // サーバで新しいマップ・状態を生成
  
  // UI
  enableControls();
  document.querySelectorAll('[data-mode]').forEach((b) => b.classList.remove('active'));
  document.getElementById('btnModeSelect').classList.add('active');
  setHint();
  clearLog();
  logMsg(kind === 'newmap' ? '新しい海域で作戦開始: ターン1' : 'リスタート: ターン1');
  // Reset session and create a new one for the new map/state
  SESSION_ID = null;
  ensureSession().then(()=>{ renderAll(); }).catch(()=>{});
}

function logMsg(msg) {
  const ts = new Date().toLocaleTimeString('ja-JP', { hour12: false });
  state.log.push({ ts, msg });
  if (state.log.length > 200) state.log.shift();
  const line = document.createElement('div');
  line.className = 'entry';
  line.innerHTML = `<span class="ts">[${ts}]</span>${escapeHtml(msg)}`;
  el.log.appendChild(line);
  el.log.scrollTop = el.log.scrollHeight;
}

function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;','\'':'&#39;'}[c])); }

// start
window.addEventListener('DOMContentLoaded', init);
