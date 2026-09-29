"use strict";
/*
 * Gemeinsamer Code für alle Seiten: Sprachen, Geldformat, API-Aufrufe,
 * Kopfzeile (Login, Guthaben, Aufladen, Sprachschalter).
 *
 * Sprachen: Alle Texte stehen in static/i18n/<sprache>.json. Im HTML markiert
 *   data-i18n="schlüssel"              → setzt textContent
 *   data-i18n-placeholder="schlüssel"  → setzt placeholder
 *   data-i18n-title="schlüssel"        → setzt title
 * die Stellen, die übersetzt werden. Im JavaScript liefert t("schlüssel", {param})
 * den Text. Neue Sprache = neue JSON-Datei + Eintrag in LANGS.
 */

const LANGS = ["en", "de"];
const DEFAULT_LANG = "en";   // Zielgruppe international, vor allem USA
const $ = (id) => document.getElementById(id);

const I18N = { lang: DEFAULT_LANG, dict: {} };

function storedLang() {
  try {
    const l = localStorage.getItem("lang");
    if (LANGS.includes(l)) return l;
  } catch (_) { /* privates Fenster o. Ä. */ }
  return DEFAULT_LANG;
}

async function loadLang(lang) {
  const res = await fetch(`/static/i18n/${lang}.json`, { cache: "no-cache" });
  I18N.dict = await res.json();
  I18N.lang = lang;
  document.documentElement.lang = lang;
}

function t(key, params = {}) {
  let v = key.split(".").reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), I18N.dict);
  if (typeof v !== "string") return key; // fehlender Schlüssel fällt sofort auf
  return v.replace(/\{(\w+)\}/g, (_, name) => (params[name] !== undefined ? params[name] : `{${name}}`));
}

function applyI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => (el.placeholder = t(el.dataset.i18nPlaceholder)));
  root.querySelectorAll("[data-i18n-title]").forEach((el) => (el.title = t(el.dataset.i18nTitle)));
}

async function setLang(lang) {
  try { localStorage.setItem("lang", lang); } catch (_) {}
  await loadLang(lang);
  applyI18n();
  // Seiten mit dynamischen Texten hören darauf und zeichnen neu.
  document.dispatchEvent(new Event("langchange"));
}

// ---------------------------------------------------------------- Geld & Zeit

// Nur FORMATIERUNG fürs Auge (Cent → "€0.02" / "0,02 €"), keine Preisberechnung.
function fmtMoney(cents) {
  const locale = I18N.lang === "de" ? "de-DE" : "en-US";
  return new Intl.NumberFormat(locale, { style: "currency", currency: "EUR" }).format(cents / 100);
}

function fmtDateTime(iso) {
  const locale = I18N.lang === "de" ? "de-DE" : "en-US";
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

function fmtNumber(n) {
  return new Intl.NumberFormat(I18N.lang === "de" ? "de-DE" : "en-US").format(n);
}

// ---------------------------------------------------------------- API

async function api(path, opts = {}) {
  const headers = opts.body && typeof opts.body === "string" ? { "Content-Type": "application/json" } : {};
  const res = await fetch(path, { credentials: "same-origin", headers, ...opts });
  let data = null;
  try { data = await res.json(); } catch (_) { /* leere Antwort */ }
  if (!res.ok) {
    const err = new Error((data && data.message) || res.statusText);
    err.status = res.status;
    err.data = data || {};
    throw err;
  }
  return data;
}

// Server-Fehlercode → übersetzter Text. Geldbeträge aus der Antwort werden formatiert.
function errorText(err) {
  const d = err.data || {};
  if (!d.error) return t("errors.generic");
  const params = { ...d };
  if (d.price_cents !== undefined) params.price = fmtMoney(d.price_cents);
  if (d.current_price_cents !== undefined) params.price = fmtMoney(d.current_price_cents);
  if (d.cap_cents !== undefined) params.cap = fmtMoney(d.cap_cents);
  const text = t("errors." + d.error, params);
  return text === "errors." + d.error ? d.message || t("errors.generic") : text;
}

let toastTimer = null;
function toast(msg, ms = 2800) {
  let el = $("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), ms);
}

// ---------------------------------------------------------------- Kopfzeile & Account

const Account = { me: null, listeners: [] };

function onAccountChange(fn) { Account.listeners.push(fn); }

async function loadMe() {
  try { Account.me = await api("/api/me"); } catch (_) { Account.me = null; }
  renderHeader();
  Account.listeners.forEach((fn) => fn(Account.me));
  return Account.me;
}

function renderHeader() {
  const me = Account.me;
  if (!$("login-form")) return;
  $("login-form").hidden = !!me;
  $("me").hidden = !me;
  if (me) {
    $("me-handle").textContent = me.handle;
    $("me-balance").textContent = fmtMoney(me.balance_cents);
    const g = $("me-guild");
    if (me.guild) {
      g.hidden = false;
      g.href = `/c/${me.guild.slug}`;
      g.textContent = me.guild.name;
      g.style.setProperty("--guild", me.guild.color);
    } else {
      g.hidden = true;
    }
  }
}

function headerHTML() {
  return `
    <a class="brand" href="/" data-i18n="app.title"></a>
    <a class="navlink" href="/guilds" data-i18n="nav.guilds"></a>
    <span id="event-status" class="muted"></span>
    <span id="conn" class="conn off" data-i18n-title="account.connection" hidden>●</span>
    <button id="lang-btn" type="button" class="ghost" data-i18n="lang.switch" data-i18n-title="lang.switch_title"></button>
    <div id="account">
      <form id="login-form" hidden>
        <input id="login-handle" data-i18n-placeholder="account.name_placeholder" maxlength="24" autocomplete="nickname" required>
        <button type="submit" data-i18n="account.login"></button>
      </form>
      <span id="me" hidden>
        <a id="me-guild" class="guild-chip" hidden></a>
        <span id="me-handle"></span>
        <b id="me-balance"></b>
        <button id="topup-btn" type="button" data-i18n="account.topup"></button>
      </span>
    </div>`;
}

function topupDialogHTML() {
  return `
    <dialog id="topup-dialog">
      <form method="dialog">
        <h2 data-i18n="topup.title"></h2>
        <p class="muted" id="topup-cap"></p>
        <div id="topup-packages"></div>
        <label class="age"><input type="checkbox" id="topup-age"> <span data-i18n="topup.age"></span></label>
        <p class="warn" data-i18n="topup.warn"></p>
        <div class="row"><button value="cancel" type="submit" class="ghost" data-i18n="topup.cancel"></button></div>
      </form>
    </dialog>`;
}

function setupHeader() {
  const header = $("topbar");
  header.innerHTML = headerHTML();
  document.body.insertAdjacentHTML("beforeend", topupDialogHTML());

  $("lang-btn").addEventListener("click", () => setLang(I18N.lang === "en" ? "de" : "en"));

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/dev/login", { method: "POST", body: JSON.stringify({ handle: $("login-handle").value.trim() }) });
      await loadMe();
    } catch (err) {
      toast(errorText(err));
    }
  });

  $("topup-btn").addEventListener("click", openTopup);
  document.addEventListener("langchange", renderHeader);
}

async function openTopup() {
  const me = await loadMe();
  if (!me) return;
  $("topup-cap").textContent = t("topup.so_far", { total: fmtMoney(me.total_topup_cents), cap: fmtMoney(me.topup_cap_cents) });
  const box = $("topup-packages");
  box.innerHTML = "";
  if (me.topup_packages_cents.length === 0) box.textContent = t("topup.cap_reached");
  for (const amount of me.topup_packages_cents) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = fmtMoney(amount);
    b.addEventListener("click", () => topup(amount));
    box.appendChild(b);
  }
  $("topup-age").checked = false;
  $("topup-dialog").showModal();
}

async function topup(amount) {
  if (!$("topup-age").checked) { toast(t("topup.confirm_age_first")); return; }
  try {
    const r = await api("/api/topup", { method: "POST", body: JSON.stringify({ amount_cents: amount, age_confirmed: true }) });
    if (r.completed) {
      $("topup-dialog").close();
      await loadMe();
      toast(t("topup.done", { amount: fmtMoney(amount) }));
    } else {
      location.href = r.checkout_url; // echter Anbieter (Phase 4)
    }
  } catch (err) {
    toast(errorText(err));
  }
}

// ---------------------------------------------------------------- Event-Status (Countdown)

const EventClock = { event: null, offset: 0 };

async function loadEvent() {
  try {
    const cfg = await api("/api/config");
    EventClock.event = cfg.event;
    EventClock.offset = new Date(cfg.event.server_now) - Date.now();
    return cfg;
  } catch (_) { return null; }
}

function renderEvent() {
  const ev = EventClock.event;
  const el = $("event-status");
  if (!ev || !el) return;
  const now = Date.now() + EventClock.offset;
  const left = (target) => {
    let s = Math.max(0, Math.floor((new Date(target) - now) / 1000));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60); s %= 60;
    const pad = (n) => String(n).padStart(2, "0");
    return (d ? t("event.days", { n: d }) + " " : "") + `${pad(h)}:${pad(m)}:${pad(s)}`;
  };
  el.textContent = {
    before: t("event.starts_in", { time: left(ev.start) }),
    running: t("event.ends_in", { time: left(ev.end) }),
    night_pause: t("event.night_pause"),
    ended: t("event.ended"),
  }[ev.phase];
  // Phasenwechsel (Start/Ende) nicht verpassen.
  if ((ev.phase === "before" && now >= new Date(ev.start)) || (ev.phase === "running" && now >= new Date(ev.end))) {
    loadEvent();
  }
}

// ---------------------------------------------------------------- Seitenstart

async function initPage() {
  await loadLang(storedLang());
  if ($("topbar")) setupHeader();
  applyI18n();
  const cfg = await loadEvent();
  renderEvent();
  setInterval(renderEvent, 1000);
  setInterval(loadEvent, 60000);
  document.addEventListener("langchange", renderEvent);
  await loadMe();
  return cfg;
}
