"use strict";
/*
 * Gildenseiten. Ein Skript für zwei Pfade:
 *   /guilds       → Liste aller Gilden + Formular zum Gründen
 *   /c/<slug>     → Gildenseite mit Beitreten (ein Klick) und Folgen
 *
 * Alle Texte, die von Nutzern stammen (Name, Beschreibung), werden per
 * textContent gesetzt, nie per innerHTML – sonst könnte jemand in seine
 * Gildenbeschreibung ein <script> schreiben, das bei allen Besuchern läuft.
 */

const slugFromPath = () => {
  const m = location.pathname.match(/^\/c\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : null;
};

let guild = null;
let config = null;

// ---------------------------------------------------------------- HSV-Farbregler

/*
 * Farbton (0–360°), Sättigung (0–100 %), Helligkeit (0–100 %). Das Ergebnis ist
 * ein Hex-Wert wie #3366FF – nur der wird an den Server geschickt.
 */
function hsvToHex(h, s, v) {
  s /= 100; v /= 100;
  const f = (n) => {
    const k = (n + h / 60) % 6;
    return Math.round(255 * (v - v * s * Math.max(0, Math.min(k, 4 - k, 1))));
  };
  return "#" + [f(5), f(3), f(1)].map((c) => c.toString(16).padStart(2, "0")).join("").toUpperCase();
}

function hexToHsv(hex) {
  const n = parseInt(hex.slice(1), 16);
  const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const max = Math.max(r, g, b), d = max - Math.min(r, g, b);
  let h = 0;
  if (d) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  return { h: Math.round((h + 360) % 360), s: Math.round(max ? (d / max) * 100 : 0), v: Math.round(max * 100) };
}

function makeHsvPicker(container, initialHex) {
  container.innerHTML = `
    <div class="hsv-preview"><span class="hsv-swatch"></span><code class="hsv-hex"></code></div>
    <label><span data-i18n="guild.hue"></span><input type="range" min="0" max="360" class="hsv-h"></label>
    <label><span data-i18n="guild.saturation"></span><input type="range" min="0" max="100" class="hsv-s"></label>
    <label><span data-i18n="guild.brightness"></span><input type="range" min="0" max="100" class="hsv-v"></label>`;
  applyI18n(container);
  const [hIn, sIn, vIn] = ["h", "s", "v"].map((k) => container.querySelector(".hsv-" + k));
  const hsv = hexToHsv(initialHex);
  hIn.value = hsv.h; sIn.value = hsv.s; vIn.value = hsv.v;

  const update = () => {
    const h = +hIn.value, s = +sIn.value, v = +vIn.value;
    const hex = hsvToHex(h, s, v);
    container.querySelector(".hsv-swatch").style.background = hex;
    container.querySelector(".hsv-hex").textContent = hex;
    // Die Regler-Hintergründe zeigen, wohin man die Farbe mit ihnen verschiebt.
    hIn.style.background = `linear-gradient(to right, ${[0, 60, 120, 180, 240, 300, 360].map((x) => hsvToHex(x, s, v)).join(",")})`;
    sIn.style.background = `linear-gradient(to right, ${hsvToHex(h, 0, v)}, ${hsvToHex(h, 100, v)})`;
    vIn.style.background = `linear-gradient(to right, #000, ${hsvToHex(h, s, 100)})`;
  };
  [hIn, sIn, vIn].forEach((el) => el.addEventListener("input", update));
  update();
  return { value: () => hsvToHex(+hIn.value, +sIn.value, +vIn.value) };
}

// ---------------------------------------------------------------- Liste + Gründen

function slugify(name) {
  return name.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
}

async function renderList() {
  $("list-view").hidden = false;
  document.title = `${t("app.title")} – ${t("guild.list_title")}`;
  const { guilds } = await api("/api/guilds");
  const box = $("guild-list");
  box.innerHTML = "";
  if (!guilds.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = t("guild.none_yet");
    box.appendChild(p);
  }
  for (const g of guilds) {
    const a = document.createElement("a");
    a.className = "guild-row";
    a.href = `/c/${encodeURIComponent(g.slug)}`;
    a.style.setProperty("--guild", g.color);
    const name = document.createElement("b");
    name.textContent = g.name;
    const stats = document.createElement("span");
    stats.className = "muted";
    stats.textContent = `${fmtNumber(g.held_pixels)} px · ${fmtNumber(g.members)} ${t("guild.members")} · ${fmtNumber(g.followers)} ${t("guild.followers")}`;
    a.append(name, stats);
    box.appendChild(a);
  }
  renderCreateForm();
}

let createPicker = null;
function renderCreateForm() {
  const me = Account.me;
  $("create-login").hidden = !!me;
  $("create-in-guild").hidden = !(me && me.guild);
  if (me && me.guild) $("create-in-guild").textContent = `${t("guild.your_guild")}: ${me.guild.name}`;
  $("create-form").hidden = !me || !!me.guild;
  const price = fmtMoney(config.guild_creation_cents);
  $("c-cost").textContent = t("guild.cost_note", { price });
  $("c-submit").textContent = t("guild.create", { price });
  $("c-slug-prefix").textContent = `${location.host}/c/`;
  if (!createPicker) createPicker = makeHsvPicker($("c-color"), hsvToHex(Math.floor(Math.random() * 360), 75, 90));
}

function setupCreateForm() {
  let slugTouched = false;
  $("c-slug").addEventListener("input", () => (slugTouched = true));
  $("c-name").addEventListener("input", () => { if (!slugTouched) $("c-slug").value = slugify($("c-name").value); });
  $("create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/guilds", {
        method: "POST",
        body: JSON.stringify({
          name: $("c-name").value, slug: $("c-slug").value,
          description: $("c-desc").value, color_hex: createPicker.value(),
        }),
      });
      location.href = `/c/${encodeURIComponent(r.slug)}`;
    } catch (err) {
      toast(errorText(err));
    }
  });
}

// ---------------------------------------------------------------- Einzelne Gilde

async function loadGuild(slug) {
  try {
    guild = await api(`/api/guilds/${encodeURIComponent(slug)}`);
  } catch (err) {
    if (err.status === 404) { $("not-found").hidden = false; return; }
    throw err;
  }
  renderGuild();
}

let editPicker = null;
function renderGuild() {
  const g = guild;
  $("guild-view").hidden = false;
  document.title = `${g.name} – ${t("app.title")}`;
  document.documentElement.style.setProperty("--guild", g.color);
  $("g-name").textContent = g.name;
  $("g-owner").textContent = t("guild.founded_by", { name: g.owner });
  $("g-desc").textContent = g.description;
  $("g-members").textContent = fmtNumber(g.members);
  $("g-followers").textContent = fmtNumber(g.followers);
  $("g-held").textContent = `${fmtNumber(g.held_pixels)} (${fmtNumber(g.held_percent)} %)`;
  $("g-image").hidden = !g.image_url;
  $("g-image-placeholder").hidden = !!g.image_url;
  if (g.image_url) $("g-image").src = g.image_url;
  $("g-link").value = `${location.origin}/c/${g.slug}`;

  const v = g.viewer; // null = nicht eingeloggt
  $("g-login-hint").hidden = !!v;
  $("g-join").hidden = !v || v.is_member || v.in_other_guild;
  $("g-join-warning").hidden = $("g-join").hidden;
  $("g-joined").hidden = !(v && v.is_member);
  $("g-in-other").hidden = !(v && v.in_other_guild);
  $("g-follow").hidden = !v;
  if (v) $("g-follow").textContent = v.is_following ? t("guild.unfollow") : t("guild.follow");

  $("edit-form").hidden = !(v && v.is_owner);
  if (v && v.is_owner) {
    $("e-desc").value = g.description;
    if (!editPicker) editPicker = makeHsvPicker($("e-color"), g.color);
  }
}

function setupGuildActions(slug) {
  const path = `/api/guilds/${encodeURIComponent(slug)}`;
  $("g-join").addEventListener("click", async () => {
    try {
      await api(`${path}/join`, { method: "POST" });
      await loadMe();
      await loadGuild(slug);
    } catch (err) { toast(errorText(err)); }
  });
  $("g-follow").addEventListener("click", async () => {
    try {
      await api(`${path}/follow`, { method: guild.viewer.is_following ? "DELETE" : "POST" });
      await loadGuild(slug);
    } catch (err) { toast(errorText(err)); }
  });
  $("g-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("g-link").value); } catch (_) { $("g-link").select(); }
    toast(t("guild.copied"), 1200);
  });
  $("edit-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api(path, { method: "PATCH", body: JSON.stringify({ description: $("e-desc").value, color_hex: editPicker.value() }) });
      const file = $("e-image").files[0];
      if (file) {
        // Rohe Datei als Body – der Server prüft und verkleinert sie.
        await api(`${path}/image`, { method: "PUT", body: file, headers: { "Content-Type": file.type } });
        $("e-image").value = "";
      }
      toast(t("guild.saved"));
      await loadGuild(slug);
    } catch (err) { toast(errorText(err)); }
  });
}

// ---------------------------------------------------------------- Start

async function main() {
  config = await initPage();
  const slug = slugFromPath();
  if (slug) {
    setupGuildActions(slug);
    await loadGuild(slug);
    // Nach dem Login im Kopf: Knöpfe (Beitreten/Folgen) neu anzeigen.
    onAccountChange(() => loadGuild(slug));
    document.addEventListener("langchange", () => guild && renderGuild());
  } else {
    setupCreateForm();
    await renderList();
    onAccountChange(renderCreateForm);
    document.addEventListener("langchange", renderList);
  }
}

main().catch((e) => { console.error(e); toast(t("errors.generic"), 8000); });
