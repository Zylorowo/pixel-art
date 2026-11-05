"use strict";
/*
 * Pixel-War-Leinwand. Vanilla JS, kein Framework. Braucht common.js.
 *
 * Wichtigste Regel: Das Frontend berechnet NIE Preise. Es zeigt nur an, was der
 * Server über /api/prices und den WebSocket meldet, und schickt beim Malen den
 * angezeigten Preis mit. Ist der inzwischen gestiegen, lehnt der Server ab.
 *
 * Darstellung: Die Leinwand liegt als 400×400-Bild in einem unsichtbaren
 * "Offscreen"-Canvas (1 Bildpunkt = 1 Pixel). Der sichtbare Canvas zeichnet
 * dieses Bild nur vergrößert und verschoben. Zoomen/Verschieben ändert also nur
 * drei Zahlen (scale, ox, oy) – das ist auch auf dem Handy flüssig.
 * Die Preiskarte ist ein zweites Offscreen-Bild, das darübergelegt wird.
 */

const state = {
  size: 400,
  palette: [],          // Hex-Strings, Index 0 = leer
  rgb: [],              // [r, g, b] je Index
  pixels: null,         // Uint8Array, ein Farbindex pro Pixel, Zeile für Zeile
  prices: null,         // Uint32Array, Preis in Cent pro Pixel (vom Server!)
  canvasVersion: -1,    // Stand der Schnappschüsse, siehe resync()
  pricesVersion: -1,
  color: 1,
  tool: "brush",        // "brush" | "picker" | "info"
  selected: null,       // {x, y}
  hover: null,          // {x, y}
  showHeat: false,
};

const board = $("board");
const ctx = board.getContext("2d");
const off = document.createElement("canvas");
const heatOff = document.createElement("canvas");
let offCtx, img, heatCtx, heatImg;
// Ansicht: 1 Leinwand-Pixel = `scale` Bildschirmpixel; (ox, oy) = Bildschirmposition der Leinwand-Ecke oben links.
const view = { scale: 1, ox: 0, oy: 0, minScale: 1 };
const MAX_SCALE = 60;
let needsDraw = true, imgDirty = true, heatDirty = true;

const idx = (x, y) => y * state.size + x;

function hexToRgb(h) {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// ---------------------------------------------------------------- Pixel & Preise

function setPixel(x, y, c) {
  const i = idx(x, y);
  state.pixels[i] = c;
  const rgb = state.rgb[c] || [255, 0, 255];
  const d = img.data, j = i * 4;
  d[j] = rgb[0]; d[j + 1] = rgb[1]; d[j + 2] = rgb[2]; d[j + 3] = 255;
  imgDirty = true;
}

function redrawAllPixels() {
  for (let y = 0; y < state.size; y++)
    for (let x = 0; x < state.size; x++) setPixel(x, y, state.pixels[idx(x, y)]);
}

/*
 * Preiskarte: Die Farbe zeigt, wie viel teurer ein Pixel als das billigste ist.
 * Das ist reine Darstellung – der Preis selbst kommt vom Server.
 * Logarithmisch, weil Preise exponentiell steigen: 2×, 4×, 8× teurer sollen
 * gleichmäßig dunkler werden.
 */
let cheapest = 1;
function setHeatPixel(i) {
  const ratio = state.prices[i] / cheapest;
  const a = ratio <= 1 ? 0 : Math.min(200, 40 + Math.log2(ratio) * 45);
  const d = heatImg.data, j = i * 4;
  d[j] = 255; d[j + 1] = 30; d[j + 2] = 0; d[j + 3] = a;
  heatDirty = true;
}

function rebuildHeat() {
  cheapest = Math.max(1, state.prices.reduce((m, p) => (p < m ? p : m), Infinity));
  for (let i = 0; i < state.prices.length; i++) setHeatPixel(i);
}

function setPrice(x, y, p) {
  const i = idx(x, y);
  state.prices[i] = p;
  setHeatPixel(i);
}

// ---------------------------------------------------------------- Live-Verbindung

let ws = null, wsBuffer = null, reconnectDelay = 500;

function setConn(on) {
  const el = $("conn");
  el.hidden = false;
  el.className = "conn " + (on ? "on" : "off");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  // Nachrichten, die ankommen, während wir die Schnappschüsse laden, werden
  // gepuffert und danach angewendet.
  wsBuffer = [];
  ws.onopen = () => resync(ws);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (wsBuffer) wsBuffer.push(msg); else handleMessage(msg);
  };
  ws.onclose = () => {
    setConn(false);
    // Exponentielles Warten mit Zufallsanteil: Stürzt der Server kurz ab,
    // sollen nicht 2.000 Browser im selben Moment wieder anklopfen.
    const wait = reconnectDelay * (0.5 + Math.random());
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    setTimeout(connect, wait);
  };
}

async function fetchSnapshot(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(url);
  return { buf: await res.arrayBuffer(), version: parseInt(res.headers.get("X-Version"), 10) };
}

async function loadPrices() {
  const snap = await fetchSnapshot("/api/prices");
  state.prices.set(new Uint32Array(snap.buf));
  state.pricesVersion = snap.version;
  rebuildHeat();
}

/*
 * Lückenloser Neuaufbau: ERST WebSocket verbinden, DANN Schnappschüsse laden.
 * Jeder Schnappschuss nennt eine Version v. Alle WebSocket-Pakete mit
 * Nummer ≤ v sind darin schon enthalten und werden verworfen, alle neueren
 * angewendet. So geht beim (Neu-)Verbinden kein einziges Pixel verloren.
 */
async function resync(sock) {
  try {
    const [canvas] = await Promise.all([fetchSnapshot("/api/canvas"), loadPrices()]);
    if (sock !== ws) return; // in der Zwischenzeit neu verbunden
    state.pixels.set(new Uint8Array(canvas.buf));
    state.canvasVersion = canvas.version;
    redrawAllPixels();
    const pending = wsBuffer || [];
    wsBuffer = null;
    pending.forEach(handleMessage);
    setConn(true);
    reconnectDelay = 500;
    updateInfoLine();
  } catch (e) {
    sock.close();
  }
}

let reloadingPrices = false;
function handleMessage(msg) {
  if (msg.type === "pixel") {
    if (msg.v <= state.canvasVersion) return;
    const d = msg.data;
    for (let i = 0; i < d.length; i += 3) setPixel(d[i], d[i + 1], d[i + 2]);
    state.canvasVersion = msg.v;
  } else if (msg.type === "price") {
    if (msg.v <= state.pricesVersion) return;
    const d = msg.data;
    for (let i = 0; i < d.length; i += 3) setPrice(d[i], d[i + 1], d[i + 2]);
    state.pricesVersion = msg.v;
    updateInfoLine();
  } else if (msg.type === "prices_reset") {
    // Admin hat die Preisparameter geändert → alle Preise neu laden.
    if (msg.v <= state.pricesVersion || reloadingPrices) return;
    reloadingPrices = true;
    loadPrices().then(updateInfoLine).finally(() => (reloadingPrices = false));
  }
}

// ---------------------------------------------------------------- Ansicht (Zoom & Verschieben)

function viewport() {
  const r = board.getBoundingClientRect();
  return { w: r.width, h: r.height, left: r.left, top: r.top };
}

/*
 * Kleinster Zoom = die ganze Leinwand passt genau ins Fenster. Weiter raus geht
 * nicht. Außerdem darf die Leinwand nicht aus dem Bild geschoben werden: Ist
 * sie in einer Richtung kleiner als das Fenster, wird sie dort zentriert.
 */
function clampView() {
  const { w, h } = viewport();
  view.minScale = Math.min(w, h) / state.size;
  view.scale = Math.min(MAX_SCALE, Math.max(view.minScale, view.scale));
  const cw = state.size * view.scale, ch = state.size * view.scale;
  view.ox = cw <= w ? (w - cw) / 2 : Math.min(0, Math.max(w - cw, view.ox));
  view.oy = ch <= h ? (h - ch) / 2 : Math.min(0, Math.max(h - ch, view.oy));
  needsDraw = true;
}

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const { w, h } = viewport();
  const wasFit = view.scale <= view.minScale + 1e-6;
  board.width = Math.round(w * dpr);
  board.height = Math.round(h * dpr);
  if (wasFit) view.scale = 0; // bleibt "ganz eingepasst", wenn es das vorher war
  clampView();
}

function zoomAt(clientX, clientY, newScale) {
  // Zoomen so, dass der Punkt unter Finger/Maus an derselben Stelle bleibt.
  const { left, top } = viewport();
  const px = clientX - left, py = clientY - top;
  newScale = Math.min(MAX_SCALE, Math.max(view.minScale, newScale));
  view.ox = px - (px - view.ox) * (newScale / view.scale);
  view.oy = py - (py - view.oy) * (newScale / view.scale);
  view.scale = newScale;
  clampView();
}

function screenToCanvas(clientX, clientY) {
  const { left, top } = viewport();
  const x = Math.floor((clientX - left - view.ox) / view.scale);
  const y = Math.floor((clientY - top - view.oy) / view.scale);
  if (x < 0 || y < 0 || x >= state.size || y >= state.size) return null;
  return { x, y };
}

// ---------------------------------------------------------------- Zeichnen

function frame() {
  if (imgDirty) { offCtx.putImageData(img, 0, 0); imgDirty = false; needsDraw = true; }
  if (heatDirty && state.showHeat) { heatCtx.putImageData(heatImg, 0, 0); heatDirty = false; needsDraw = true; }
  if (needsDraw) { draw(); needsDraw = false; }
  requestAnimationFrame(frame);
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#15151a";
  ctx.fillRect(0, 0, board.width, board.height);

  // imageSmoothingEnabled=false: harte Pixelkanten statt Matsch beim Vergrößern.
  const s = view.scale * dpr;
  ctx.setTransform(s, 0, 0, s, view.ox * dpr, view.oy * dpr);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0);
  if (state.showHeat) ctx.drawImage(heatOff, 0, 0);

  // Pixelraster ab starkem Zoom, damit man einzelne Pixel treffen kann.
  if (view.scale >= 12) {
    const { w, h } = viewport();
    const x0 = Math.max(0, Math.floor(-view.ox / view.scale)), x1 = Math.min(state.size, Math.ceil((w - view.ox) / view.scale));
    const y0 = Math.max(0, Math.floor(-view.oy / view.scale)), y1 = Math.min(state.size, Math.ceil((h - view.oy) / view.scale));
    ctx.strokeStyle = "rgba(0,0,0,0.08)";
    ctx.lineWidth = 1 / view.scale;
    ctx.beginPath();
    for (let x = x0; x <= x1; x++) { ctx.moveTo(x, y0); ctx.lineTo(x, y1); }
    for (let y = y0; y <= y1; y++) { ctx.moveTo(x0, y); ctx.lineTo(x1, y); }
    ctx.stroke();
  }

  const outline = (p, color, width) => {
    ctx.lineWidth = Math.max(width / view.scale, 0.08);
    ctx.strokeStyle = color;
    ctx.strokeRect(p.x, p.y, 1, 1);
  };
  const h = state.hover, sel = state.selected;
  if (h && !(sel && sel.x === h.x && sel.y === h.y)) outline(h, "rgba(255,255,255,0.8)", 1.5);
  if (state.selected) {
    outline({ x: state.selected.x - 0.1, y: state.selected.y - 0.1 }, "#000", 2);
    outline(state.selected, "#fff", 1.5);
  }
}

// ---------------------------------------------------------------- Werkzeuge

function setTool(tool) {
  state.tool = tool;
  document.querySelectorAll(".tool").forEach((b) => {
    const on = b.dataset.tool === tool;
    b.classList.toggle("active", on);
    b.setAttribute("aria-checked", on);
  });
  board.dataset.tool = tool; // steuert den Mauszeiger per CSS
  updateInfoLine();
}

function setColor(i) {
  state.color = i;
  document.querySelectorAll(".swatch").forEach((s) => s.classList.toggle("active", +s.dataset.color === i));
  const active = document.querySelector(".swatch.active");
  if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function onTap(e) {
  const p = screenToCanvas(e.clientX, e.clientY);
  if (!p) return;
  if (state.tool === "picker") {
    const c = state.pixels[idx(p.x, p.y)];
    if (c === 0) { toast(t("canvas.picked_empty")); return; }
    setColor(c);
    setTool("brush"); // nach dem Aufnehmen will man fast immer malen
    toast(t("canvas.picked"), 1200);
    return;
  }
  if (state.tool === "info") {
    showPixelInfo(p.x, p.y);
    return;
  }
  // Pinsel. Maus: Klick malt direkt (der Preis stand schon unten links).
  // Finger: Tippen wählt nur aus, gemalt wird mit dem Knopf – sonst würde man
  // beim Verschieben aus Versehen Geld ausgeben.
  state.selected = p;
  needsDraw = true;
  updateInfoLine();
  if (e.pointerType === "mouse") paint();
}

async function showPixelInfo(x, y) {
  try {
    const info = await api(`/api/pixel?x=${x}&y=${y}`);
    $("pixel-title").textContent = t("info.title", { x, y });
    const rows = [
      [t("info.price"), fmtMoney(info.price_cents)],
      [t("info.overpaints"), fmtNumber(info.overpaint_count)],
      [t("info.last_painted"), info.painted_at ? fmtDateTime(info.painted_at) : t("info.never")],
      [t("info.user"), info.user || t("info.none")],
    ];
    const dl = $("pixel-details");
    dl.innerHTML = "";
    for (const [k, v] of rows) {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      dl.append(dt, dd);
    }
    // Gilde als Link zur Gildenseite (textContent statt innerHTML: Gildennamen
    // stammen von Nutzern und dürfen nie als HTML interpretiert werden).
    const dt = document.createElement("dt"); dt.textContent = t("info.guild");
    const dd = document.createElement("dd");
    if (info.guild) {
      const a = document.createElement("a");
      a.href = `/c/${encodeURIComponent(info.guild.slug)}`;
      a.className = "guild-chip";
      a.style.setProperty("--guild", info.guild.color);
      a.textContent = info.guild.name;
      dd.appendChild(a);
    } else {
      dd.textContent = t("info.none");
    }
    dl.append(dt, dd);
    $("pixel-dialog").showModal();
  } catch (err) {
    toast(errorText(err));
  }
}

// ---------------------------------------------------------------- Malen

let painting = false;

async function paint() {
  if (!state.selected || painting) return;
  if (!Account.me) { toast(t("account.login_required")); $("login-handle")?.focus(); return; }
  const { x, y } = state.selected;
  const shownPrice = state.prices[idx(x, y)];
  const color = state.color;
  painting = true;
  updateInfoLine();
  try {
    const r = await api("/api/paint", {
      method: "POST",
      body: JSON.stringify({ x, y, color_index: color, max_price_cents: shownPrice }),
    });
    // Sofort lokal anzeigen; die Bestätigung per WebSocket folgt ohnehin.
    setPixel(x, y, color);
    setPrice(x, y, r.new_price_cents);
    Account.me.balance_cents = r.balance_cents;
    renderHeader();
  } catch (err) {
    const d = err.data || {};
    if (d.error === "price_changed") {
      setPrice(x, y, d.current_price_cents);
      toast(t("canvas.price_rose", { price: fmtMoney(d.current_price_cents) }));
    } else {
      if (err.status === 401) await loadMe();
      toast(errorText(err));
    }
  } finally {
    painting = false;
    updateInfoLine();
  }
}

function updateHoverStatus() {
  // Unten links: Koordinate + Preis des Pixels unter der Maus (bzw. des
  // ausgewählten Pixels auf Touch-Geräten, die kein "Überfahren" kennen).
  const p = state.hover || state.selected;
  const el = $("hover-status");
  if (!p || !state.prices) { el.hidden = true; return; }
  const i = idx(p.x, p.y);
  el.hidden = false;
  el.textContent = `(${p.x}, ${p.y}) · ${fmtMoney(state.prices[i])}` + (state.pixels[i] === 0 ? ` · ${t("canvas.empty")}` : "");
}

function updateInfoLine() {
  updateHoverStatus();
  const btn = $("paint-btn");
  const brush = state.tool === "brush";
  btn.hidden = !brush;
  $("info-line").textContent = brush && !state.selected ? t("canvas.tap_hint") : "";
  if (!brush) return;
  if (!state.selected || !state.prices) {
    btn.disabled = true;
    btn.textContent = t("canvas.paint");
    return;
  }
  btn.textContent = t("canvas.paint_price", { price: fmtMoney(state.prices[idx(state.selected.x, state.selected.y)]) });
  btn.disabled = painting || EventClock.event?.phase !== "running";
}

// ---------------------------------------------------------------- Eingabe (Maus, Touch, Stift)

/*
 * Pointer Events vereinheitlichen Maus, Finger und Stift. Wir merken uns alle
 * aktiven Zeiger:
 *   1 Zeiger, kaum bewegt, losgelassen → Tippen (je nach Werkzeug)
 *   1 Zeiger, bewegt                   → Verschieben
 *   2 Zeiger                           → Pinch-Zoom + Verschieben
 */
const pointers = new Map();
let gesture = null;
const TAP_SLOP = 6; // px Bewegung, ab der es kein Tippen mehr ist

function startPan(p, type = "tap") {
  gesture = { type, sx: p.x, sy: p.y, ox: view.ox, oy: view.oy };
}

function startPinch() {
  const [a, b] = [...pointers.values()];
  gesture = {
    type: "pinch",
    dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
    mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2,
    scale: view.scale, ox: view.ox, oy: view.oy,
  };
}

function setHover(p) {
  const same = p && state.hover && p.x === state.hover.x && p.y === state.hover.y;
  if (same || (!p && !state.hover)) return;
  state.hover = p;
  needsDraw = true;
  updateHoverStatus();
}

function setupInput() {
  board.addEventListener("pointerdown", (e) => {
    board.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) startPan({ x: e.clientX, y: e.clientY });
    else if (pointers.size === 2) startPinch();
  });

  board.addEventListener("pointermove", (e) => {
    if (e.pointerType === "mouse") setHover(screenToCanvas(e.clientX, e.clientY));
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (!gesture) return;

    if (gesture.type === "pinch" && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const { left, top } = viewport();
      const newScale = Math.min(MAX_SCALE, Math.max(view.minScale, gesture.scale * dist / gesture.dist));
      // Leinwandpunkt, der am Anfang unter der Fingermitte lag …
      const cx = (gesture.mx - left - gesture.ox) / gesture.scale;
      const cy = (gesture.my - top - gesture.oy) / gesture.scale;
      // … soll jetzt unter der aktuellen Fingermitte liegen.
      view.scale = newScale;
      view.ox = mx - left - cx * newScale;
      view.oy = my - top - cy * newScale;
      clampView();
    } else if (gesture.type === "tap" || gesture.type === "pan") {
      const dx = e.clientX - gesture.sx, dy = e.clientY - gesture.sy;
      if (gesture.type === "tap" && Math.hypot(dx, dy) > TAP_SLOP) gesture.type = "pan";
      if (gesture.type === "pan") {
        view.ox = gesture.ox + dx;
        view.oy = gesture.oy + dy;
        clampView();
      }
    }
  });

  const end = (e) => {
    if (!pointers.has(e.pointerId)) return;
    const wasTap = gesture && gesture.type === "tap" && pointers.size === 1;
    pointers.delete(e.pointerId);
    if (wasTap && e.type === "pointerup") onTap(e);
    if (pointers.size === 1) {
      // Nach dem Pinch mit dem verbleibenden Finger weiterschieben (kein Tippen!).
      startPan([...pointers.values()][0], "pan");
    } else if (pointers.size === 0) {
      gesture = null;
    }
  };
  board.addEventListener("pointerup", end);
  board.addEventListener("pointercancel", end);
  board.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") setHover(null); });

  board.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, view.scale * Math.exp(-e.deltaY * 0.0015));
  }, { passive: false });

  // ResizeObserver statt window.resize: Die Fläche ändert sich auch, wenn z. B.
  // die Kopfzeile nach dem Login umbricht, ohne dass sich das Fenster ändert.
  new ResizeObserver(resize).observe(board);

  window.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, dialog")) return;
    if ((e.key === " " || e.key === "Enter") && state.selected && state.tool === "brush") { e.preventDefault(); paint(); }
    if (e.key === "b") setTool("brush");
    if (e.key === "i") setTool("info");
    if (e.key === "p") setTool("picker");
  });
}

// ---------------------------------------------------------------- Start

function buildPalette() {
  const el = $("palette");
  el.innerHTML = "";
  // Index 0 ist "leer" und nicht malbar.
  for (let i = 1; i < state.palette.length; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch";
    b.dataset.color = i;
    b.style.background = state.palette[i];
    b.setAttribute("role", "radio");
    b.setAttribute("aria-label", state.palette[i]);
    b.addEventListener("click", () => setColor(i));
    el.appendChild(b);
  }
  setColor(state.color);
}

async function main() {
  const cfg = await initPage();
  state.size = cfg.canvas_size;
  state.palette = cfg.palette;
  state.rgb = cfg.palette.map(hexToRgb);
  state.pixels = new Uint8Array(state.size * state.size);
  state.prices = new Uint32Array(state.size * state.size);

  off.width = off.height = heatOff.width = heatOff.height = state.size;
  offCtx = off.getContext("2d");
  heatCtx = heatOff.getContext("2d");
  img = offCtx.createImageData(state.size, state.size);
  heatImg = heatCtx.createImageData(state.size, state.size);
  redrawAllPixels();

  buildPalette();
  setupInput();
  document.querySelectorAll(".tool").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("paint-btn").addEventListener("click", paint);
  $("heat-toggle").addEventListener("change", (e) => { state.showHeat = e.target.checked; heatDirty = true; needsDraw = true; });
  document.addEventListener("langchange", updateInfoLine);
  onAccountChange(updateInfoLine);
  setInterval(updateInfoLine, 5000); // Event-Phase (Start/Ende) im Malen-Knopf nachziehen

  setTool("brush");
  resize();
  connect();
  requestAnimationFrame(frame);
}

main().catch((e) => {
  console.error(e);
  toast(t("canvas.load_failed"), 10000);
});
