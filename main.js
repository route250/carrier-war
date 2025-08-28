// === Config ===
const MAP_W = 30; // axial q range: 0..MAP_W-1
const MAP_H = 30; // axial r range: 0..MAP_H-1
// Hex layout (pointy-top axial)
const SQRT3 = Math.sqrt(3);
let HEX_SIZE = 10; // pixel radius of hex (computed at init to fit canvas)
let ORIGIN_X = 0, ORIGIN_Y = 0; // render offset to center the map

// === State ===
const SQUAD_MAX_HP = 40;
const CARRIER_MAX_HP = 100;
const state = {
  map: [], // 0=sea, 1=island
  turn: 1,
  mode: 'select', // select | move | launch
  carrier: { id: 'C1', x: 3, y: 3, hp: 100, speed: 2, vision: 10, hangar: 2 },
  enemy: { carrier: { id: 'E1', x: 26, y: 26, hp: 100, speed: 2, vision: 10, hangar: 2 }, squadrons: [] },
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
  generateMap();
  placeEnemyCarrier();
  bindUI();
  computeHexMetrics();
  // 初期編隊（基地待機、HPはゲーム中回復しない）
  state.squadrons = Array.from({ length: state.carrier.hangar }, (_, i) => ({ id: `SQ${i + 1}`, hp: SQUAD_MAX_HP, state: 'base' }));
  state.enemy.squadrons = Array.from({ length: state.enemy.carrier.hangar }, (_, i) => ({ id: `ESQ${i + 1}`, hp: SQUAD_MAX_HP, state: 'base' }));
  renderAll();
  logMsg('作戦開始: ターン1');
  setHint();
}

function generateMap() {
  // 海ベース
  state.map = new Array(MAP_H).fill(0).map(() => new Array(MAP_W).fill(0));
  // 簡易ランダム諸島
  const islandBlobs = 10;
  for (let i = 0; i < islandBlobs; i++) {
    const cx = rand(2, MAP_W - 3);
    const cy = rand(2, MAP_H - 3);
    const r = rand(1, 3);
    for (let y = -r; y <= r; y++) {
      for (let x = -r; x <= r; x++) {
        if (x * x + y * y <= r * r) {
          const tx = clamp(cx + x, 0, MAP_W - 1);
          const ty = clamp(cy + y, 0, MAP_H - 1);
          state.map[ty][tx] = 1;
        }
      }
    }
  }
  // 開始地点とその周囲を海にして初手で身動きできるよう確保
  carveSea(state.carrier.x, state.carrier.y, 2);
  ensureSeaExit(state.carrier.x, state.carrier.y);
}

function placeEnemyCarrier() {
  // 右下付近の海タイルを探す
  let ex = MAP_W - 4, ey = MAP_H - 4;
  for (let yy = MAP_H - 5; yy >= MAP_H - 10; yy--) {
    for (let xx = MAP_W - 5; xx >= MAP_W - 10; xx--) {
      if (xx >= 0 && yy >= 0 && state.map[yy] && state.map[yy][xx] === 0) { ex = xx; ey = yy; break; }
    }
    if (state.map[ey] && state.map[ey][ex] === 0) break;
  }
  state.enemy.carrier.x = ex; state.enemy.carrier.y = ey;
  // 敵も初期周辺は海に（初手で詰まないように）
  carveSea(ex, ey, 2);
  ensureSeaExit(ex, ey);
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
    ? 'マップをクリックして2マス以内へ移動'
    : m === 'launch'
      ? '目標地点をクリックして打撃隊を出撃'
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
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 2;
    ctx.stroke();
  }
}

function renderUnits() {
  // carrier
  drawRectTile(state.carrier.x, state.carrier.y, getCss('--carrier'));
  drawHpBarTile(state.carrier.x, state.carrier.y, state.carrier.hp, CARRIER_MAX_HP, '#6ad4ff');

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

// === Panels ===
function updatePanels() {
  const c = state.carrier;
  el.carrierStatus.innerHTML = `
    <div class="kv">
      <div>位置</div><div class="mono">(${c.x}, ${c.y})</div>
      <div>HP</div><div>${c.hp}</div>
      <div>視界</div><div>${c.vision}</div>
      <div>搭載枠</div><div>${c.hangar - countActiveSquadrons()} / ${c.hangar}</div>
    </div>
    <div class="kv"><div>ターン</div><div>${state.turn}</div></div>
  `;

  if (state.squadrons.filter((s)=>s.state!=='lost').length === 0) {
    el.squadronList.textContent = '出撃中の編隊はありません';
  } else {
    el.squadronList.innerHTML = state.squadrons.map((s) => {
      return `<div class="kv">
        <div>ID</div><div class="mono">${s.id}</div>
        <div>状態</div><div>${labelSqState(s.state)}</div>
        <div>位置</div><div class="mono">${s.state==='base'?'(CARRIER)':`(${s.x}, ${s.y})`}</div>
        <div>目標</div><div class="mono">${s.target?`(${s.target.x}, ${s.target.y})`:'-'}</div>
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
    tryMoveCarrier(t.x, t.y);
  } else if (state.mode === 'launch') {
    tryLaunchStrike(t.x, t.y);
  }
}

function tryMoveCarrier(x, y) {
  if (state.map[y][x] === 1) { logMsg('島には移動できません'); return; }
  const dist = hexDistance(state.carrier, { x, y });
  if (dist > state.carrier.speed) { logMsg(`遠すぎます（距離${dist}）`); return; }
  if (isOccupied(x, y, { ignore: { type: 'carrier', side: 'player' } })) { logMsg('そのマスは使用中です'); return; }
  state.carrier.x = x;
  state.carrier.y = y;
  logMsg(`空母を(${x}, ${y})へ移動`);
  renderAll();
}

function tryLaunchStrike(tx, ty) {
  if (countBaseAvailable() <= 0) { logMsg('搭載機がありません'); return; }
  const sq = state.squadrons.find((s)=>s.state==='base' && s.hp>0);
  if (!sq) { logMsg('搭載機がありません'); return; }
  // 発艦位置は空母の周囲の空きマス
  const spawn = findFreeAdjacent(state.carrier.x, state.carrier.y, { preferAwayFrom: { x: tx, y: ty } });
  if (!spawn) { logMsg('発艦スペースがありません'); return; }
  sq.x = spawn.x; sq.y = spawn.y; sq.target = { x: tx, y: ty }; sq.state = 'outbound'; sq.speed = 10; sq.vision = 10;
  logMsg(`打撃隊${sq.id}を(${tx}, ${ty})へ出撃（発艦: ${spawn.x},${spawn.y}）`);
  renderAll();
}

// === Turn ===
function nextTurn() {
  if (state.gameOver) return;
  state.turn += 1;

  // 1) 自軍編隊の行動（発見→接近→同ターン攻撃可→帰還）
  for (const sq of [...state.squadrons]) {
    const ec = state.enemy.carrier;
    if (sq.state === 'outbound') {
      // 索敵に入ったら接近。1マス以内に到達できたらこのターンで攻撃。
      if (hexDistance(sq, ec) <= (sq.vision || 10)) {
        const before = hexDistance(sq, ec);
        stepOnGridTowards(sq, ec, sq.speed, { stopRange: 1, avoid: true, ignoreId: sq.id, passIslands: true });
        const after = hexDistance(sq, ec);
        if (after <= 1) {
          const dmg = scaledDamage(sq, 25);
          ec.hp = Math.max(0, ec.hp - dmg);
          logMsg(`${sq.id} が敵空母に攻撃（${dmg}） 残HP:${ec.hp}`);
          // 対空砲火（AA）
          const aa = scaledAA(state.enemy.carrier, 20);
          sq.hp = Math.max(0, (sq.hp ?? SQUAD_MAX_HP) - aa);
          logMsg(`${sq.id} が対空砲火を受けた（${aa}） 残HP:${sq.hp}`);
          if (sq.hp <= 0) {
            logMsg(`${sq.id} は撃墜された`);
            sq.state = 'lost'; delete sq.x; delete sq.y; delete sq.target;
          } else {
            sq.state = 'returning';
          }
        } else {
          if (before > (sq.vision || 10)) {
            logMsg(`${sq.id} 敵空母を発見、接近中`);
          }
          sq.state = 'engaging';
        }
      } else {
        stepOnGridTowards(sq, sq.target, sq.speed, { avoid: true, ignoreId: sq.id, passIslands: true });
        if (sq.x === sq.target.x && sq.y === sq.target.y) {
          logMsg(`${sq.id} 目標到達、敵見当たらず 帰還`);
          sq.state = 'returning';
        }
      }
    } else if (sq.state === 'engaging') {
      stepOnGridTowards(sq, ec, sq.speed, { stopRange: 1, avoid: true, ignoreId: sq.id, passIslands: true });
      if (hexDistance(sq, ec) <= 1) {
        const dmg = scaledDamage(sq, 25);
        ec.hp = Math.max(0, ec.hp - dmg);
        logMsg(`${sq.id} が敵空母に攻撃（${dmg}） 残HP:${ec.hp}`);
        const aa = scaledAA(state.enemy.carrier, 20);
        sq.hp = Math.max(0, (sq.hp ?? SQUAD_MAX_HP) - aa);
        logMsg(`${sq.id} が対空砲火を受けた（${aa}） 残HP:${sq.hp}`);
        if (sq.hp <= 0) {
          logMsg(`${sq.id} は撃墜された`);
          sq.state = 'lost'; delete sq.x; delete sq.y; delete sq.target;
        } else {
          sq.state = 'returning';
        }
      }
    } else if (sq.state === 'returning') {
      // 空母と同一マスは禁止。1マス以内に入ったら帰還完了とみなす。
      stepOnGridTowards(sq, state.carrier, sq.speed, { stopRange: 1, avoid: true, ignoreId: sq.id, passIslands: true });
      if (hexDistance(sq, state.carrier) <= 1) {
        logMsg(`${sq.id} 帰還完了（基地待機へ）`);
        sq.state = 'base'; delete sq.x; delete sq.y; delete sq.target;
      }
    }
  }

  // 2) 敵AI：空母移動/出撃/編隊行動
  enemyTurn();

  // 3) 可視情報更新（player側表示用）
  updatePlayerIntel();
  renderAll();

  // 4) 勝敗判定
  checkGameEnd();
  logMsg(`ターン${state.turn}`);
}

// === Utils ===
function tileFromEvent(e) {
  const rect = el.canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
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

// グリッド上をチェビシェフ距離短縮のため1歩ずつ進める。占有マスは回避（簡易）。
function stepOnGridTowards(obj, target, stepMax, { stopRange = 0, avoid = false, ignoreId = null, passIslands = false } = {}) {
  // Prefer straight-line stepping along hex line; fallback to greedy neighbor if blocked
  for (let step = 0; step < stepMax; step++) {
    const dist = hexDistance(obj, target);
    if (dist <= stopRange) break;
    const nxt = nextStepOnHexLine(obj, target);
    let moved = false;
    const tryCells = [];
    if (nxt) tryCells.push(nxt);
    // fallback candidates ordered by closeness to line and target
    const nbrs = offsetNeighbors(obj.x, obj.y);
    const lineDir = nxt || { x: obj.x, y: obj.y };
    nbrs.sort((A, B) => {
      const dA = hexDistance(A, target) - (A.x === lineDir.x && A.y === lineDir.y ? 0.1 : 0);
      const dB = hexDistance(B, target) - (B.x === lineDir.x && B.y === lineDir.y ? 0.1 : 0);
      return dA - dB;
    });
    tryCells.push(...nbrs);
    for (const p of tryCells) {
      const nx = p.x, ny = p.y;
      if (nx < 0 || ny < 0 || nx >= MAP_W || ny >= MAP_H) continue;
      if (!passIslands && state.map[ny][nx] === 1) continue;
      if (avoid && isOccupied(nx, ny, { ignore: { type: 'squad', id: ignoreId } })) continue;
      if (hexDistance({ x: nx, y: ny }, target) > dist) continue;
      obj.x = nx; obj.y = ny; moved = true; break;
    }
    // 最後の手段: どれも距離が縮まらない場合、直線候補が安全なら距離維持で1歩進む
    if (!moved && nxt) {
      const nx = nxt.x, ny = nxt.y;
      if (nx >= 0 && ny >= 0 && nx < MAP_W && ny < MAP_H && (passIslands || state.map[ny][nx] === 0) && !(avoid && isOccupied(nx, ny, { ignore: { type: 'squad', id: ignoreId } }))) {
        obj.x = nx; obj.y = ny; moved = true;
      }
    }
    if (!moved) break;
  }
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

// 指定中心（offset座標）からhex距離r以内を海にする（島の浸食）
function carveSea(cx, cy, r) {
  for (let y = 0; y < MAP_H; y++) {
    for (let x = 0; x < MAP_W; x++) {
      if (hexDistance({ x, y }, { x: cx, y: cy }) <= r) state.map[y][x] = 0;
    }
  }
}

// 指定地点の海から外洋（マップ端の海）へ到達可能か判定
function hasSeaPathToEdge(sq, sr) {
  if (state.map[sr][sq] !== 0) return false;
  const W = MAP_W, H = MAP_H;
  const q = [{ x: sq, y: sr }];
  const vis = Array.from({ length: H }, () => Array(W).fill(false));
  vis[sr][sq] = true;
  while (q.length) {
    const p = q.shift();
    if (p.x === 0 || p.y === 0 || p.x === W - 1 || p.y === H - 1) return true;
    for (const nb of offsetNeighbors(p.x, p.y)) {
      const nx = nb.x, ny = nb.y;
      if (nx < 0 || ny < 0 || nx >= W || ny >= H) continue;
      if (vis[ny][nx]) continue;
      if (state.map[ny][nx] !== 0) continue;
      vis[ny][nx] = true;
      q.push({ x: nx, y: ny });
    }
  }
  return false;
}

function carveChannelToEdge(sq, sr) {
  // carve along straight line toward nearest edge using greedy neighbor selection
  const dists = [
    { edge: { x: 0, y: sr }, d: sq },
    { edge: { x: MAP_W - 1, y: sr }, d: MAP_W - 1 - sq },
    { edge: { x: sq, y: 0 }, d: sr },
    { edge: { x: sq, y: MAP_H - 1 }, d: MAP_H - 1 - sr },
  ];
  const best = dists.reduce((a, b) => (a.d < b.d ? a : b));
  let cur = { x: sq, y: sr };
  carveSea(cur.x, cur.y, 1);
  while (!(cur.x === best.edge.x || cur.y === best.edge.y)) {
    const nbrs = hexNeighbors(cur.x, cur.y).sort((A, B) => {
      const da = Math.min(Math.abs(A.x - best.edge.x), Math.abs(A.y - best.edge.y));
      const db = Math.min(Math.abs(B.x - best.edge.x), Math.abs(B.y - best.edge.y));
      return da - db;
    });
    const nxt = nbrs.find(n => n.x >= 0 && n.y >= 0 && n.x < MAP_W && n.y < MAP_H);
    if (!nxt) break;
    cur = nxt; carveSea(cur.x, cur.y, 1);
    if (cur.x === 0 || cur.y === 0 || cur.x === MAP_W - 1 || cur.y === MAP_H - 1) break;
  }
}

function ensureSeaExit(x, y) {
  if (!hasSeaPathToEdge(x, y)) carveChannelToEdge(x, y);
}

function getCss(varName) { return getComputedStyle(document.documentElement).getPropertyValue(varName).trim(); }
function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }
function rand(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
function damageRoll(base) { const variance = Math.round(base * 0.2); return base + rand(-variance, variance); }
function scaledDamage(attacker, base) {
  const hp = attacker.hp ?? SQUAD_MAX_HP;
  const scale = Math.max(0, Math.min(1, hp / SQUAD_MAX_HP));
  const raw = damageRoll(base);
  return Math.max(0, Math.round(raw * scale));
}
function scaledAA(carrier, base) {
  const scale = Math.max(0, Math.min(1, (carrier.hp ?? CARRIER_MAX_HP) / CARRIER_MAX_HP));
  const raw = damageRoll(base);
  return Math.max(0, Math.round(raw * scale));
}

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
  if (hexDistance({ x, y }, state.carrier) <= state.carrier.vision) return true;
  for (const sq of state.squadrons) if (sq.state!=='base' && sq.state!=='lost' && hexDistance({ x, y }, sq) <= (sq.vision || 10)) return true;
  return false;
}

function isVisibleToEnemy(x, y) {
  const ec = state.enemy.carrier;
  if (hexDistance({ x, y }, ec) <= ec.vision) return true;
  for (const sq of state.enemy.squadrons) if (sq.state!=='base' && sq.state!=='lost' && hexDistance({ x, y }, sq) <= (sq.vision || 10)) return true;
  return false;
}

// === Hex helpers ===
function computeHexMetrics() {
  const W = el.canvas.width, H = el.canvas.height;
  // compute HEX_SIZE to fit map into canvas
  // pointy-top odd-r rectangle: width ~= sqrt(3)*size*(MAP_W + 0.5), height ~= 1.5*size*(MAP_H - 1) + 2*size
  const sizeByW = W / (SQRT3 * (MAP_W + 0.5));
  const sizeByH = H / (1.5 * (MAP_H - 1) + 2);
  HEX_SIZE = Math.max(8, Math.floor(Math.min(sizeByW, sizeByH)));
  // center map
  const mapPixelW = SQRT3 * HEX_SIZE * (MAP_W + 0.5);
  const mapPixelH = 1.5 * HEX_SIZE * (MAP_H - 1) + 2 * HEX_SIZE;
  ORIGIN_X = Math.round((W - mapPixelW) / 2 + HEX_SIZE);
  ORIGIN_Y = Math.round((H - mapPixelH) / 2 + HEX_SIZE);
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

function enemyTurn() {
  const ec = state.enemy.carrier;

  // 敵の知覚更新（プレイヤー空母）
  if (isVisibleToEnemy(state.carrier.x, state.carrier.y)) {
    state.enemyIntel.carrier = { seen: true, x: state.carrier.x, y: state.carrier.y, ttl: 3 };
  } else if (state.enemyIntel.carrier.ttl > 0) {
    state.enemyIntel.carrier.ttl -= 1;
  }

  // 敵空母の移動（ランダム、島と占有マスは不可）
  const step = rand(0, ec.speed);
  for (let s = 0; s < step; s++) {
    const nbs = offsetNeighbors(ec.x, ec.y).filter(p => p.x>=0&&p.y>=0&&p.x<MAP_W&&p.y<MAP_H&&state.map[p.y][p.x]===0&&!isOccupied(p.x,p.y,{ignore:{type:'carrier',side:'enemy'}}));
    if (nbs.length === 0) break;
    const choice = nbs[rand(0, nbs.length - 1)];
    ec.x = choice.x; ec.y = choice.y;
  }

  // 敵の出撃
  if (countActiveEnemySquadrons() < ec.hangar && state.enemy.squadrons.some((s)=>s.state==='base' && s.hp>0)) {
    if (state.enemyIntel.carrier.ttl > 0) {
      // 既知位置へ打撃出撃
      const t = { x: state.enemyIntel.carrier.x, y: state.enemyIntel.carrier.y };
      const spawn = findFreeAdjacent(ec.x, ec.y, { preferAwayFrom: t });
      if (spawn) {
        const esq = state.enemy.squadrons.find((s)=>s.state==='base' && s.hp>0);
        if (esq) { esq.x = spawn.x; esq.y = spawn.y; esq.target = t; esq.state = 'outbound'; esq.speed = 10; esq.vision = 10; }
        state.enemyIntel.carrier.ttl -= 1;
        logMsg(`敵編隊が出撃した気配`);
      }
    } else {
      // 情報なし→定期的に索敵パトロール
      const turnsSince = state.turn - state.enemyAI.lastPatrolTurn;
      if (turnsSince >= 3) {
        const wp = getEnemyPatrolWaypoint();
        const t = nearestSea(wp.x, wp.y);
        const spawn = findFreeAdjacent(ec.x, ec.y, { preferAwayFrom: t });
        if (spawn) {
          const esq = state.enemy.squadrons.find((s)=>s.state==='base' && s.hp>0);
          if (esq) { esq.x = spawn.x; esq.y = spawn.y; esq.target = t; esq.state = 'outbound'; esq.speed = 10; esq.vision = 10; }
          state.enemyAI.lastPatrolTurn = state.turn;
          logMsg(`敵編隊が索敵に出撃した気配`);
        }
      }
    }
  }

  // 敵編隊の行動（発見→接近→攻撃→帰還）
  for (const sq of [...state.enemy.squadrons]) {
    if (sq.state === 'outbound') {
      if (hexDistance(sq, state.carrier) <= (sq.vision || 10)) {
        stepOnGridTowards(sq, state.carrier, sq.speed, { stopRange: 1, avoid: true, ignoreId: sq.id, passIslands: true });
        if (hexDistance(sq, state.carrier) <= 1) {
          const dmg = scaledDamage(sq, 25);
          state.carrier.hp = Math.max(0, state.carrier.hp - dmg);
          logMsg(`敵編隊が我が空母を攻撃（${dmg}） 残HP:${state.carrier.hp}`);
          const aa = scaledAA(state.carrier, 20);
          sq.hp = Math.max(0, (sq.hp ?? SQUAD_MAX_HP) - aa);
          logMsg(`敵編隊${sq.id} が対空砲火を受けた（${aa}） 残HP:${sq.hp}`);
          if (sq.hp <= 0) {
            logMsg(`敵編隊${sq.id} は撃墜された`);
            sq.state = 'lost'; delete sq.x; delete sq.y; delete sq.target;
          } else {
            sq.state = 'returning';
          }
        } else {
          sq.state = 'engaging';
        }
      } else {
        stepOnGridTowards(sq, sq.target, sq.speed, { avoid: true, ignoreId: sq.id, passIslands: true });
        if (sq.x === sq.target.x && sq.y === sq.target.y) {
          sq.state = 'returning';
        }
      }
    } else if (sq.state === 'engaging') {
      stepOnGridTowards(sq, state.carrier, sq.speed, { stopRange: 1, avoid: true, ignoreId: sq.id, passIslands: true });
      if (hexDistance(sq, state.carrier) <= 1) {
        const dmg = scaledDamage(sq, 25);
        state.carrier.hp = Math.max(0, state.carrier.hp - dmg);
        logMsg(`敵編隊が我が空母を攻撃（${dmg}） 残HP:${state.carrier.hp}`);
        const aa = scaledAA(state.carrier, 20);
        sq.hp = Math.max(0, (sq.hp ?? SQUAD_MAX_HP) - aa);
        logMsg(`敵編隊${sq.id} が対空砲火を受けた（${aa}） 残HP:${sq.hp}`);
        if (sq.hp <= 0) {
          logMsg(`敵編隊${sq.id} は撃墜された`);
          sq.state = 'lost'; delete sq.x; delete sq.y; delete sq.target;
        } else {
          sq.state = 'returning';
        }
      }
    } else if (sq.state === 'returning') {
      stepOnGridTowards(sq, ec, sq.speed, { stopRange: 1, avoid: true, ignoreId: sq.id, passIslands: true });
      if (hexDistance(sq, ec) <= 1) { sq.state = 'base'; delete sq.x; delete sq.y; delete sq.target; }
    }
  }
}

// パトロール用の巡回ポイント（四隅＋中心）
const PATROL_POINTS = [
  { x: 4, y: 4 },
  { x: MAP_W - 5, y: 4 },
  { x: 4, y: MAP_H - 5 },
  { x: MAP_W - 5, y: MAP_H - 5 },
  { x: Math.floor(MAP_W / 2), y: Math.floor(MAP_H / 2) },
];

function getEnemyPatrolWaypoint() {
  const i = state.enemyAI.patrolIx % PATROL_POINTS.length;
  state.enemyAI.patrolIx = (state.enemyAI.patrolIx + 1) % PATROL_POINTS.length;
  return PATROL_POINTS[i];
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

function updatePlayerIntel() {
  // 敵空母
  const c = state.enemy.carrier;
  const ic = state.intel.carrier;
  if (isVisibleToPlayer(c.x, c.y)) {
    ic.seen = true; ic.x = c.x; ic.y = c.y; ic.ttl = 3;
  } else if (ic.ttl > 0) {
    ic.ttl -= 1;
  }

  // 敵編隊
  for (const es of state.enemy.squadrons) {
    const prev = state.intel.squadrons.get(es.id) || { seen: false, x: null, y: null, ttl: 0 };
    if (isVisibleToPlayer(es.x, es.y)) {
      state.intel.squadrons.set(es.id, { seen: true, x: es.x, y: es.y, ttl: 3 });
    } else {
      if (prev.ttl > 0) prev.ttl -= 1;
      state.intel.squadrons.set(es.id, prev);
    }
  }

  // 既に消滅した敵編隊の記憶TTLも減衰（ゼロで放置）
  for (const [id, m] of state.intel.squadrons.entries()) {
    if (!state.enemy.squadrons.find((s) => s.id === id) && m.ttl > 0 && !isVisibleToPlayer(m.x, m.y)) {
      m.ttl -= 1; state.intel.squadrons.set(id, m);
    }
  }
}

function checkGameEnd() {
  if (state.gameOver) return;
  if (state.enemy.carrier.hp <= 0) return finishGame('win', '敵空母撃沈！勝利');
  if (state.carrier.hp <= 0) return finishGame('lose', '我が空母撃沈…敗北');
  if (state.turn >= 20) {
    if (state.carrier.hp > state.enemy.carrier.hp) return finishGame('win', '終戦判定：優勢で勝利');
    if (state.carrier.hp < state.enemy.carrier.hp) return finishGame('lose', '終戦判定：劣勢で敗北');
    return finishGame('draw', '終戦判定：引き分け');
  }
}

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
  state.carrier = { id: 'C1', x: 3, y: 3, hp: 100, speed: 2, vision: 10, hangar: 2 };
  state.enemy = { carrier: { id: 'E1', x: 26, y: 26, hp: 100, speed: 2, vision: 10, hangar: 2 }, squadrons: [] };
  state.intel = { carrier: { seen: false, x: null, y: null, ttl: 0 }, squadrons: new Map() };
  state.enemyIntel = { carrier: { seen: false, x: null, y: null, ttl: 0 } };
  state.enemyAI = { patrolIx: 0, lastPatrolTurn: 0 };
  state.squadrons = Array.from({ length: state.carrier.hangar }, (_, i) => ({ id: `SQ${i + 1}`, hp: SQUAD_MAX_HP, state: 'base' }));
  state.enemy.squadrons = Array.from({ length: state.enemy.carrier.hangar }, (_, i) => ({ id: `ESQ${i + 1}`, hp: SQUAD_MAX_HP, state: 'base' }));
  state.highlight = null;
  state.gameOver = false;

  // regenerate map and place units
  generateMap();
  placeEnemyCarrier();

  // UI
  enableControls();
  document.querySelectorAll('[data-mode]').forEach((b) => b.classList.remove('active'));
  document.getElementById('btnModeSelect').classList.add('active');
  setHint();
  clearLog();
  logMsg(kind === 'newmap' ? '新しい海域で作戦開始: ターン1' : 'リスタート: ターン1');
  renderAll();
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
