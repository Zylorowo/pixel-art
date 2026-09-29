"use strict";
/*
 * Pixelkrieg-Frontend. Vanilla JS, kein Framework.
 *
 * Wichtigste Regel: Das Frontend berechnet NIE Preise. Es zeigt nur an, was der
 * Server über /api/zones und den WebSocket meldet, und schickt beim Malen den
 * angezeigten Preis mit. Ist der inzwischen gestiegen, lehnt der Server ab.
 *
 * Darstellung: Die Leinwand liegt als 400×400-Bild in einem unsichtbaren
 * "Offscreen"-Canvas (1 Bildpunkt = 1 Pixel). Der sichtbare Canvas zeichnet
 * dieses Bild nur vergrößert und verschoben. Zoomen/Verschieben ändert also nur
 * zwei Zahlen (scale, ox/oy) – das ist auch auf dem Handy flüssig.
 */

const $ = (id) => document.getElementById(id);

const state = {
  size: 400,
  zoneSize: 20,
  zonesPerSide: 20,
  palette: [],          // Hex-Strings, Index 0 = leer
  rgb: [],              // [r, g, b] je Index
  pixels: null,         // Uint8Array, ein Farbindex pro Pixel, Zeile für Zeile
  zonePrice: null,      // Int32Array, Preis in Cent je Zone (vom Server!)
  zoneHeat: null,
  zoneFrozen: null,
  canvasVersion: -1,    // Stand des Schnappschusses, siehe resync()
  zonesVersion: -1,
  me: null,
  color: 1,
  selected: null,       // {x, y}
  hover: null,          // {x, y}
  showHeat: false,
  event: null,
  clockOffset: 0,       // Serverzeit minus lokale Zeit (ms)
};

const board = $("board");
const ctx = board.getContext("2d");
const off = document.createElement("canvas");
let offCtx, img;
// Ansicht: 1 Leinwand-Pixel = `scale` Bildschirmpixel; (ox, oy) = Bildschirmposition der Leinwand-Ecke oben links.
const view = { scale: 2, ox: 0, oy: 0 };
let needsDraw = true;
let imgDirty = true;
let viewTouched = false; // hat der Nutzer schon gezoomt/verschoben?

// ---------------------------------------------------------------- Hilfen

function fmtCents(c) {
  // Nur Formatierung fürs Auge, keine Preisberechnung.
  if (c < 100) return `${c} ct`;
  return `${(c / 100).toFixed(2).replace(".", ",")} €`;
}

function hexToRgb(h) {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

let toastTimer = null;
function toast(msg, ms = 2500) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* leere Antwort */ }
  if (!res.ok) {
    const err = new Error((data && data.message) || res.statusText);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

const zoneIndex = (x, y) =>
  Math.floor(y / state.zoneSize) * state.zonesPerSide + Math.floor(x / state.zoneSize);

// ---------------------------------------------------------------- Pixel & Zonen

function setPixel(x, y, c) {
  const i = y * state.size + x;
  state.pixels[i] = c;
  const rgb = state.rgb[c] || [255, 0, 255];
  const d = img.data, j = i * 4;
  d[j] = rgb[0]; d[j + 1] = rgb[1]; d[j + 2] = rgb[2]; d[j + 3] = 255;
  imgDirty = true;
}

function redrawAllPixels() {
  const d = img.data;
  for (let i = 0; i < state.pixels.length; i++) {
    const rgb = state.rgb[state.pixels[i]] || [255, 0, 255];
    const j = i * 4;
    d[j] = rgb[0]; d[j + 1] = rgb[1]; d[j + 2] = rgb[2]; d[j + 3] = 255;
  }
  imgDirty = true;
}

function setZone(zx, zy, price, heat) {
  const i = zy * state.zonesPerSide + zx;
  state.zonePrice[i] = price;
  state.zoneHeat[i] = heat;
  needsDraw = true;
}

// ---------------------------------------------------------------- Live-Verbindung

let ws = null;
let wsBuffer = null;
let reconnectDelay = 500;

function setConn(on) {
  $("conn").className = "conn " + (on ? "on" : "off");
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  // Nachrichten, die ankommen, während wir den Schnappschuss laden, werden
  // gepuffert und danach angewendet.
  wsBuffer = [];
  ws.onopen = () => resync(ws);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (wsBuffer) wsBuffer.push(msg); else handleMessage(msg);
  };
  ws.onclose = () => {
    setConn(false);
    // Exponentielles Warten: Stürzt der Server kurz ab, sollen nicht 2.000
    // Browser im selben Moment wieder anklopfen. Mit Zufallsanteil.
    const wait = reconnectDelay * (0.5 + Math.random());
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    setTimeout(connect, wait);
  };
}

/*
 * Lückenloser Neuaufbau: ERST WebSocket verbinden, DANN Schnappschuss laden.
 * Der Schnappschuss nennt eine Version v. Alle WebSocket-Pakete mit Nummer ≤ v
 * sind darin schon enthalten und werden verworfen, alle neueren angewendet.
 * So geht beim (Neu-)Verbinden kein einziges Pixel verloren.
 */
async function resync(sock) {
  try {
    const [canvasRes, zones] = await Promise.all([
      fetch("/api/canvas", { cache: "no-store" }),
      api("/api/zones"),
    ]);
    if (!canvasRes.ok) throw new Error("canvas");
    const buf = new Uint8Array(await canvasRes.arrayBuffer());
    if (sock !== ws) return; // in der Zwischenzeit neu verbunden
    state.pixels.set(buf);
    state.canvasVersion = parseInt(canvasRes.headers.get("X-Canvas-Version"), 10);
    redrawAllPixels();

    state.zonePrice.set(zones.prices);
    state.zoneHeat.set(zones.heat);
    state.zoneFrozen.set(zones.frozen);
    state.zonesVersion = zones.version;

    const pending = wsBuffer || [];
    wsBuffer = null;
    pending.forEach(handleMessage);
    setConn(true);
    reconnectDelay = 500;
    updateInfo();
  } catch (e) {
    sock.close();
  }
}

function handleMessage(msg) {
  if (msg.type === "pixel") {
    if (msg.v <= state.canvasVersion) return;
    const d = msg.data;
    for (let i = 0; i < d.length; i += 3) setPixel(d[i], d[i + 1], d[i + 2]);
    state.canvasVersion = msg.v;
  } else if (msg.type === "zone_price") {
    if (msg.v <= state.zonesVersion) return;
    const d = msg.data;
    for (let i = 0; i < d.length; i += 4) setZone(d[i], d[i + 1], d[i + 2], d[i + 3]);
    state.zonesVersion = msg.v;
    updateInfo();
  } else if (msg.type === "freeze") {
    // Phase 3 (Moderation)
  }
}

// ---------------------------------------------------------------- Zeichnen

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const r = board.getBoundingClientRect();
  board.width = Math.round(r.width * dpr);
  board.height = Math.round(r.height * dpr);
  needsDraw = true;
}

function fitView() {
  const r = board.getBoundingClientRect();
  view.scale = Math.max(0.5, Math.min(r.width, r.height) / state.size * 0.95);
  view.ox = (r.width - state.size * view.scale) / 2;
  view.oy = (r.height - state.size * view.scale) / 2;
  needsDraw = true;
}

function frame() {
  if (imgDirty) {
    offCtx.putImageData(img, 0, 0);
    imgDirty = false;
    needsDraw = true;
  }
  if (needsDraw) {
    draw();
    needsDraw = false;
  }
  requestAnimationFrame(frame);
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#15151a";
  ctx.fillRect(0, 0, board.width, board.height);

  // Leinwand vergrößert zeichnen. imageSmoothingEnabled=false: harte
  // Pixelkanten statt Matsch beim Vergrößern.
  const s = view.scale * dpr;
  ctx.setTransform(s, 0, 0, s, view.ox * dpr, view.oy * dpr);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0);

  const zs = state.zoneSize, nz = state.zonesPerSide;
  if (state.showHeat) {
    for (let zy = 0; zy < nz; zy++) {
      for (let zx = 0; zx < nz; zx++) {
        const i = zy * nz + zx;
        const h = state.zoneHeat[i];
        if (state.zoneFrozen[i]) {
          ctx.fillStyle = "rgba(128,128,128,0.7)";
          ctx.fillRect(zx * zs, zy * zs, zs, zs);
        } else if (h > 0) {
          ctx.fillStyle = `rgba(255,40,0,${Math.min(0.65, 0.08 + h * 0.05)})`;
          ctx.fillRect(zx * zs, zy * zs, zs, zs);
        }
      }
    }
    ctx.strokeStyle = "rgba(0,0,0,0.25)";
    ctx.lineWidth = 1 / view.scale;
    ctx.beginPath();
    for (let k = 0; k <= nz; k++) {
      ctx.moveTo(k * zs, 0); ctx.lineTo(k * zs, state.size);
      ctx.moveTo(0, k * zs); ctx.lineTo(state.size, k * zs);
    }
    ctx.stroke();
  }

  // Zone unter dem Mauszeiger / ausgewählte Zone umranden.
  const focus = state.hover || state.selected;
  if (focus) {
    const zx = Math.floor(focus.x / zs), zy = Math.floor(focus.y / zs);
    ctx.strokeStyle = "rgba(255,255,255,0.6)";
    ctx.lineWidth = 2 / view.scale;
    ctx.strokeRect(zx * zs, zy * zs, zs, zs);
  }
  if (state.selected) {
    const { x, y } = state.selected;
    ctx.lineWidth = Math.max(1 / view.scale, 0.12);
    ctx.strokeStyle = "#000";
    ctx.strokeRect(x - 0.1, y - 0.1, 1.2, 1.2);
    ctx.strokeStyle = "#fff";
    ctx.strokeRect(x + 0.05, y + 0.05, 0.9, 0.9);
  }

  // Preise als Text in die Zonen schreiben, wenn sie groß genug sind.
  if (state.showHeat && zs * view.scale >= 44) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = "600 12px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let zy = 0; zy < nz; zy++) {
      for (let zx = 0; zx < nz; zx++) {
        const cx = view.ox + (zx + 0.5) * zs * view.scale;
        const cy = view.oy + (zy + 0.5) * zs * view.scale;
        if (cx < -50 || cy < -50 || cx > board.width / dpr + 50 || cy > board.height / dpr + 50) continue;
        const label = fmtCents(state.zonePrice[zy * nz + zx]);
        ctx.lineWidth = 3;
        ctx.strokeStyle = "rgba(0,0,0,0.7)";
        ctx.strokeText(label, cx, cy);
        ctx.fillStyle = "#fff";
        ctx.fillText(label, cx, cy);
      }
    }
  }
}

// ---------------------------------------------------------------- Eingabe (Maus, Touch, Stift)

function screenToCanvas(clientX, clientY) {
  const r = board.getBoundingClientRect();
  const x = Math.floor((clientX - r.left - view.ox) / view.scale);
  const y = Math.floor((clientY - r.top - view.oy) / view.scale);
  if (x < 0 || y < 0 || x >= state.size || y >= state.size) return null;
  return { x, y };
}

function zoomAt(clientX, clientY, newScale) {
  // Zoomen so, dass der Punkt unter Finger/Maus an derselben Stelle bleibt.
  const r = board.getBoundingClientRect();
  const px = clientX - r.left, py = clientY - r.top;
  newScale = Math.min(60, Math.max(0.5, newScale));
  view.ox = px - (px - view.ox) * (newScale / view.scale);
  view.oy = py - (py - view.oy) * (newScale / view.scale);
  view.scale = newScale;
  viewTouched = true;
  needsDraw = true;
}

/*
 * Pointer Events vereinheitlichen Maus, Finger und Stift. Wir merken uns alle
 * aktiven Zeiger:
 *   1 Zeiger, kaum bewegt, losgelassen → Tippen (Pixel wählen / malen)
 *   1 Zeiger, bewegt                   → Verschieben
 *   2 Zeiger                           → Pinch-Zoom + Verschieben
 */
const pointers = new Map();
let gesture = null;
const TAP_SLOP = 6; // px Bewegung, ab der es kein Tippen mehr ist

function startPan(p) {
  gesture = { type: "tap", sx: p.x, sy: p.y, ox: view.ox, oy: view.oy };
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

function setupInput() {
  board.addEventListener("pointerdown", (e) => {
    board.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.size === 1) startPan({ x: e.clientX, y: e.clientY });
    else if (pointers.size === 2) startPinch();
  });

  board.addEventListener("pointermove", (e) => {
    if (e.pointerType === "mouse" && pointers.size === 0) {
      const p = screenToCanvas(e.clientX, e.clientY);
      const changed = (p && state.hover) ? (p.x !== state.hover.x || p.y !== state.hover.y) : p !== state.hover;
      if (changed) { state.hover = p; needsDraw = true; updateInfo(); }
      return;
    }
    if (!pointers.has(e.pointerId)) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (!gesture) return;

    if (gesture.type === "pinch" && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      const r = board.getBoundingClientRect();
      const newScale = Math.min(60, Math.max(0.5, gesture.scale * dist / gesture.dist));
      // Leinwandpunkt, der am Anfang unter der Fingermitte lag …
      const cx = (gesture.mx - r.left - gesture.ox) / gesture.scale;
      const cy = (gesture.my - r.top - gesture.oy) / gesture.scale;
      // … soll jetzt unter der aktuellen Fingermitte liegen.
      view.scale = newScale;
      view.ox = mx - r.left - cx * newScale;
      view.oy = my - r.top - cy * newScale;
      viewTouched = true;
      needsDraw = true;
    } else if (gesture.type === "tap" || gesture.type === "pan") {
      const dx = e.clientX - gesture.sx, dy = e.clientY - gesture.sy;
      if (gesture.type === "tap" && Math.hypot(dx, dy) > TAP_SLOP) gesture.type = "pan";
      if (gesture.type === "pan") {
        view.ox = gesture.ox + dx;
        view.oy = gesture.oy + dy;
        viewTouched = true;
        needsDraw = true;
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
      const [p] = [...pointers.values()];
      startPan(p);
      gesture.type = "pan";
    } else if (pointers.size === 0) {
      gesture = null;
    }
  };
  board.addEventListener("pointerup", end);
  board.addEventListener("pointercancel", end);
  board.addEventListener("pointerleave", (e) => {
    if (e.pointerType === "mouse" && state.hover) { state.hover = null; needsDraw = true; updateInfo(); }
  });

  board.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, view.scale * Math.exp(-e.deltaY * 0.0015));
  }, { passive: false });

  // ResizeObserver statt window.resize: Die Fläche ändert sich auch, wenn z. B.
  // die Kopfzeile nach dem Login umbricht, ohne dass sich das Fenster ändert.
  // Solange der Nutzer die Ansicht nicht selbst bewegt hat, neu einpassen.
  new ResizeObserver(() => {
    resize();
    if (!viewTouched) fitView();
  }).observe(board);
  window.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && state.selected && document.activeElement === document.body) {
      e.preventDefault();
      paint();
    }
  });
}

function onTap(e) {
  const p = screenToCanvas(e.clientX, e.clientY);
  if (!p) return;
  state.selected = p;
  needsDraw = true;
  updateInfo();
  // Maus: Klick malt direkt (der Preis stand schon beim Drüberfahren da).
  // Finger: Tippen wählt nur aus, gemalt wird mit dem Knopf – sonst würde man
  // beim Verschieben aus Versehen Geld ausgeben.
  if (e.pointerType === "mouse") paint();
}

// ---------------------------------------------------------------- Malen

let painting = false;

async function paint() {
  if (!state.selected || painting) return;
  if (!state.me) { toast("Bitte zuerst einloggen"); $("login-handle").focus(); return; }
  const { x, y } = state.selected;
  const shownPrice = state.zonePrice[zoneIndex(x, y)];
  const color = state.color;
  painting = true;
  try {
    const r = await api("/api/paint", {
      method: "POST",
      body: JSON.stringify({ x, y, color_index: color, max_price_cents: shownPrice }),
    });
    // Sofort lokal anzeigen; die Bestätigung per WebSocket folgt ohnehin.
    setPixel(x, y, color);
    const zx = Math.floor(x / state.zoneSize), zy = Math.floor(y / state.zoneSize);
    setZone(zx, zy, r.zone_price_cents, r.heat);
    state.me.balance_cents = r.balance_cents;
    renderMe();
  } catch (err) {
    const d = err.data || {};
    if (d.error === "price_changed") {
      const zx = Math.floor(x / state.zoneSize), zy = Math.floor(y / state.zoneSize);
      state.zonePrice[zy * state.zonesPerSide + zx] = d.current_price_cents;
      toast(`Preis ist gestiegen auf ${fmtCents(d.current_price_cents)} – nochmal malen zum Bestätigen`);
    } else if (d.error === "insufficient_funds") {
      toast(`Nicht genug Guthaben (${fmtCents(d.price_cents)} nötig)`);
    } else if (err.status === 401) {
      state.me = null; renderMe(); toast("Bitte einloggen");
    } else {
      toast(err.message);
    }
  } finally {
    painting = false;
    updateInfo();
  }
}

function updateInfo() {
  const p = state.hover || state.selected;
  const btn = $("paint-btn");
  if (!p || !state.zonePrice) {
    $("info").textContent = "Tippe auf ein Pixel";
    btn.disabled = true;
    btn.textContent = "Malen";
    return;
  }
  const zi = zoneIndex(p.x, p.y);
  const price = state.zonePrice[zi];
  const empty = state.pixels[p.y * state.size + p.x] === 0;
  const frozen = state.zoneFrozen[zi];
  $("info").textContent =
    `(${p.x}, ${p.y}) · ${frozen ? "gesperrt" : fmtCents(price)}` +
    (empty ? " · leer" : " · übermalen erhöht den Preis");
  if (state.selected) {
    const sp = state.zonePrice[zoneIndex(state.selected.x, state.selected.y)];
    btn.textContent = `Malen · ${fmtCents(sp)}`;
    btn.disabled = painting || state.event?.phase !== "running";
  }
}

// ---------------------------------------------------------------- Palette

function buildPalette() {
  const el = $("palette");
  el.innerHTML = "";
  // Index 0 ist "leer" und nicht malbar.
  for (let i = 1; i < state.palette.length; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "swatch" + (i === state.color ? " active" : "");
    b.style.background = state.palette[i];
    b.setAttribute("role", "radio");
    b.setAttribute("aria-label", state.palette[i]);
    b.addEventListener("click", () => {
      state.color = i;
      el.querySelectorAll(".swatch").forEach((s) => s.classList.remove("active"));
      b.classList.add("active");
    });
    el.appendChild(b);
  }
}

// ---------------------------------------------------------------- Account & Aufladen

async function loadMe() {
  try {
    state.me = await api("/api/me");
  } catch (_) {
    state.me = null;
  }
  renderMe();
}

function renderMe() {
  const me = state.me;
  $("login-form").hidden = !!me;
  $("me").hidden = !me;
  if (me) {
    $("me-handle").textContent = me.handle;
    $("me-balance").textContent = fmtCents(me.balance_cents);
  }
}

function setupAccount() {
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/dev/login", {
        method: "POST",
        body: JSON.stringify({ handle: $("login-handle").value.trim() }),
      });
      await loadMe();
    } catch (err) {
      toast(err.message);
    }
  });

  $("topup-btn").addEventListener("click", async () => {
    await loadMe();
    const me = state.me;
    if (!me) return;
    $("topup-cap").textContent =
      `Bisher aufgeladen: ${fmtCents(me.total_topup_cents)} von höchstens ${fmtCents(me.topup_cap_cents)}.`;
    const box = $("topup-packages");
    box.innerHTML = "";
    if (me.topup_packages_cents.length === 0) {
      box.textContent = "Du hast die Obergrenze erreicht.";
    }
    for (const amount of me.topup_packages_cents) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = fmtCents(amount);
      b.addEventListener("click", () => topup(amount));
      box.appendChild(b);
    }
    $("topup-age").checked = false;
    $("topup-dialog").showModal();
  });
}

async function topup(amount) {
  if (!$("topup-age").checked) {
    toast("Bitte zuerst die Altersabfrage bestätigen");
    return;
  }
  try {
    const r = await api("/api/topup", {
      method: "POST",
      body: JSON.stringify({ amount_cents: amount, age_confirmed: true }),
    });
    if (r.completed) {
      $("topup-dialog").close();
      await loadMe();
      toast(`${fmtCents(amount)} gutgeschrieben`);
    } else {
      location.href = r.checkout_url; // echter Anbieter (Phase 4)
    }
  } catch (err) {
    toast(err.message);
  }
}

// ---------------------------------------------------------------- Event-Status

async function loadEvent() {
  try {
    const cfg = await api("/api/config");
    state.event = cfg.event;
    state.clockOffset = new Date(cfg.event.server_now) - Date.now();
  } catch (_) { /* nächster Versuch in einer Minute */ }
}

function renderEvent() {
  const ev = state.event;
  if (!ev) return;
  const now = Date.now() + state.clockOffset;
  const left = (t) => {
    let s = Math.max(0, Math.floor((new Date(t) - now) / 1000));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60); s %= 60;
    return (d ? `${d} T ` : "") + `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };
  const text = {
    before: `Start in ${left(ev.start)}`,
    running: `Ende in ${left(ev.end)}`,
    night_pause: "Nachtpause",
    ended: "Event beendet",
  }[ev.phase];
  $("event-status").textContent = text;
  // Phasenwechsel (Start/Ende) nicht verpassen.
  if ((ev.phase === "before" && now >= new Date(ev.start)) || (ev.phase === "running" && now >= new Date(ev.end))) {
    loadEvent().then(updateInfo);
  }
}

// ---------------------------------------------------------------- Start

async function main() {
  const cfg = await api("/api/config");
  state.size = cfg.canvas_size;
  state.zoneSize = cfg.zone_size;
  state.zonesPerSide = cfg.canvas_size / cfg.zone_size;
  state.palette = cfg.palette;
  state.rgb = cfg.palette.map(hexToRgb);
  state.event = cfg.event;
  state.clockOffset = new Date(cfg.event.server_now) - Date.now();

  state.pixels = new Uint8Array(state.size * state.size);
  const nzz = state.zonesPerSide * state.zonesPerSide;
  state.zonePrice = new Int32Array(nzz);
  state.zoneHeat = new Int32Array(nzz);
  state.zoneFrozen = new Uint8Array(nzz);

  off.width = off.height = state.size;
  offCtx = off.getContext("2d");
  img = offCtx.createImageData(state.size, state.size);
  redrawAllPixels();

  buildPalette();
  setupInput();
  setupAccount();
  $("paint-btn").addEventListener("click", paint);
  $("heat-toggle").addEventListener("change", (e) => { state.showHeat = e.target.checked; needsDraw = true; });

  resize();
  fitView();
  await loadMe();
  connect();
  renderEvent();
  setInterval(renderEvent, 1000);
  setInterval(() => loadEvent().then(updateInfo), 60000);
  requestAnimationFrame(frame);
}

main().catch((e) => {
  console.error(e);
  toast("Laden fehlgeschlagen – bitte Seite neu laden", 10000);
});
