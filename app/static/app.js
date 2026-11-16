/* TTS yönetim paneli — bağımlılıksız tek sayfa uygulama. */
"use strict";

/* ====================================================================== yardımcılar */

const $ = (s, root = document) => root.querySelector(s);
// replaceChildren'a null/false/dizi verilebilsin (koşullu parçalar için)
const _replaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...children) { _replaceChildren.call(this); append(this, children); };
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") Object.assign(el.style, v);
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else if (k === "html") el.innerHTML = v;
      else if (k in el && typeof v !== "string" && k !== "list") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
function icon(name, cls = "") {
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", `i ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVGNS, "use");
  use.setAttribute("href", `/static/icons.svg#i-${name}`);
  svg.append(use);
  return svg;
}
const nf = (n, d = 0) => Number(n || 0).toLocaleString("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d });
function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${nf(b, i > 1 ? 1 : 0)} ${u[i]}`;
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const hh = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (hh) return `${hh} sa ${m} dk`;
  if (m) return s ? `${m} dk ${s} sn` : `${m} dk`;
  return `${s} sn`;
}
const clock = (t) => { t = Math.max(0, t || 0); return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`; };
function fmtDate(iso, withTime = true) {
  if (!iso) return "—";
  const d = new Date(iso), now = new Date();
  const hm = d.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (withTime && diff === 0) return `Bugün ${hm}`;
  if (withTime && diff === 1) return `Dün ${hm}`;
  const ds = d.toLocaleDateString("tr-TR", { day: "numeric", month: "short", year: d.getFullYear() !== now.getFullYear() ? "numeric" : undefined });
  return withTime ? `${ds} ${hm}` : ds;
}
function fmtAgo(iso) {
  if (!iso) return "hiç";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "az önce";
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
  return fmtDate(iso);
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

const STATUS = {
  queued: ["Sırada", "run"], running: ["Üretiliyor", "run"], succeeded: ["Hazır", "ok"],
  failed: ["Başarısız", "bad"], canceled: ["İptal", ""], expired: ["Silindi", ""],
};
function statusBadge(st, audioAvailable = true) {
  const key = st === "succeeded" && !audioAvailable ? "expired" : st;
  const [label, cls] = STATUS[key] || [st, ""];
  return h("span", { class: `badge ${cls}` }, label);
}

/* ====================================================================== API */

const S = { me: null, csrf: "", voices: null, styles: [], settings: null, engine: {} };

class ApiError extends Error {
  constructor(status, body) {
    super(body?.error?.message || `İstek başarısız (${status})`);
    this.status = status; this.code = body?.error?.code; this.body = body;
  }
}

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = S.csrf;
  let res;
  try {
    res = await fetch(`/admin/api/${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, { error: { message: "Sunucuya ulaşılamadı. Bağlantınızı kontrol edin." } });
  }
  if (raw) return res;
  const data = (res.headers.get("content-type") || "").includes("json") ? await res.json() : null;
  if (res.status === 401 && path !== "auth/login") {
    const wasIn = !!S.me;
    S.me = null;
    renderLogin(wasIn ? data?.error?.message : undefined);  // ilk açılışta uyarı gösterme
    throw new ApiError(401, data);
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

/* ====================================================================== bildirimler, pencereler */

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}` }, icon(kind === "bad" ? "alert" : "check", "sm"), msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 5000 : 2800);
}
const fail = (e) => toast(e.message || String(e), "bad");

function modal({ title, body, actions = [], wide = false, onClose }) {
  const dlg = h("dialog", { class: "modal" });
  if (wide) dlg.style.width = "min(720px, calc(100vw - 32px))";
  const close = () => { dlg.close(); };
  dlg.addEventListener("close", () => { dlg.remove(); onClose && onClose(); });
  append(dlg, [
    h("div", { class: "modal-head" }, h("h2", null, title),
      h("button", { class: "btn ghost icon sm", type: "button", "aria-label": "Kapat", on: { click: close } }, icon("close", "sm"))),
    h("div", { class: "modal-body" }, body),
    actions.length ? h("div", { class: "modal-foot" }, actions.map((a) => {
      const b = h("button", { class: `btn ${a.kind || ""}`, type: "button" }, a.icon ? icon(a.icon, "sm") : null, a.label);
      b.onclick = async () => {
        if (!a.run) return close();
        busy(b, true);
        try { const keep = await a.run(close); if (keep !== true) close(); } catch (e) { fail(e); } finally { busy(b, false); }
      };
      return b;
    })) : null,
  ]);
  document.body.append(dlg);
  dlg.showModal();
  return { dlg, close };
}

function confirmDialog({ title, text, confirm = "Onayla", danger = false }) {
  return new Promise((resolve) => {
    let ok = false;
    modal({ title, body: h("p", { class: "muted" }, text), onClose: () => resolve(ok),
      actions: [{ label: "Vazgeç" }, { label: confirm, kind: danger ? "danger solid" : "primary", run: () => { ok = true; } }] });
  });
}

function busy(btn, on) {
  if (!btn) return;
  btn.disabled = on;
  btn.classList.toggle("busy", on);
  if (on) { btn._html = [...btn.childNodes]; btn.replaceChildren(h("span", { class: "spin" }), ...btn._html.filter((n) => n.nodeType === 3)); }
  else if (btn._html) { btn.replaceChildren(...btn._html); btn._html = null; }
}

async function copy(text, label = "Kopyalandı") {
  try { await navigator.clipboard.writeText(text); toast(label); }
  catch { modal({ title: "Kopyalayın", body: h("textarea", { class: "input mono", rows: 6, readOnly: true }, text) }); }
}

function codeBlock(text) {
  return h("div", { class: "code" }, h("pre", null, text),
    h("button", { class: "btn sm", type: "button", on: { click: () => copy(text) } }, icon("copy", "sm"), "Kopyala"));
}

/* ====================================================================== form parçaları */

function field(label, input, hint, opts = {}) {
  const id = input.id || (input.id = `f_${Math.random().toString(36).slice(2, 9)}`);
  return h("div", { class: `field ${opts.full ? "full" : ""}` },
    label ? h("label", { for: id }, label) : null, input, hint ? h("div", { class: "hint" }, hint) : null);
}
function textInput(value = "", attrs = {}) { return h("input", { class: "input", value: value ?? "", ...attrs }); }
function numInput(value, attrs = {}) { return h("input", { class: "input", type: "number", inputMode: "numeric", value: value ?? "", ...attrs }); }
function withSuffix(input, suffix) { return h("div", { class: "suffix" }, input, h("span", null, suffix)); }
function selectInput(options, value, attrs = {}) {
  return h("select", { class: "input", ...attrs }, options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)));
}
function toggle(checked, title, sub, onChange) {
  const input = h("input", { type: "checkbox", checked: !!checked, on: { change: (e) => onChange && onChange(e.target.checked) } });
  return { el: h("label", { class: "switch" }, input, h("span", { class: "track" }), h("span", { class: "txt" }, h("b", null, title), sub ? h("small", null, sub) : null)), input };
}
function seg(options, value, onPick) {
  const wrap = h("div", { class: "seg", role: "group" });
  const draw = (v) => wrap.replaceChildren(...options.map(([val, label]) => h("button", {
    type: "button", "aria-pressed": String(val === v), on: { click: () => { draw(val); onPick(val); } } }, label)));
  draw(value);
  return wrap;
}
function slider({ label, min, max, step, value, format, onInput }) {
  const out = h("output", null, format(value));
  const input = h("input", { type: "range", min, max, step, value });
  const paint = () => { input.style.setProperty("--fill", `${((input.value - min) / (max - min)) * 100}%`); out.textContent = format(Number(input.value)); };
  input.addEventListener("input", () => { paint(); onInput(Number(input.value)); });
  paint();
  return { el: h("div", { class: "field" }, h("div", { class: "slider-head" }, h("label", null, label), out), input), input, paint };
}
function intOrNull(v) { if (v === "" || v == null) return null; const n = parseInt(v, 10); return Number.isFinite(n) ? n : null; }

/* ====================================================================== iskelet ve yönlendirme */

const NAV = [
  ["", "Genel bakış", "dashboard"],
  ["projects", "Projeler", "projects"],
  ["requests", "İstek kayıtları", "requests"],
  ["playground", "Deneme alanı", "mic"],
  ["voices", "Sesler", "wave"],
  null,
  ["settings", "Ayarlar", "settings"],
  ["users", "Kullanıcılar", "users"],
  ["audit", "İşlem kaydı", "audit"],
];

let shell, mainEl, enginePill;

function renderShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Ana menü" }, NAV.map((n) => n
    ? h("a", { href: `#/${n[0]}`, "data-route": n[0] }, icon(n[2]), n[1])
    : h("div", { class: "sep" })));
  enginePill = h("div", { class: "engine-pill", title: "Ses motoru" }, h("i", { class: "dot pulse" }), h("span", null, "Ses motoru kontrol ediliyor"));
  const brand = () => h("a", { class: "brand", href: "#/", "aria-label": "TTS ana sayfa" }, h("span", { class: "logo" }, icon("wave", "sm")),
    h("span", { class: "brand-txt" }, h("b", null, "TTS"), h("small", null, "Yönetim paneli")));
  const display = S.me.name || S.me.email || S.me.username;
  const initials = (S.me.name ? S.me.name.split(/\s+/).map((w) => w[0]).join("") : (S.me.email || "?"))
    .replace(/[^\p{L}\p{N}]/gu, "").slice(0, 2).toLocaleUpperCase("tr-TR") || "?";
  const roleLabel = S.me.role === "admin" ? "Yönetici" : "İzleyici";
  const side = h("aside", { class: "side" }, brand(), nav,
    h("div", { class: "side-foot" }, enginePill,
      h("div", { class: "me" },
        h("a", { class: "me-card", href: "#/account", title: "Hesabım" }, h("span", { class: "avatar", "aria-hidden": "true" }, initials),
          h("span", { class: "me-txt" }, h("b", { title: S.me.email }, display), h("small", null, roleLabel))),
        h("button", { class: "btn ghost icon sm", type: "button", title: "Tema", "aria-label": "Temayı değiştir", on: { click: toggleTheme } }, icon("moon", "sm")),
        h("button", { class: "btn ghost icon sm", type: "button", title: "Çıkış", "aria-label": "Çıkış yap", on: { click: logout } }, icon("logout", "sm")))));
  const topbar = h("header", { class: "topbar" },
    h("button", { class: "btn ghost icon", type: "button", "aria-label": "Menüyü aç", on: { click: () => shell.classList.add("nav-open") } }, icon("menu")),
    brand());
  mainEl = h("main", { class: "main", id: "main" });
  shell = h("div", { class: "shell" }, side, h("div", null, topbar, mainEl));
  shell.addEventListener("click", (e) => { if (shell.classList.contains("nav-open") && (e.target === shell || e.target.closest(".nav a"))) shell.classList.remove("nav-open"); });
  $("#app").replaceChildren(shell);
  pollEngine();
}

async function pollEngine() {
  if (!S.me) return;
  try {
    const sys = await api("system");
    S.engine = sys.engine;
    const st = sys.engine.status;
    const [cls, txt] = st === "ok" ? [sys.engine.busy ? "ok pulse" : "ok", sys.engine.busy ? "Ses motoru çalışıyor" : "Ses motoru hazır"]
      : st === "loading" ? ["warn pulse", "Ses motoru hazırlanıyor"] : ["bad", st === "error" ? "Ses motorunda hata" : "Ses motoruna ulaşılamıyor"];
    enginePill.replaceChildren(h("i", { class: `dot ${cls}` }), h("span", null, txt));
    enginePill.title = sys.engine.error || "";
  } catch { /* oturum bitti */ }
  setTimeout(pollEngine, 10000);
}

function toggleTheme() {
  const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  const next = dark ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("tini.theme", next); } catch { /* yok */ }
  window.dispatchEvent(new Event("themechange"));
}

async function logout() {
  try { await api("auth/logout", { method: "POST" }); } catch { /* yok */ }
  S.me = null; location.hash = "#/"; renderLogin();
}

const ROUTES = [
  [/^$/, pageOverview], [/^projects$/, pageProjects], [/^projects\/([\w-]+)(?:\/(\w+))?$/, pageProject],
  [/^requests$/, pageRequests], [/^playground$/, pagePlayground], [/^voices$/, pageVoices],
  [/^settings(?:\/(\w+))?$/, pageSettings], [/^users$/, pageUsers], [/^audit$/, pageAudit], [/^account$/, pageAccount],
];

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, qs] = raw.split("?");
  return { path, query: new URLSearchParams(qs || "") };
}

let routeToken = 0;
async function route() {
  if (!S.me) return;
  const { path, query } = parseHash();
  const top = path.split("/")[0];
  document.querySelectorAll(".nav a").forEach((a) => { if (a.dataset.route !== top) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); });
  closeSheet();
  shell?.classList.remove("nav-open");
  const token = ++routeToken;
  for (const [re, fn] of ROUTES) {
    const m = path.match(re);
    if (m) {
      mainEl.replaceChildren(h("div", { class: "page" }, h("div", { class: "skeleton", style: { width: "220px", height: "28px" } }), h("div", { class: "card card-pad stack" }, [1, 2, 3].map(() => h("div", { class: "skeleton" })))));
      try {
        const page = await fn(m.slice(1), query);
        if (token === routeToken && page) { mainEl.replaceChildren(page); mainEl.scrollTop = 0; window.scrollTo(0, 0); }
      } catch (e) {
        if (e.status === 401) return;
        if (token === routeToken) mainEl.replaceChildren(h("div", { class: "page" }, emptyState("alert", "Sayfa yüklenemedi", e.message, h("button", { class: "btn", on: { click: route } }, icon("refresh", "sm"), "Tekrar dene"))));
      }
      return;
    }
  }
  mainEl.replaceChildren(h("div", { class: "page" }, emptyState("search", "Sayfa bulunamadı", "Adres yanlış olabilir.", h("a", { class: "btn", href: "#/" }, "Genel bakışa dön"))));
}

function pageHead(title, lead, ...actions) {
  return h("div", { class: "page-head" }, h("div", null, h("h1", null, title), lead ? h("p", { class: "lead" }, lead) : null), actions.length ? h("div", { class: "row" }, actions) : null);
}
function emptyState(ic, title, text, action) {
  return h("div", { class: "empty" }, h("div", { class: "ic" }, icon(ic, "lg")), h("h3", null, title), text ? h("p", null, text) : null, action || null);
}
function card(title, sub, body, actions) {
  return h("section", { class: "card" },
    title ? h("div", { class: "card-head" }, h("div", null, h("h2", null, title), sub ? h("p", null, sub) : null), actions || null) : null,
    h("div", { class: "card-body" }, body));
}
const isAdmin = () => S.me?.role === "admin";

async function loadVoices(force = false) {
  if (!S.voices || force) { const r = await api("voices"); S.voices = r.voices; S.styles = r.styles; }
  return S.voices;
}

/* ====================================================================== giriş */

function renderLogin(message) {
  const user = textInput("", { type: "email", autocomplete: "email", inputMode: "email", required: true, autofocus: true, placeholder: "ornek@alanadiniz.com" });
  const pass = textInput("", { type: "password", autocomplete: "current-password", required: true });
  const err = h("div", { class: "banner", hidden: !message, role: "alert" }, icon("alert", "sm"), h("p", null, message || ""));
  const btn = h("button", { class: "btn primary", type: "submit" }, "Giriş yap");
  const form = h("form", { class: "card form", on: { submit: async (e) => {
    e.preventDefault(); busy(btn, true); err.hidden = true;
    try {
      const r = await api("auth/login", { method: "POST", body: { email: user.value.trim(), password: pass.value } });
      S.me = r.user; S.csrf = r.csrf; start();
    } catch (ex) { err.lastChild.textContent = ex.message; err.hidden = false; pass.select(); }
    finally { busy(btn, false); }
  } } },
    h("div", null, h("h1", null, "Giriş yapın"), h("p", { class: "muted small" }, "Yönetim paneline erişmek için hesabınızla giriş yapın.")),
    err, field("E-posta", user), field("Şifre", pass), btn);
  $("#app").replaceChildren(h("div", { class: "auth" }, h("div", { class: "auth-card" },
    h("span", { class: "brand" }, h("span", { class: "logo" }, icon("wave", "sm")), h("span", { class: "brand-txt" }, h("b", null, "TTS"), h("small", null, "Metinden sese"))), form)));
  user.focus();
}

/* ====================================================================== Genel bakış */

function barChart(series, key = "requests", failKey = "failed") {
  const W = 720, H = 190, pad = { l: 34, r: 8, t: 10, b: 24 };
  const max = Math.max(1, ...series.map((d) => d[key]));
  const nice = Math.pow(10, Math.floor(Math.log10(max))) * Math.ceil(max / Math.pow(10, Math.floor(Math.log10(max))));
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart"); svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Günlük istek sayısı grafiği");
  const el = (tag, attrs, text) => { const e = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; svg.append(e); return e; };
  const ih = H - pad.t - pad.b, iw = W - pad.l - pad.r;
  [0, .5, 1].forEach((f) => {
    const y = pad.t + ih * (1 - f);
    el("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid" });
    const v = nice * f;
    el("text", { x: pad.l - 6, y: y + 4, "text-anchor": "end", class: "axis" }, nf(v, Number.isInteger(v) ? 0 : 1));
  });
  const bw = iw / series.length;
  series.forEach((d, i) => {
    const x = pad.l + i * bw + bw * 0.18, w = bw * 0.64;
    const total = d[key], f = d[failKey] || 0;
    const th = (total / nice) * ih, fh = (f / nice) * ih;
    const ok = el("rect", { x, y: pad.t + ih - th, width: w, height: Math.max(th - fh, total ? 1 : 0), rx: 3, class: "bar" });
    if (fh) el("rect", { x, y: pad.t + ih - fh, width: w, height: fh, rx: 2, class: "bar fail" });
    const tt = document.createElementNS(SVGNS, "title");
    tt.textContent = `${new Date(d.date).toLocaleDateString("tr-TR", { day: "numeric", month: "long" })}: ${nf(total)} istek, ${nf(d.chars)} karakter, ${fmtDur(d.audio_seconds)} ses${f ? `, ${f} hata` : ""}`;
    ok.append(tt);
    if (series.length <= 16 || i % Math.ceil(series.length / 10) === 0)
      el("text", { x: x + w / 2, y: H - 6, "text-anchor": "middle", class: "axis" }, new Date(d.date).toLocaleDateString("tr-TR", { day: "numeric", month: "short" }).replace(".", ""));
  });
  return svg;
}

function kpis(items) {
  return h("div", { class: "card kpis" }, items.map(([label, value, sub, ic]) => h("div", { class: "kpi" },
    h("span", { class: "label" }, ic ? icon(ic, "sm") : null, label), h("span", { class: "value" }, value), sub ? h("span", { class: "sub" }, sub) : null)));
}

function genTable(items, { project = true } = {}) {
  if (!items.length) return emptyState("requests", "Henüz istek yok", "API'ye ya da deneme alanına gelen istekler burada görünür.");
  return h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
    h("thead", null, h("tr", null, h("th", null, "Zaman"), project ? h("th", null, "Proje") : null, h("th", null, "Metin"), h("th", null, "Ses"), h("th", { class: "num" }, "Süre"), h("th", null, "Durum"))),
    h("tbody", null, items.map((g) => h("tr", { class: "click", tabIndex: 0, on: { click: () => openGeneration(g.id), keydown: (e) => { if (e.key === "Enter") openGeneration(g.id); } } },
      h("td", { "data-label": "Zaman" }, fmtDate(g.created_at)),
      project ? h("td", { "data-label": "Proje" }, g.project_name || "—") : null,
      h("td", { "data-label": "Metin", class: "excerpt" }, g.excerpt || h("span", { class: "faint" }, `${nf(g.chars)} karakter (metin saklanmadı)`)),
      h("td", { "data-label": "Ses" }, voiceName(g.voice)),
      h("td", { "data-label": "Süre", class: "num" }, g.duration ? fmtDur(g.duration) : "—"),
      h("td", { "data-label": "Durum" }, statusBadge(g.status, g.audio_available)))))));
}
function voiceName(id) { return S.voices?.find((v) => v.id === id)?.name || id; }

async function pageOverview(_, query) {
  await loadVoices().catch(() => {});
  const days = Number(query.get("days") || 14);
  const o = await api(`overview?days=${days}`);
  const errRate = o.period.requests ? (o.period.failed / o.period.requests) * 100 : 0;
  const disk = o.storage.disk;
  const page = h("div", { class: "page" },
    pageHead("Genel bakış", "Bugünkü kullanım, kuyruk durumu ve son istekler.",
      seg([[7, "7 gün"], [14, "14 gün"], [30, "30 gün"]], days, (v) => { location.hash = `#/?days=${v}`; })),
    o.maintenance ? h("div", { class: "banner" }, icon("alert", "sm"), h("p", null, "Bakım modu açık: API yeni istekleri kabul etmiyor. ", h("a", { href: "#/settings/maintenance" }, "Ayarlardan kapatın"))) : null,
    kpis([
      ["Bugün istek", nf(o.today.requests), `${nf(o.today.failed)} hata`, "requests"],
      ["Bugün karakter", nf(o.today.chars), null, "text"],
      ["Bugün üretilen ses", fmtDur(o.today.audio_seconds), o.today.avg_processing_ms ? `ortalama ${nf(o.today.avg_processing_ms / 1000, 1)} sn işlem` : null, "volume"],
      ["Kuyruk", `${nf(o.queue.running)} çalışıyor`, `${nf(o.queue.queued)} sırada`, "queue"],
    ]),
    h("div", { class: "grid2" },
      card(`Son ${days} gün`, `${nf(o.period.requests)} istek, ${nf(o.period.chars)} karakter, ${fmtDur(o.period.audio_seconds)} ses. Hata oranı %${nf(errRate, 1)}.`,
        h("div", { class: "stack" }, barChart(o.series), h("div", { class: "legend" }, h("span", null, h("i"), "Başarılı"), h("span", null, h("i", { class: "fail" }), "Hatalı")))),
      h("div", { class: "stack" },
        card("En çok kullanan projeler", null, o.top_projects.length ? h("div", { class: "stack" }, o.top_projects.map((p) => {
          const max = Math.max(1, ...o.top_projects.map((x) => x.n));
          return h("a", { href: `#/projects/${p.id}`, class: "stack", style: { gap: "6px", color: "inherit" } },
            h("div", { class: "row" }, h("span", { class: "cell-title" }, p.name), h("span", { class: "spacer" }), h("span", { class: "muted small" }, `${nf(p.n)} istek`)),
            h("div", { class: "meter" }, h("i", { style: { width: `${(p.n / max) * 100}%` } })));
        })) : emptyState("projects", "Henüz proje yok", null, isAdmin() ? h("a", { class: "btn primary", href: "#/projects?new=1" }, icon("add", "sm"), "Proje oluştur") : null)),
        card("Depolama", null, h("dl", { class: "kv" },
          h("dt", null, "Saklanan ses"), h("dd", null, `${nf(o.storage.files)} dosya, ${fmtBytes(o.storage.bytes)}`),
          disk ? [h("dt", null, "Diskte boş alan"), h("dd", null, `${fmtBytes(disk.free)} / ${fmtBytes(disk.total)}`)] : null,
          h("dt", null, "Ses motoru"), h("dd", null, o.engine.status === "ok" ? `Hazır, sesler: ${(o.engine.voices || []).map(voiceName).join(", ")}` : (o.engine.error || o.engine.status)))))),
    card("Son istekler", null, genTable(o.recent), h("a", { class: "btn sm", href: "#/requests" }, "Tümünü gör", icon("right", "sm"))));
  return page;
}

/* ====================================================================== Projeler */

async function pageProjects(_, query) {
  const { projects } = await api("projects");
  const list = projects.filter((p) => !p.is_system);
  const page = h("div", { class: "page" },
    pageHead("Projeler", "Her proje kendi API anahtarlarına, sınırlarına ve webhook adresine sahiptir. CRM gibi her uygulama için ayrı proje açın.",
      isAdmin() ? h("button", { class: "btn primary", type: "button", on: { click: () => newProject() } }, icon("add", "sm"), "Yeni proje") : null),
    list.length ? h("section", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
      h("thead", null, h("tr", null, h("th", null, "Proje"), h("th", null, "Durum"), h("th", null, "Anahtar"), h("th", null, "Bugün"), h("th", null, "Bu ay"), h("th", null, "Dakikalık sınır"))),
      h("tbody", null, list.map((p) => h("tr", { class: "click", tabIndex: 0, on: { click: () => (location.hash = `#/projects/${p.id}`), keydown: (e) => { if (e.key === "Enter") location.hash = `#/projects/${p.id}`; } } },
        h("td", { "data-label": "Proje" }, h("div", { class: "cell-title" }, p.name), p.description ? h("div", { class: "cell-sub" }, p.description) : null),
        h("td", { "data-label": "Durum" }, p.status === "active" ? h("span", { class: "badge ok" }, "Etkin") : h("span", { class: "badge warn" }, "Durduruldu")),
        h("td", { "data-label": "Anahtar" }, `${p.keys_active} etkin`),
        h("td", { "data-label": "Bugün" }, quotaText(p.usage.day.chars, p.daily_char_quota, p.usage.day.requests)),
        h("td", { "data-label": "Bu ay" }, quotaText(p.usage.month.chars, p.monthly_char_quota, p.usage.month.requests)),
        h("td", { "data-label": "Sınır" }, `${nf(p.rate_limit_rpm)} / dk`))))))) :
      h("section", { class: "card" }, emptyState("projects", "İlk projenizi oluşturun", "Bir proje oluşturup API anahtarı aldığınızda CRM'iniz ses üretmeye başlayabilir.",
        isAdmin() ? h("button", { class: "btn primary", on: { click: () => newProject() } }, icon("add", "sm"), "Yeni proje") : null)));
  if (query.get("new") && isAdmin()) setTimeout(newProject, 50);
  return page;
}

function quotaText(used, quota, requests) {
  const main = `${nf(used)} karakter`;
  if (!quota) return h("div", null, h("div", null, main), h("div", { class: "cell-sub" }, `${nf(requests)} istek`));
  const pct = Math.min(100, (used / quota) * 100);
  return h("div", { class: "stack", style: { gap: "4px", minWidth: "140px" } }, h("div", { class: "small" }, `${main} / ${nf(quota)}`),
    h("div", { class: `meter ${pct > 90 ? "bad" : pct > 70 ? "warn" : ""}` }, h("i", { style: { width: `${pct}%` } })));
}

function newProject() {
  const name = textInput("", { maxLength: 80, placeholder: "Örn. Sosyal medya CRM" });
  const desc = textInput("", { maxLength: 500, placeholder: "İsteğe bağlı" });
  modal({ title: "Yeni proje", body: h("div", { class: "form" }, field("Proje adı", name), field("Açıklama", desc, "Yalnız panelde görünür.")),
    actions: [{ label: "Vazgeç" }, { label: "Oluştur", kind: "primary", run: async () => {
      if (!name.value.trim()) throw new Error("Proje adı gerekli.");
      const p = await api("projects", { method: "POST", body: { name: name.value.trim(), description: desc.value.trim() } });
      toast("Proje oluşturuldu"); location.hash = `#/projects/${p.id}/keys`;
    } }] });
  setTimeout(() => name.focus(), 50);
}

const PROJECT_TABS = [["general", "Genel", "settings"], ["keys", "API anahtarları", "key"], ["limits", "Sınırlar", "shield"],
  ["webhook", "Webhook", "webhook"], ["security", "Erişim", "lock"], ["usage", "Kullanım", "chart"], ["integration", "Entegrasyon", "code"]];

async function pageProject([pid, tab = "general"]) {
  const [p] = await Promise.all([api(`projects/${pid}`), loadVoices().catch(() => [])]);
  const content = h("div", { class: "stack" });
  const page = h("div", { class: "page" },
    h("div", null, h("a", { class: "crumb", href: "#/projects" }, icon("left", "sm"), "Projeler"),
      pageHead(p.name, p.is_system ? "Panelin deneme alanının kullandığı sistem projesi." : (p.description || null),
        p.status === "active" ? h("span", { class: "badge ok" }, "Etkin") : h("span", { class: "badge warn" }, "Durduruldu"))),
    h("nav", { class: "tabs", "aria-label": "Proje bölümleri" }, PROJECT_TABS.filter(([id]) => !(p.is_system && ["keys", "webhook", "security", "integration"].includes(id)))
      .map(([id, label, ic]) => h("a", { href: `#/projects/${pid}/${id}`, "aria-current": id === tab ? "page" : null }, icon(ic, "sm"), label))),
    content);
  const render = { general: projectGeneral, keys: projectKeys, limits: projectLimits, webhook: projectWebhook,
    security: projectSecurity, usage: projectUsage, integration: projectIntegration }[tab] || projectGeneral;
  content.append(await render(p));
  return page;
}

async function saveProject(p, body, btn, msg = "Kaydedildi") {
  busy(btn, true);
  try { const r = await api(`projects/${p.id}`, { method: "PATCH", body }); Object.assign(p, r); toast(msg); return r; }
  catch (e) { fail(e); throw e; } finally { busy(btn, false); }
}

function saveBar(onSave) {
  const btn = h("button", { class: "btn primary", type: "button", disabled: !isAdmin() }, icon("check", "sm"), "Değişiklikleri kaydet");
  btn.onclick = () => onSave(btn).catch(() => {});
  return h("div", { class: "row" }, h("span", { class: "spacer" }), btn);
}

async function projectGeneral(p) {
  const { presets } = await api("presets");
  const defPreset = selectInput([["", "Yok (istekte gönderilirse uygulanır)"], ...presets.map((x) => [x.id, `${x.name} (${x.id})`])], p.default_preset || "");
  const name = textInput(p.name, { maxLength: 80 });
  const desc = textInput(p.description, { maxLength: 500 });
  const voices = S.voices || [];
  const defVoice = selectInput([["", `Sistem varsayılanı (${voiceName(voices.find((v) => v.default)?.id || "ema")})`], ...voices.filter((v) => v.enabled).map((v) => [v.id, v.name])], p.default_voice || "");
  const allowed = new Set(p.allowed_voices);
  const checks = h("div", { class: "checks" }, voices.map((v) => h("label", { class: "check" },
    h("input", { type: "checkbox", value: v.id, checked: allowed.has(v.id) }), v.name)));
  const status = toggle(p.status === "active", "Proje etkin", "Kapalıyken bu projenin anahtarlarıyla gelen istekler reddedilir.");
  const storeText = toggle(!!p.store_text, "Metinleri sakla", "Kapalıysa metin yalnız ses üretilene kadar bellekte tutulur, veritabanına yazılmaz.");
  const useGlobal = p.retention_days === null;
  const retGlobal = toggle(useGlobal, "Sistem saklama süresini kullan", null, (v) => { retDays.disabled = v; });
  const retDays = numInput(p.retention_days ?? "", { min: 0, max: 3650, disabled: useGlobal, placeholder: "7" });
  const form = h("div", { class: "form" },
    h("div", { class: "form-grid" }, field("Proje adı", name), field("Açıklama", desc)),
    p.is_system ? null : status.el,
    h("div", { class: "divider" }),
    h("div", { class: "form-grid" },
      field("Varsayılan ses", defVoice, "İstekte ses belirtilmezse bu kullanılır. İleride modeli değiştirmek için yalnız burayı değiştirmeniz yeter."),
      field("İzin verilen sesler", checks, "Hiçbiri seçili değilse etkin tüm sesler kullanılabilir."),
      field("Varsayılan hazır ayar", defPreset, "İstekte preset gönderilmezse bu uygulanır. İstekteki açık alanlar her zaman önceliklidir.")),
    h("div", { class: "divider" }),
    storeText.el,
    h("div", { class: "form-grid" }, h("div", { class: "field" }, retGlobal.el),
      field("Bu projede saklama süresi", withSuffix(retDays, "gün"), "Ses dosyaları bu süre sonunda silinir. 0 = bu projede otomatik silme yok.")),
    saveBar(async (btn) => {
      const body = { name: name.value.trim(), description: desc.value.trim(), default_voice: defVoice.value || null,
        allowed_voices: [...checks.querySelectorAll("input:checked")].map((i) => i.value), store_text: storeText.input.checked,
        default_preset: defPreset.value || null };
      if (!p.is_system) body.status = status.input.checked ? "active" : "paused";
      if (retGlobal.input.checked) body.use_global_retention = true; else body.retention_days = intOrNull(retDays.value) ?? 0;
      await saveProject(p, body, btn);
    }));
  const danger = !p.is_system && isAdmin() ? card("Projeyi sil", "Projenin tüm anahtarları, istek kayıtları ve ses dosyaları kalıcı olarak silinir.",
    h("button", { class: "btn danger", on: { click: async () => {
      if (!(await confirmDialog({ title: `“${p.name}” silinsin mi?`, text: "Bu işlem geri alınamaz. Projeye bağlı CRM entegrasyonları çalışmayı durdurur.", confirm: "Kalıcı olarak sil", danger: true }))) return;
      try { await api(`projects/${p.id}`, { method: "DELETE" }); toast("Proje silindi"); location.hash = "#/projects"; } catch (e) { fail(e); }
    } } }, icon("trash", "sm"), "Projeyi sil")) : null;
  return h("div", { class: "stack" }, card("Proje ayarları", null, form), danger);
}

async function projectKeys(p) {
  const { keys } = await api(`projects/${p.id}/keys`);
  const wrap = h("div", { class: "stack" });
  const draw = (ks) => {
    wrap.replaceChildren(card("API anahtarları", "Anahtarın tamamı yalnız oluşturulduğu anda gösterilir. Kaybederseniz yenisini oluşturup eskisini iptal edin.",
      ks.length ? h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
        h("thead", null, h("tr", null, h("th", null, "Ad"), h("th", null, "Anahtar"), h("th", null, "Yetkiler"), h("th", null, "Son kullanım"), h("th", null, "Durum"), h("th"))),
        h("tbody", null, ks.map((k) => h("tr", null,
          h("td", { "data-label": "Ad" }, h("div", { class: "cell-title" }, k.name), h("div", { class: "cell-sub" }, `${fmtDate(k.created_at, false)} tarihinde ${k.created_by || ""} oluşturdu`)),
          h("td", { "data-label": "Anahtar" }, h("code", null, k.display)),
          h("td", { "data-label": "Yetkiler", class: "small" }, k.scopes.map(scopeLabel).join(", ")),
          h("td", { "data-label": "Son kullanım", class: "small" }, k.last_used_at ? `${fmtAgo(k.last_used_at)}${k.last_used_ip ? `, ${k.last_used_ip}` : ""}` : "Hiç kullanılmadı", h("div", { class: "cell-sub" }, `${nf(k.use_count)} istek`)),
          h("td", { "data-label": "Durum" }, k.state === "active" ? h("span", { class: "badge ok" }, k.expires_at ? `${fmtDate(k.expires_at, false)} tarihine kadar` : "Etkin") : h("span", { class: "badge bad" }, k.state === "revoked" ? "İptal edildi" : "Süresi doldu")),
          h("td", { class: "actions" }, !isAdmin() ? null : k.state === "active"
            ? h("button", { class: "btn sm danger", on: { click: () => revokeKey(k) } }, icon("ban", "sm"), "İptal et")
            : h("button", { class: "btn sm ghost", title: "Kaydı sil", on: { click: async () => { await api(`keys/${k.id}`, { method: "DELETE" }).catch(fail); reload(); } } }, icon("trash", "sm")))))))) :
        emptyState("key", "Henüz anahtar yok", "CRM'inizin bu projeye istek atabilmesi için bir anahtar oluşturun."),
      isAdmin() ? h("button", { class: "btn primary", on: { click: () => newKey(p, reload) } }, icon("add", "sm"), "Yeni anahtar") : null));
  };
  const reload = async () => draw((await api(`projects/${p.id}/keys`)).keys);
  async function revokeKey(k) {
    if (!(await confirmDialog({ title: "Anahtar iptal edilsin mi?", text: `“${k.name}” (${k.display}) hemen geçersiz olur. Bu anahtarı kullanan uygulamalar 401 hatası almaya başlar.`, confirm: "İptal et", danger: true }))) return;
    try { await api(`keys/${k.id}/revoke`, { method: "POST" }); toast("Anahtar iptal edildi"); reload(); } catch (e) { fail(e); }
  }
  draw(keys);
  return wrap;
}
const scopeLabel = (s) => ({ "speech:create": "Ses üret", "speech:read": "Oku", "speech:delete": "Sil" })[s] || s;

function newKey(p, done) {
  const name = textInput("", { maxLength: 80, placeholder: "Örn. CRM canlı sunucu" });
  const scopes = h("div", { class: "checks" }, [["speech:create", "Ses üret"], ["speech:read", "İşleri ve sesleri oku"], ["speech:delete", "Ses dosyası sil"]]
    .map(([v, l]) => h("label", { class: "check" }, h("input", { type: "checkbox", value: v, checked: true }), l)));
  const exp = selectInput([["", "Süresiz"], ["30", "30 gün"], ["90", "90 gün"], ["180", "6 ay"], ["365", "1 yıl"]], "");
  modal({ title: "Yeni API anahtarı", body: h("div", { class: "form" }, field("Anahtar adı", name, "Anahtarın nerede kullanıldığını hatırlamanızı sağlar."), field("Yetkiler", scopes), field("Geçerlilik", exp)),
    actions: [{ label: "Vazgeç" }, { label: "Oluştur", kind: "primary", run: async () => {
      if (!name.value.trim()) throw new Error("Anahtar adı gerekli.");
      const k = await api(`projects/${p.id}/keys`, { method: "POST", body: { name: name.value.trim(), scopes: [...scopes.querySelectorAll("input:checked")].map((i) => i.value), expires_in_days: intOrNull(exp.value) } });
      done(); showKey(k);
    } }] });
  setTimeout(() => name.focus(), 50);
}

function showKey(k) {
  modal({ title: "Anahtarınız hazır", body: h("div", { class: "stack" },
    h("div", { class: "banner" }, icon("alert", "sm"), h("p", null, "Bu anahtarı şimdi kopyalayıp güvenli bir yere kaydedin. Pencereyi kapattıktan sonra bir daha gösterilmez.")),
    h("div", { class: "secret" }, h("code", null, k.key), h("button", { class: "btn sm", on: { click: () => copy(k.key, "Anahtar kopyalandı") } }, icon("copy", "sm"), "Kopyala")),
    h("p", { class: "muted small" }, "İsteklerde şu başlıkla gönderin: ", h("code", null, "Authorization: Bearer <anahtar>"))),
    actions: [{ label: "Kaydettim, kapat", kind: "primary" }] });
}

async function projectLimits(p) {
  const rpm = numInput(p.rate_limit_rpm, { min: 1 });
  const conc = numInput(p.max_concurrency, { min: 1, max: 100 });
  const maxc = numInput(p.max_chars_per_request, { min: 10 });
  const daily = numInput(p.daily_char_quota || "", { min: 0, placeholder: "Sınırsız" });
  const monthly = numInput(p.monthly_char_quota || "", { min: 0, placeholder: "Sınırsız" });
  const u = p.usage;
  return h("div", { class: "stack" },
    card("Kullanım", null, h("div", { class: "grid2" },
      h("div", { class: "stack" }, h("h3", null, "Bugün"), quotaText(u.day.chars, p.daily_char_quota, u.day.requests), h("p", { class: "small muted" }, `${nf(u.day.requests)} istek, ${fmtDur(u.day.audio_seconds)} ses`)),
      h("div", { class: "stack" }, h("h3", null, "Bu ay"), quotaText(u.month.chars, p.monthly_char_quota, u.month.requests), h("p", { class: "small muted" }, `${nf(u.month.requests)} istek, ${fmtDur(u.month.audio_seconds)} ses`)))),
    card("Sınırlar", "Sınır aşıldığında API 429 ya da 413 döner; yanıtta ne zaman tekrar denenebileceği yazar.", h("div", { class: "form" },
      h("div", { class: "form-grid" },
        field("Dakikalık istek sınırı", withSuffix(rpm, "istek / dk"), "Bu projedeki tüm anahtarların toplamı."),
        field("Aynı anda çalışan iş", withSuffix(conc, "iş"), "Fazlası sırada bekler; diğer projelerin önünü kesmez."),
        field("Tek istekte en fazla", withSuffix(maxc, "karakter"), "Yaklaşık 1.000 karakter ≈ 1 dakika ses."),
        h("div"),
        field("Günlük karakter kotası", withSuffix(daily, "karakter"), "Boş ya da 0 = sınırsız. Gece yarısı sıfırlanır."),
        field("Aylık karakter kotası", withSuffix(monthly, "karakter"), "Boş ya da 0 = sınırsız. Ayın ilk günü sıfırlanır.")),
      saveBar((btn) => saveProject(p, { rate_limit_rpm: intOrNull(rpm.value) || 1, max_concurrency: intOrNull(conc.value) || 1,
        max_chars_per_request: intOrNull(maxc.value) || 10, daily_char_quota: intOrNull(daily.value) || 0, monthly_char_quota: intOrNull(monthly.value) || 0 }, btn)))));
}

async function projectWebhook(p) {
  const url = textInput(p.webhook_url, { type: "url", placeholder: "https://crm.alanadiniz.com/webhooks/tini" });
  const secretBox = h("div", { class: "secret" }, h("code", null, "whsec_••••••••••••••••"),
    isAdmin() ? h("button", { class: "btn sm", on: { click: async () => { const r = await api(`projects/${p.id}/webhook/secret`).catch(fail); if (r) { secretBox.firstChild.textContent = r.webhook_secret; } } } }, icon("eye", "sm"), "Göster") : null,
    isAdmin() ? h("button", { class: "btn sm", on: { click: async () => {
      if (!(await confirmDialog({ title: "İmza anahtarı yenilensin mi?", text: "Eski anahtarla imza doğrulayan sistemler yeni anahtarı alana kadar webhook'ları reddeder.", confirm: "Yenile" }))) return;
      const r = await api(`projects/${p.id}/webhook/rotate`, { method: "POST" }).catch(fail); if (r) { secretBox.firstChild.textContent = r.webhook_secret; toast("İmza anahtarı yenilendi"); }
    } } }, icon("refresh", "sm"), "Yenile") : null);
  const list = h("div");
  const loadDeliveries = async () => {
    const { deliveries } = await api(`projects/${p.id}/webhook/deliveries`);
    list.replaceChildren(deliveries.length ? h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
      h("thead", null, h("tr", null, h("th", null, "Zaman"), h("th", null, "Olay"), h("th", null, "Sonuç"), h("th", null, "Deneme"), h("th"))),
      h("tbody", null, deliveries.map((d) => h("tr", null,
        h("td", { "data-label": "Zaman" }, fmtDate(d.created_at)),
        h("td", { "data-label": "Olay" }, h("code", null, d.event)),
        h("td", { "data-label": "Sonuç" }, d.status === "delivered" ? h("span", { class: "badge ok" }, `Teslim edildi (${d.last_status_code})`) : d.status === "pending" ? h("span", { class: "badge run" }, d.attempts ? `Yeniden denenecek (${d.last_status_code || "bağlantı yok"})` : "Bekliyor") : h("span", { class: "badge bad", title: d.last_error || "" }, `Başarısız${d.last_status_code ? ` (${d.last_status_code})` : ""}`),
          d.last_error && d.status !== "delivered" ? h("div", { class: "cell-sub" }, d.last_error.slice(0, 120)) : null),
        h("td", { "data-label": "Deneme" }, String(d.attempts)),
        h("td", { class: "actions" }, d.status === "failed" && d.generation_id && isAdmin() ? h("button", { class: "btn sm", on: { click: async () => { await api(`webhook/deliveries/${d.id}/retry`, { method: "POST" }).catch(fail); toast("Yeniden gönderilecek"); setTimeout(loadDeliveries, 2500); } } }, icon("retry", "sm"), "Tekrar gönder") : null)))))) :
      emptyState("webhook", "Henüz gönderim yok", "Bir iş bittiğinde gönderilen webhook'lar burada listelenir."));
  };
  await loadDeliveries();
  return h("div", { class: "stack" },
    card("Webhook", "Ses hazır olduğunda (ya da üretilemediğinde) bu adrese imzalı bir POST isteği gönderilir. Teslim edilemezse artan aralıklarla yeniden denenir.", h("div", { class: "form" },
      field("Webhook adresi", url, "Boş bırakırsanız webhook gönderilmez. İstekte webhook_url verilirse o kullanılır."),
      field("İmza anahtarı", secretBox, "Gelen isteğin bizden geldiğini doğrulamak için kullanın. Ayrıntılar Entegrasyon sekmesinde."),
      h("div", { class: "row" },
        isAdmin() ? h("button", { class: "btn", on: { click: async (e) => {
          const b = e.currentTarget; busy(b, true);
          try { const r = await api(`projects/${p.id}/webhook/test`, { method: "POST" }); r.status === "delivered" ? toast(`Test teslim edildi (${r.last_status_code})`) : toast(`Teslim edilemedi: ${r.last_error || r.last_status_code}`, "bad"); loadDeliveries(); }
          catch (ex) { fail(ex); } finally { busy(b, false); }
        } } }, icon("send", "sm"), "Test gönder") : null,
        h("span", { class: "spacer" }),
        isAdmin() ? h("button", { class: "btn primary", on: { click: (e) => saveProject(p, { webhook_url: url.value.trim() }, e.currentTarget).catch(() => {}) } }, icon("check", "sm"), "Kaydet") : null))),
    card("Son gönderimler", null, list, h("button", { class: "btn sm", on: { click: loadDeliveries } }, icon("refresh", "sm"), "Yenile")));
}

async function projectSecurity(p) {
  const ips = h("textarea", { class: "input mono", rows: 4, placeholder: "203.0.113.10\n198.51.100.0/24" }, p.ip_allowlist);
  const origins = h("textarea", { class: "input mono", rows: 3, placeholder: "https://crm.alanadiniz.com" }, p.allowed_origins);
  return card("Erişim kısıtları", "Anahtar sızsa bile yalnız izin verdiğiniz yerlerden kullanılabilsin.", h("div", { class: "form" },
    field("İzinli IP adresleri", ips, "Her satıra bir IP ya da CIDR aralığı. Boşsa her IP'den istek kabul edilir. CRM sunucunuzun IP'sini yazmanız önerilir."),
    field("Tarayıcıdan izinli kaynaklar (CORS)", origins, "API'yi doğrudan tarayıcıdan çağıracaksanız sitenizin adresini yazın. Önerilen yol, API'yi CRM sunucunuzdan çağırmaktır; o durumda boş bırakın."),
    saveBar((btn) => saveProject(p, { ip_allowlist: ips.value.trim(), allowed_origins: origins.value.trim() }, btn))));
}

async function projectUsage(p) {
  const o = await api(`projects/${p.id}/usage?days=30`);
  return h("div", { class: "stack" },
    kpis([["30 gün istek", nf(o.period.requests), `${nf(o.period.failed)} hata`, "requests"], ["30 gün karakter", nf(o.period.chars), null, "text"],
      ["30 gün ses", fmtDur(o.period.audio_seconds), null, "volume"], ["Bugün", nf(o.today.requests), `${nf(o.today.chars)} karakter`, "calendar"]]),
    card("Günlük istekler", null, barChart(o.series)),
    card("Son istekler", null, genTable(o.recent, { project: false }), h("a", { class: "btn sm", href: `#/requests?project=${p.id}` }, "Tümü", icon("right", "sm"))));
}

async function projectIntegration(p) {
  const s = (await api("settings")).settings;
  const base = s.public_api_url || location.origin.replace("panel.", "api.");
  const curlSync = `curl -X POST ${base}/v1/speech \\
  -H "Authorization: Bearer $TINI_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: post-4821" \\
  -d '{
    "text": "Yeni kampanyamız bugün başladı! Ayrıntılar profilimizde.",
    "preset": "sosyal-medya",
    "reference_id": "post-4821"
  }'`;
  const presetEx = `// Hazır ayar + tek bir ayarı ezme (preset isteğe bağlıdır)
{ "text": "…", "preset": "haber" }                       // haber ayarları
{ "text": "…", "preset": "hikaye", "speed": 1.0 }        // hikâye ayarları, ama hız 1.0
{ "text": "…", "voice": "ema", "speed": 1.1, "pitch": -1 } // hazır ayarsız, kendi ayarlarınız
{ "text": "…" }                                           // projenin varsayılanları

// Mevcut hazır ayarlar:  GET ${base}/v1/presets`;
  const response = `{
  "id": "gen_8Kx…",
  "status": "succeeded",
  "voice": "ema",
  "reference_id": "post-4821",
  "audio": {
    "url": "${base}/files/gen_8Kx….mp3?s=…",
    "download_url": "${base}/files/gen_8Kx….mp3?s=…&download=1",
    "format": "mp3",
    "duration": 4.21,
    "size_bytes": 67380
  },
  "expires_at": "2026-11-23T10:12:00Z"
}`;
  const node = `// Node.js / CRM sunucusu
const res = await fetch("${base}/v1/speech", {
  method: "POST",
  headers: {
    "Authorization": \`Bearer \${process.env.TINI_API_KEY}\`,
    "Content-Type": "application/json",
    "Idempotency-Key": \`post-\${post.id}\`,
  },
  body: JSON.stringify({ text: post.text, reference_id: String(post.id) }),
});
const job = await res.json();
if (res.status === 200) saveAudio(post.id, job.audio.url);        // hazır
else if (res.status === 202) waitForWebhook(job.id);              // uzun metin: webhook gelecek
else throw new Error(job.error.message);`;
  const php = `<?php // PHP / Laravel
$res = Http::withToken(env('TINI_API_KEY'))
    ->withHeaders(['Idempotency-Key' => 'post-'.$post->id])
    ->timeout(90)
    ->post('${base}/v1/speech', ['text' => $post->text, 'reference_id' => (string) $post->id]);
$audioUrl = $res->json('audio.url');`;
  const verify = `// Webhook doğrulama (Node.js / Express)
import crypto from "node:crypto";
app.post("/webhooks/tini", express.raw({ type: "application/json" }), (req, res) => {
  const ts = req.header("X-Tini-Timestamp");
  const sig = req.header("X-Tini-Signature");          // "sha256=..."
  const mac = "sha256=" + crypto.createHmac("sha256", process.env.TINI_WEBHOOK_SECRET)
    .update(\`\${ts}.\${req.body}\`).digest("hex");
  const fresh = Math.abs(Date.now() / 1000 - Number(ts)) < 300;
  if (!fresh || !crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(sig))) return res.sendStatus(401);
  const event = JSON.parse(req.body);                  // event.event: "speech.succeeded" | "speech.failed"
  updatePost(event.data.reference_id, event.data.audio?.url);
  res.sendStatus(200);
});`;
  return h("div", { class: "stack" },
    card("Nasıl çalışır", null, h("ol", { class: "stack", style: { paddingLeft: "20px", margin: 0 } },
      h("li", null, "CRM, metni ", h("code", null, "POST /v1/speech"), " ile gönderir. Kısa metinlerde ses birkaç saniyede hazırlanır ve yanıtta ", h("code", null, "audio.url"), " gelir."),
      h("li", null, "Metin uzunsa ya da ses ", h("b", null, `${s.sync_wait_seconds} saniye`), " içinde bitmezse yanıt ", h("code", null, "202"), " olur; bittiğinde webhook gelir. Uzun metinler için doğrudan ", h("code", null, "POST /v1/jobs"), " kullanın."),
      h("li", null, "CRM ses dosyasını bu URL'den indirip kendi deposuna kaydetmeli ya da URL'yi doğrudan oynatıcıda kullanmalıdır."),
      h("li", null, s.auto_delete_enabled ? `Ses dosyaları ${p.retention_days ?? s.retention_days} gün sonra otomatik silinir (yanıttaki expires_at). Kalıcı saklamak istediğiniz sesleri CRM'e indirin.` : "Otomatik silme kapalı; sesler siz silene kadar saklanır."),
      h("li", null, h("code", null, "Idempotency-Key"), " başlığı aynı gönderinin iki kez işlenmesini engeller (CRM yeniden denediğinde)."))),
    card("İstek (cURL)", null, codeBlock(curlSync)),
    card("Hazır ayarlar ve ses ayarları", `Bu projenin varsayılan hazır ayarı: ${p.default_preset || "yok"}. Tüm alanlar isteğe bağlıdır.`, codeBlock(presetEx)),
    card("Yanıt", null, codeBlock(response)),
    card("Node.js", null, codeBlock(node)),
    card("PHP (Laravel)", null, codeBlock(php)),
    card("Webhook imzasını doğrulama", "Gövdeyi ham haliyle (byte olarak) kullanın; JSON'a çevirip yeniden yazarsanız imza tutmaz.", codeBlock(verify)),
    card("Tüm uç noktalar", null, h("p", null, "Ayrıntılı ve denenebilir belgeler: ", h("a", { href: `${base}/docs`, target: "_blank", rel: "noopener" }, `${base}/docs`, " ", icon("external", "sm")))));
}

/* ====================================================================== İstek kayıtları */

async function pageRequests(_, query) {
  await loadVoices().catch(() => {});
  const { projects } = await api("projects");
  const f = { project_id: query.get("project") || "", status: query.get("status") || "", voice: "", q: query.get("q") || "" };
  const tbody = h("div");
  const more = h("button", { class: "btn", hidden: true }, "Daha fazla göster");
  let next = null;
  const qs = () => new URLSearchParams(Object.entries({ ...f, limit: 30 }).filter(([, v]) => v)).toString();
  const load = async (append = false) => {
    const r = await api(`generations?${qs()}${append && next ? `&before=${next}` : ""}`);
    next = r.next; more.hidden = !next;
    const items = append ? [...(tbody._items || []), ...r.items] : r.items;
    tbody._items = items;
    tbody.replaceChildren(items.length ? genTable(items) : emptyState("search", "Kayıt bulunamadı", "Filtreleri değiştirin ya da daha sonra tekrar bakın."));
  };
  more.onclick = () => load(true).catch(fail);
  const proj = selectInput([["", "Tüm projeler"], ...projects.map((p) => [p.id, p.name])], f.project_id, { on: { change: (e) => { f.project_id = e.target.value; load().catch(fail); } } });
  const st = selectInput([["", "Tüm durumlar"], ["succeeded", "Hazır"], ["queued", "Sırada"], ["running", "Üretiliyor"], ["failed", "Başarısız"], ["canceled", "İptal"], ["expired", "Silindi"]], f.status, { on: { change: (e) => { f.status = e.target.value; load().catch(fail); } } });
  const vc = selectInput([["", "Tüm sesler"], ...(S.voices || []).map((v) => [v.id, v.name])], "", { on: { change: (e) => { f.voice = e.target.value; load().catch(fail); } } });
  const search = textInput(f.q, { type: "search", placeholder: "Metin, kayıt kimliği ya da reference_id", on: { input: debounce((e) => { f.q = e.target.value; load().catch(fail); }, 350) } });
  await load();
  const filters = h("div", { class: "card-body row", style: { borderBottom: "1px solid var(--line)" } },
    h("div", { style: { flex: "2 1 260px" } }, search), h("div", { style: { flex: "1 1 160px" } }, proj), h("div", { style: { flex: "1 1 140px" } }, st), h("div", { style: { flex: "1 1 140px" } }, vc),
    h("button", { class: "btn icon", title: "Yenile", "aria-label": "Yenile", on: { click: () => load().catch(fail) } }, icon("refresh", "sm")));
  return h("div", { class: "page" }, pageHead("İstek kayıtları", "API'ye ve deneme alanına gelen tüm seslendirme istekleri. Ayrıntı için satıra tıklayın."),
    h("section", { class: "card" }, filters, tbody), h("div", { class: "row" }, h("span", { class: "spacer" }), more, h("span", { class: "spacer" })));
}

/* ---------------------------------------------------------------------- ayrıntı paneli */

let sheetEl = null;
function closeSheet() { if (sheetEl) { sheetEl.audio?.pause(); sheetEl.remove(); sheetEl = null; document.removeEventListener("keydown", sheetEsc); } }
function sheetEsc(e) { if (e.key === "Escape") closeSheet(); }

async function openGeneration(id) {
  closeSheet();
  const body = h("div", { class: "sheet-body" }, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
  const title = h("h2", null, "İstek ayrıntısı");
  const wrap = h("div", null, h("div", { class: "sheet-backdrop", on: { click: closeSheet } }),
    h("aside", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": "İstek ayrıntısı" },
      h("div", { class: "sheet-head" }, title, h("button", { class: "btn ghost icon", "aria-label": "Kapat", on: { click: closeSheet } }, icon("close"))), body));
  document.body.append(wrap);
  sheetEl = wrap;
  document.addEventListener("keydown", sheetEsc);
  try {
    const g = await api(`generations/${id}`);
    renderGeneration(g, body, title);
  } catch (e) { body.replaceChildren(emptyState("alert", "Yüklenemedi", e.message)); }
}

function renderGeneration(g, body, title) {
  title.textContent = g.reference_id ? `Kayıt: ${g.reference_id}` : "İstek ayrıntısı";
  const reload = async () => renderGeneration(await api(`generations/${g.id}`), body, title);
  const audioOk = g.audio_available;
  const player = audioOk ? audioPlayer(`/admin/api/generations/${g.id}/audio`, g.peaks, g.words, g.spoken_text || g.text) : null;
  if (player) sheetEl.audio = player.audio;
  const act = (label, ic, fn, kind = "") => h("button", { class: `btn sm ${kind}`, on: { click: async (e) => { const b = e.currentTarget; busy(b, true); try { await fn(); } catch (ex) { fail(ex); } finally { busy(b, false); } } } }, icon(ic, "sm"), label);
  const actions = h("div", { class: "row" },
    audioOk ? h("a", { class: "btn sm primary", href: `/admin/api/generations/${g.id}/audio?download=1` }, icon("download", "sm"), `İndir (${g.format.toUpperCase()})`) : null,
    audioOk && g.public?.audio ? act("Herkese açık bağlantı", "link", () => copy(g.public.audio.url, "Bağlantı kopyalandı")) : null,
    isAdmin() && audioOk ? act(g.keep ? "Silinmesin işaretini kaldır" : "Silinmesin", "pin", async () => { await api(`generations/${g.id}/keep`, { method: "POST", body: { keep: !g.keep } }); toast(g.keep ? "Otomatik silmeye dahil edildi" : "Bu ses otomatik silinmeyecek"); reload(); }) : null,
    isAdmin() && ["queued", "running"].includes(g.status) ? act("İptal et", "stop", async () => { await api(`generations/${g.id}/cancel`, { method: "POST" }); toast("İptal istendi"); reload(); }, "danger") : null,
    isAdmin() && ["failed", "canceled"].includes(g.status) ? act("Yeniden dene", "retry", async () => { await api(`generations/${g.id}/retry`, { method: "POST" }); toast("Kuyruğa alındı"); reload(); }) : null,
    isAdmin() && !["queued", "running"].includes(g.status) && g.deliveries?.length ? act("Webhook'u tekrar gönder", "send", async () => { await api(`generations/${g.id}/resend-webhook`, { method: "POST" }); toast("Webhook kuyruğa alındı"); }) : null,
    isAdmin() && !["queued", "running"].includes(g.status) ? act("Sil", "trash", async () => {
      if (!(await confirmDialog({ title: "Kayıt silinsin mi?", text: "Ses dosyası ve kayıt kalıcı olarak silinir.", confirm: "Sil", danger: true }))) return;
      await api(`generations/${g.id}`, { method: "DELETE" }); toast("Silindi"); closeSheet(); route();
    }, "danger") : null);
  const p = g.params || {};
  body.replaceChildren(
    h("div", { class: "row" }, statusBadge(g.status, audioOk), g.keep ? h("span", { class: "badge run" }, icon("pin", "sm"), "Silinmeyecek") : null, h("span", { class: "spacer" }), h("span", { class: "muted small" }, fmtDate(g.created_at))),
    g.error_code ? h("div", { class: "banner" }, icon("alert", "sm"), h("p", null, h("b", null, g.error_code), g.error_message ? `: ${g.error_message}` : "")) : null,
    player ? player.el : (g.status === "succeeded" ? h("div", { class: "banner info" }, icon("info", "sm"), h("p", null, `Ses dosyası ${fmtDate(g.audio_deleted_at)} tarihinde silindi.`)) : null),
    actions,
    !player && (g.text || g.spoken_text) ? h("div", { class: "stack" }, h("h3", null, "Metin"), h("div", { class: "reading-text" }, (g.text || "").split(/\n\s*\n/).map((t) => h("p", null, t)))) : null,
    h("dl", { class: "kv" },
      h("dt", null, "Kimlik"), h("dd", null, h("code", null, g.id)),
      h("dt", null, "Proje"), h("dd", null, h("a", { href: `#/projects/${g.project_id}`, on: { click: closeSheet } }, g.project_name || g.project_id)),
      h("dt", null, "Kaynak"), h("dd", null, g.source === "panel" ? "Panel" : `API${g.key ? `, ${g.key}` : ""}${g.client_ip ? `, ${g.client_ip}` : ""}`),
      g.reference_id ? [h("dt", null, "reference_id"), h("dd", null, h("code", null, g.reference_id))] : null,
      h("dt", null, "Ses"), h("dd", null, `${voiceName(g.voice)}${g.style && g.style !== "neutral" ? `, ${styleName(g.style)}` : ""}${g.model ? ` (${g.model})` : ""}`),
      p.preset ? [h("dt", null, "Hazır ayar"), h("dd", null, h("code", null, p.preset))] : null,
      h("dt", null, "Metin"), h("dd", null, `${nf(g.chars)} karakter${g.text ? "" : " (saklanmadı)"}`),
      g.duration ? [h("dt", null, "Dosya"), h("dd", null, `${fmtDur(g.duration)}, ${g.format.toUpperCase()}, ${(g.sample_rate || 0) / 1000} kHz, ${fmtBytes(g.audio_size)}`)] : null,
      g.processing_ms ? [h("dt", null, "İşlem süresi"), h("dd", null, `${nf(g.processing_ms / 1000, 2)} sn${g.attempts > 1 ? ` (${g.attempts} deneme)` : ""}`)] : null,
      h("dt", null, "Ayarlar"), h("dd", { class: "small" }, `hız ${p.speed ?? 1}×, perde ${p.pitch ?? 0}, duraklama ${p.pause_scale ?? 1}×, paragraf ${p.paragraph_pause ?? 0.7} sn, tohum ${g.seed ?? p.seed ?? "rastgele"}`),
      g.expires_at ? [h("dt", null, "Silinecek"), h("dd", null, fmtDate(g.expires_at))] : null,
      g.metadata ? [h("dt", null, "metadata"), h("dd", null, h("code", null, JSON.stringify(g.metadata)))] : null),
    g.deliveries?.length ? h("div", { class: "stack" }, h("h3", null, "Webhook gönderimleri"), g.deliveries.map((d) => h("div", { class: "row small" },
      d.status === "delivered" ? h("span", { class: "badge ok" }, "Teslim") : d.status === "pending" ? h("span", { class: "badge run" }, "Bekliyor") : h("span", { class: "badge bad" }, "Başarısız"),
      h("code", null, d.event), h("span", { class: "muted" }, `${d.attempts} deneme${d.last_status_code ? `, HTTP ${d.last_status_code}` : ""}`)))) : null);
  if (["queued", "running"].includes(g.status)) setTimeout(() => { if (sheetEl && body.isConnected) reload().catch(() => {}); }, 1500);
}
function styleName(id) { return S.styles?.find((s) => s.id === id)?.label || id; }

/* ---------------------------------------------------------------------- oynatıcı */

function audioPlayer(src, peaks = [], words = [], text = "") {
  const audio = new Audio(); audio.preload = "metadata"; audio.src = src;
  const btn = h("button", { class: "play", type: "button", "aria-label": "Oynat" }, icon("play"));
  const canvas = h("canvas", { class: "wave", tabIndex: 0, role: "slider", "aria-label": "Konum" });
  const time = h("span", { class: "time" }, "0:00");
  const textBox = h("div", { class: "reading-text" });
  let spans = [], now = -1;
  if (words.length) {
    const paras = (text || "").split(/\n\s*\n/).map((p) => p.split(/\s+/).filter(Boolean));
    const aligned = paras.reduce((n, p) => n + p.length, 0) === words.length;
    let i = 0;
    for (const tokens of aligned ? paras : [words.map((w) => w.w)]) {
      const p = h("p");
      tokens.forEach((t, k) => { const s = h("span", { class: "w" }, t); s.dataset.i = i++; p.append(s, k < tokens.length - 1 ? " " : ""); });
      textBox.append(p);
    }
    spans = [...textBox.querySelectorAll(".w")];
  } else if (text) textBox.append(...text.split(/\n\s*\n/).map((t) => h("p", null, t)));
  const dur = () => (isFinite(audio.duration) ? audio.duration : 0);
  const draw = () => {
    const dpr = devicePixelRatio || 1, w = canvas.clientWidth, hh = canvas.clientHeight;
    if (!w) return;
    canvas.width = w * dpr; canvas.height = hh * dpr;
    const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr);
    const css = getComputedStyle(document.documentElement);
    const played = css.getPropertyValue("--teal").trim(), rest = css.getPropertyValue("--line-strong").trim();
    const pk = peaks.length ? peaks : new Array(80).fill(0.1);
    const step = 4, bars = Math.floor(w / step), prog = dur() ? audio.currentTime / dur() : 0;
    for (let i = 0; i < bars; i++) {
      const a = Math.floor((i / bars) * pk.length), b = Math.max(a + 1, Math.floor(((i + 1) / bars) * pk.length));
      let v = 0; for (let k = a; k < b; k++) v = Math.max(v, pk[k] || 0);
      const bh = Math.max(2, Math.pow(v, 0.8) * (hh - 4));
      ctx.fillStyle = i / bars < prog ? played : rest;
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(i * step, (hh - bh) / 2, step - 1.5, bh, 1.2) : ctx.rect(i * step, (hh - bh) / 2, step - 1.5, bh); ctx.fill();
    }
  };
  const tick = () => {
    time.textContent = `${clock(audio.currentTime)} / ${clock(dur())}`;
    draw();
    if (spans.length) {
      let lo = 0, hi = words.length - 1, idx = -1;
      while (lo <= hi) { const m = (lo + hi) >> 1; if (words[m].s <= audio.currentTime) { idx = m; lo = m + 1; } else hi = m - 1; }
      if (idx !== now) {
        textBox.classList.add("live");
        spans.forEach((s, k) => { s.classList.toggle("read", k < idx); s.classList.toggle("now", k === idx); });
        now = idx;
        const cur = spans[idx];
        if (cur) { const top = cur.offsetTop - textBox.offsetTop; if (top < textBox.scrollTop || top > textBox.scrollTop + textBox.clientHeight - 40) textBox.scrollTop = top - 40; }
      }
    }
    if (!audio.paused) requestAnimationFrame(tick);
  };
  btn.onclick = () => (audio.paused ? audio.play() : audio.pause());
  audio.addEventListener("play", () => { btn.replaceChildren(icon("pause")); btn.setAttribute("aria-label", "Duraklat"); tick(); });
  audio.addEventListener("pause", () => { btn.replaceChildren(icon("play")); btn.setAttribute("aria-label", "Oynat"); tick(); });
  audio.addEventListener("loadedmetadata", tick);
  const seek = (t) => { audio.currentTime = Math.max(0, Math.min(dur(), t)); tick(); };
  canvas.addEventListener("click", (e) => { const r = canvas.getBoundingClientRect(); seek(((e.clientX - r.left) / r.width) * dur()); });
  canvas.addEventListener("keydown", (e) => { if (e.key === "ArrowRight") seek(audio.currentTime + 5); if (e.key === "ArrowLeft") seek(audio.currentTime - 5); if (e.key === " ") { e.preventDefault(); btn.click(); } });
  textBox.addEventListener("click", (e) => { const i = e.target.dataset?.i; if (i != null && words[i]) { seek(words[i].s); audio.play(); } });
  window.addEventListener("themechange", draw);
  new ResizeObserver(draw).observe(canvas);
  const el = h("div", { class: "stack" }, h("div", { class: "player" }, btn, canvas, time), (text || words.length) ? textBox : null);
  return { el, audio };
}

/* ====================================================================== Deneme alanı */

const PG_KEY = "tini.playground.v1";


async function pagePlayground() {
  const voices = (await loadVoices()).filter((v) => v.enabled);
  const [{ projects }, { presets }] = await Promise.all([api("projects"), api("presets")]);
  let st = { text: "", voice: voices.find((v) => v.default)?.id || voices[0]?.id, style: "neutral", speed: 1, pause_scale: 1, paragraph_pause: 0.7, pitch: 0, format: "mp3", sample_rate: 48000, seed: 0, project_id: "" };
  try { st = { ...st, ...JSON.parse(localStorage.getItem(PG_KEY) || "{}") }; } catch { /* yok */ }
  if (!voices.find((v) => v.id === st.voice)) st.voice = voices[0]?.id;
  const save = () => { try { localStorage.setItem(PG_KEY, JSON.stringify(st)); } catch { /* yok */ } };

  const text = h("textarea", { class: "input reading", placeholder: "Seslendirmek istediğiniz metni yazın. Paragrafları boş satırla ayırın.", on: { input: (e) => { st.text = e.target.value; counts(); save(); } } }, st.text);
  const countEl = h("span", { class: "muted small" });
  const counts = () => { const l = (st.text.match(/\p{L}/gu) || []).length; countEl.textContent = `${nf(st.text.length)} karakter${l ? `, yaklaşık ${fmtDur(l / 18 / st.speed)} ses` : ""}`; };
  counts();

  const styleWrap = h("div");
  const drawStyles = () => {
    const v = voices.find((x) => x.id === st.voice);
    const ok = v && v.styles.length > 1;
    if (!ok) st.style = "neutral";
    styleWrap.replaceChildren(field("Üslup", ok ? seg(S.styles.filter((s) => v.styles.includes(s.id)).map((s) => [s.id, s.label]), st.style, (x) => { st.style = x; save(); }) : h("p", { class: "muted small" }, `${v?.name || "Bu ses"} üslup seçeneği sunmuyor.`)));
  };
  const sliders = {
    speed: slider({ label: "Hız", min: 0.5, max: 2, step: 0.05, value: st.speed, format: (v) => `${nf(v, 2)}×`, onInput: (v) => { st.speed = v; counts(); save(); } }),
    pause_scale: slider({ label: "Cümle arası duraklama", min: 0, max: 3, step: 0.1, value: st.pause_scale, format: (v) => (v ? `${nf(v, 1)}×` : "Yok"), onInput: (v) => { st.pause_scale = v; save(); } }),
    paragraph_pause: slider({ label: "Paragraf arası", min: 0, max: 3, step: 0.1, value: st.paragraph_pause, format: (v) => `${nf(v, 1)} sn`, onInput: (v) => { st.paragraph_pause = v; save(); } }),
    pitch: slider({ label: "Ses perdesi", min: -4, max: 4, step: 0.5, value: st.pitch, format: (v) => (v ? `${v > 0 ? "+" : "−"}${nf(Math.abs(v), 1)} yarım ton` : "Doğal"), onInput: (v) => { st.pitch = v; save(); } }),
  };
  const voiceSel = selectInput(voices.map((v) => [v.id, `${v.name}${v.default ? " (varsayılan)" : ""}`]), st.voice, { on: { change: (e) => { st.voice = e.target.value; drawStyles(); save(); } } });
  const applyPreset = (vals) => {
    const { format, sample_rate, ...rest } = vals;
    Object.assign(st, rest);
    if (rest.voice && voices.find((v) => v.id === rest.voice)) voiceSel.value = rest.voice;
    for (const [k, s] of Object.entries(sliders)) { s.input.value = st[k]; s.paint(); }
    drawStyles(); counts(); save();
  };
  drawStyles();
  const seed = numInput(st.seed ?? "", { min: 0, placeholder: "Rastgele", on: { change: (e) => { st.seed = intOrNull(e.target.value); save(); } } });
  const result = h("div", { class: "stack" });
  const go = h("button", { class: "btn primary", type: "button" }, icon("play", "sm"), "Seslendir");
  const run = async () => {
    if (!st.text.trim()) { toast("Önce metin yazın", "bad"); text.focus(); return; }
    busy(go, true);
    result.replaceChildren(card(null, null, h("div", { class: "row" }, h("span", { class: "spin" }), h("span", { class: "muted" }, "Ses üretiliyor…"))));
    try {
      const body = { text: st.text, voice: st.voice, style: st.style, speed: st.speed, pause_scale: st.pause_scale, paragraph_pause: st.paragraph_pause, pitch: st.pitch, format: st.format, sample_rate: st.sample_rate, seed: st.seed };
      if (st.project_id) body.project_id = st.project_id;
      const g = await api("playground", { method: "POST", body });
      if (g.status !== "succeeded") throw new Error(g.error_message || "Ses üretilemedi");
      const pl = audioPlayer(`/admin/api/generations/${g.id}/audio`, g.peaks, g.words, g.spoken_text);
      result.replaceChildren(card("Sonuç", `${fmtDur(g.duration)} ses, ${nf(g.processing_ms / 1000, 2)} sn'de üretildi (gerçek zamanın ${nf(g.duration / Math.max(g.processing_ms / 1000, 0.01), 1)} katı hızında).`,
        h("div", { class: "stack" }, pl.el, h("div", { class: "row" },
          h("a", { class: "btn primary sm", href: `/admin/api/generations/${g.id}/audio?download=1` }, icon("download", "sm"), `İndir (${g.format.toUpperCase()})`),
          h("button", { class: "btn sm", on: { click: () => openGeneration(g.id) } }, icon("info", "sm"), "Ayrıntılar")))));
      pl.audio.play().catch(() => {});
    } catch (e) { result.replaceChildren(h("div", { class: "banner" }, icon("alert", "sm"), h("p", null, e.message))); }
    finally { busy(go, false); }
  };
  go.onclick = run;
  text.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); } });

  const preview = h("div", { class: "muted", hidden: true, style: { fontFamily: "var(--read)", whiteSpace: "pre-wrap" } });
  const previewBtn = h("button", { class: "btn sm ghost", type: "button", on: { click: async () => {
    if (!preview.hidden) { preview.hidden = true; return; }
    try { const r = await api("normalize", { method: "POST", body: { text: st.text.slice(0, 60000) } }); preview.textContent = r.spoken_text || "—"; preview.hidden = false; } catch (e) { fail(e); }
  } } }, icon("text", "sm"), "Okunuşu göster");

  return h("div", { class: "page" },
    pageHead("Deneme alanı", "Sesleri ve ayarları deneyin. Buradaki istekler “Panel” projesine kaydedilir; isterseniz başka bir proje adına da üretebilirsiniz."),
    h("div", { class: "split" },
      h("div", { class: "stack" },
        card(null, null, h("div", { class: "stack" }, text, h("div", { class: "row" }, countEl, h("span", { class: "spacer" }), previewBtn, go), preview)),
        result),
      h("div", { class: "stack" },
        card("Ses", null, h("div", { class: "stack" },
          field(null, voiceSel),
          styleWrap,
          field("Hazır ayar", presets.length ? h("div", { class: "checks" }, presets.map((p) => h("button", { class: "check", type: "button", title: p.description || presetSummary(p.params), on: { click: () => { applyPreset(p.params); toast(`${p.name} uygulandı`); } } }, p.name))) : h("p", { class: "muted small" }, "Ayarlar > Hazır ayarlar bölümünden ekleyin.")))),
        card("Okuma", null, h("div", { class: "stack" }, Object.values(sliders).map((s) => s.el))),
        card("Çıktı", null, h("div", { class: "stack" },
          field("Biçim", seg([["mp3", "MP3"], ["wav", "WAV"], ["ogg", "OGG"], ["flac", "FLAC"]], st.format, (v) => { st.format = v; save(); })),
          field("Örnekleme hızı", seg([[48000, "48 kHz"], [24000, "24"], [16000, "16"], [8000, "8"]], st.sample_rate, (v) => { st.sample_rate = v; save(); })),
          field("Tohum", seed, "Aynı tohum aynı sesi verir. Boş = her seferinde farklı."),
          field("Proje", selectInput([["", "Panel (varsayılan)"], ...projects.filter((p) => !p.is_system).map((p) => [p.id, p.name])], st.project_id, { on: { change: (e) => { st.project_id = e.target.value; save(); } } }), "Seçilen projenin sınırları ve kotası uygulanır."))))));
}

/* ====================================================================== Sesler */

async function pageVoices() {
  const voices = await loadVoices(true);
  const s = (await api("settings")).settings;
  const list = h("div", { class: "stack" });
  const draw = (vs) => list.replaceChildren(...vs.map((v) => {
    const en = toggle(v.enabled, "Etkin", "Kapalı sesler API'de kullanılamaz.", async (val) => {
      try { S.voices = (await api(`voices/${v.id}`, { method: "PATCH", body: { enabled: val } })).voices; toast(val ? "Ses etkinleştirildi" : "Ses kapatıldı"); draw(S.voices); }
      catch (e) { fail(e); en.input.checked = !val; }
    });
    en.input.disabled = !isAdmin();
    const name = textInput(v.name, { maxLength: 60 });
    return h("section", { class: "card card-pad stack" },
      h("div", { class: "row" }, h("h2", null, v.name), v.default ? h("span", { class: "badge ok" }, "Varsayılan") : null,
        v.loaded ? h("span", { class: "badge" }, "Yüklü") : h("span", { class: "badge bad" }, "Motorda yüklü değil"), h("span", { class: "spacer" }),
        isAdmin() && !v.default && v.enabled ? h("button", { class: "btn sm", on: { click: async () => { try { await api("settings", { method: "PATCH", body: { default_voice: v.id } }); toast(`${v.name} varsayılan ses oldu`); draw(await loadVoices(true)); } catch (e) { fail(e); } } } }, "Varsayılan yap") : null),
      h("p", { class: "muted" }, v.description),
      h("dl", { class: "kv" },
        h("dt", null, "API kimliği"), h("dd", null, h("code", null, v.id)),
        h("dt", null, "Model"), h("dd", null, `${v.model} ${v.model_version || ""}`),
        h("dt", null, "Üsluplar"), h("dd", null, v.styles.map(styleName).join(", ")),
        h("dt", null, "Lisans"), h("dd", null, v.license || "—"),
        h("dt", null, "Atıf"), h("dd", { class: "small" }, v.attribution || "—")),
      isAdmin() ? h("div", { class: "form-grid" }, en.el, field("Panelde görünen ad", h("div", { class: "input-group" }, name,
        h("button", { class: "btn", on: { click: async () => { try { S.voices = (await api(`voices/${v.id}`, { method: "PATCH", body: { display_name: name.value.trim() } })).voices; toast("Ad kaydedildi"); } catch (e) { fail(e); } } } }, "Kaydet")))) : null);
  }));
  draw(voices);
  return h("div", { class: "page" }, pageHead("Sesler", "Ses motorunda yüklü modeller. API'de sesler kimlikleriyle (ör. ema) kullanılır; model değişse de bu kimlikler sabit kalır, böylece entegrasyonunuz bozulmaz."),
    h("div", { class: "banner info" }, icon("info", "sm"), h("p", null, `Sistem varsayılanı: `, h("b", null, voiceName(s.default_voice)), `. İstekte ses belirtilmezse önce projenin, sonra sistemin varsayılan sesi kullanılır.`)),
    list);
}

/* ====================================================================== Ayarlar */

const SETTINGS_TABS = [["general", "Genel", "settings"], ["presets", "Hazır ayarlar", "sparkle"], ["retention", "Saklama", "clock"], ["queue", "Kuyruk ve sınırlar", "queue"], ["security", "Güvenlik", "shield"], ["maintenance", "Bakım ve yedek", "database"]];

async function pageSettings([tab = "general"]) {
  const [{ settings: s, env }, sys] = await Promise.all([api("settings"), api("system"), loadVoices().catch(() => [])]);
  const changes = {};
  const set = (k) => (v) => { changes[k] = v; };
  const num = (k, attrs = {}) => numInput(s[k], { ...attrs, on: { input: (e) => { changes[k] = intOrNull(e.target.value); } } });
  const sw = (k, title, sub) => { const t = toggle(s[k], title, sub, set(k)); t.input.disabled = !isAdmin(); return t.el; };
  const save = saveBar(async (btn) => {
    if (!Object.keys(changes).length) { toast("Değişiklik yok"); return; }
    busy(btn, true);
    try { await api("settings", { method: "PATCH", body: changes }); toast("Ayarlar kaydedildi"); for (const k of Object.keys(changes)) delete changes[k]; }
    catch (e) { fail(e); } finally { busy(btn, false); }
  });
  let body;
  if (tab === "presets") {
    body = await presetsSection();
  } else if (tab === "general") {
    body = card("Genel", null, h("div", { class: "form" },
      field("API'nin herkese açık adresi", textInput(s.public_api_url, { type: "url", placeholder: env.public_api_url || "https://api.alanadiniz.com", on: { input: (e) => { changes.public_api_url = e.target.value; } } }),
        "Ses dosyası bağlantıları ve webhook'lardaki URL'ler bu adresle oluşturulur. Boşsa ortam değişkenindeki PUBLIC_API_URL kullanılır."),
      field("Varsayılan ses", selectInput((S.voices || []).filter((v) => v.enabled).map((v) => [v.id, v.name]), s.default_voice, { on: { change: (e) => { changes.default_voice = e.target.value; } } }), "Projede ve istekte ses belirtilmezse kullanılır."),
      sw("docs_public", "API belgeleri herkese açık", "/docs ve /redoc sayfaları. Kapatırsanız yalnız panelden bakılabilir."),
      save));
  } else if (tab === "retention") {
    body = card("Saklama ve otomatik silme", "Ses dosyaları diskte yer kaplar. CRM'iniz sesleri kendi deposuna indiriyorsa kısa bir süre yeterlidir.", h("div", { class: "form" },
      sw("auto_delete_enabled", "Ses dosyalarını otomatik sil", "Kapalıysa sesler siz silene kadar saklanır. Projeler kendi sürelerini belirleyebilir."),
      h("div", { class: "form-grid" },
        field("Saklama süresi", withSuffix(num("retention_days", { min: 1, max: 3650 }), "gün"), "Ses tamamlandıktan bu kadar gün sonra dosya silinir. Kayıt (metin ve istatistik) kalır."),
        field("Bağlantı geçerlilik süresi", withSuffix(num("file_url_ttl_hours", { min: 0 }), "saat"), "0 = bağlantı ses silinene kadar geçerli. Daha sıkı güvenlik için 24 gibi bir değer verin."),
        field("İstek kayıtlarını sakla", withSuffix(num("log_retention_days", { min: 0 }), "gün"), "Bu süreden eski istek kayıtları ve metinleri silinir. 0 = hiç silinmez.")),
      save));
  } else if (tab === "queue") {
    body = h("div", { class: "stack" },
      card("Kuyruk", null, h("div", { class: "form" }, h("div", { class: "form-grid" },
        field("Yanıt bekleme süresi", withSuffix(num("sync_wait_seconds", { min: 5, max: 300 }), "saniye"), "POST /v1/speech ses hazır olana kadar en çok bu kadar bekler; sonra 202 döner."),
        field("Paralel işçi", withSuffix(num("worker_concurrency", { min: 1, max: 16 }), "işçi"), "Ses motoru aynı anda gelen işleri birlikte üretir. İşçilerin yarısı her zaman kısa (anlık) isteklere ayrılır. 8 önerilir."),
        field("Kuyruk sınırı", withSuffix(num("max_queue", { min: 1 }), "iş"), "Bekleyen iş bu sayıya ulaşınca yeni istekler 503 alır."),
        field("Tek istekte en fazla", withSuffix(num("global_max_chars", { min: 100 }), "karakter"), "Tüm projeler için üst sınır.")), save)),
      card("Yeni projeler için varsayılan sınırlar", null, h("div", { class: "form-grid" },
        field("Dakikalık istek", withSuffix(num("default_rate_limit_rpm", { min: 1 }), "istek / dk")),
        field("Aynı anda çalışan iş", withSuffix(num("default_max_concurrency", { min: 1 }), "iş")),
        field("Tek istekte en fazla", withSuffix(num("default_max_chars", { min: 10 }), "karakter")))),
      card("Webhook", null, h("div", { class: "form" }, h("div", { class: "form-grid" },
        field("En fazla deneme", withSuffix(num("webhook_max_attempts", { min: 1, max: 20 }), "deneme"), "Artan aralıklarla: 10 sn, 1 dk, 5 dk, 30 dk, 2 sa, 6 sa…"),
        field("Zaman aşımı", withSuffix(num("webhook_timeout_seconds", { min: 2, max: 60 }), "saniye"))),
        sw("webhook_allow_private", "Özel ağ adreslerine gönderime izin ver", "Kapalıyken 10.x, 192.168.x, localhost gibi adreslere webhook gönderilmez (SSRF koruması). CRM aynı sunucuda ve iç adresle erişiliyorsa açın."))));
  } else if (tab === "security") {
    body = card("Güvenlik", null, h("div", { class: "form" }, h("div", { class: "form-grid" },
      field("Oturum süresi", withSuffix(num("session_hours", { min: 1, max: 720 }), "saat"), "Panelde bu süre sonunda yeniden giriş istenir."),
      field("Hatalı giriş sınırı", withSuffix(num("login_max_attempts", { min: 3 }), "deneme / 15 dk"), "Aşılınca o IP'den giriş 15 dakika engellenir.")),
      h("div", { class: "banner info" }, icon("info", "sm"), h("p", null, "API anahtarları veritabanında yalnız özet (HMAC) olarak saklanır. Panel oturumları HttpOnly çerez ve CSRF belirteci ile korunur. Tüm yönetim işlemleri İşlem kaydı'na yazılır.")),
      save));
  } else {
    const mm = toggle(s.maintenance_mode, "Bakım modu", "Açıkken API yeni istekleri 503 ile reddeder. Panel ve deneme alanı çalışmaya devam eder.", set("maintenance_mode"));
    mm.input.disabled = !isAdmin();
    body = h("div", { class: "stack" },
      card("Bakım modu", null, h("div", { class: "form" }, mm.el,
        field("Bakım mesajı", textInput(s.maintenance_message, { on: { input: (e) => { changes.maintenance_message = e.target.value; } } }), "API yanıtında görünür."), save)),
      card("Sistem", null, h("dl", { class: "kv" },
        h("dt", null, "Sürüm"), h("dd", null, sys.version),
        h("dt", null, "Ses motoru"), h("dd", null, `${sys.engine.status}${sys.engine.version ? `, sürüm ${sys.engine.version}` : ""}${sys.engine.uptime ? `, ${fmtDur(sys.engine.uptime)} çalışıyor` : ""}`),
        h("dt", null, "Veritabanı"), h("dd", null, fmtBytes(sys.db_bytes)),
        sys.disk ? [h("dt", null, "Disk"), h("dd", null, `${fmtBytes(sys.disk.free)} boş / ${fmtBytes(sys.disk.total)}`)] : null,
        h("dt", null, "Saat dilimi"), h("dd", null, sys.timezone),
        h("dt", null, "Domainler"), h("dd", null, `Panel: ${env.panel_hosts.join(", ") || "tümü"}; API: ${env.api_hosts.join(", ") || "tümü"}`))),
      isAdmin() ? card("Yedek ve temizlik", null, h("div", { class: "row" },
        h("a", { class: "btn", href: "/admin/api/backup" }, icon("download", "sm"), "Veritabanı yedeğini indir"),
        h("button", { class: "btn", on: { click: async (e) => { const b = e.currentTarget; busy(b, true); try { const r = await api("maintenance/cleanup", { method: "POST" }); toast(`Temizlik bitti: ${r.audio_deleted} ses, ${r.records_deleted} kayıt silindi`); } catch (ex) { fail(ex); } finally { busy(b, false); } } } }, icon("trash", "sm"), "Süresi dolanları şimdi temizle"))) : null);
  }
  return h("div", { class: "page" }, pageHead("Ayarlar", null),
    h("nav", { class: "tabs" }, SETTINGS_TABS.map(([id, label, ic]) => h("a", { href: `#/settings/${id}`, "aria-current": id === tab ? "page" : null }, icon(ic, "sm"), label))),
    body);
}

/* ---------------------------------------------------------------------- hazır ayarlar */

const PRESET_FIELDS = [
  ["speed", "Hız", 0.5, 2, "×"], ["pitch", "Ses perdesi", -4, 4, "yarım ton"], ["pause_scale", "Cümle arası duraklama", 0, 3, "×"],
  ["paragraph_pause", "Paragraf arası", 0, 3, "sn"], ["volume", "Ek ses seviyesi", -20, 10, "dB"],
];
function presetSummary(params) {
  const out = [];
  if (params.voice) out.push(voiceName(params.voice));
  if (params.style && params.style !== "neutral") out.push(styleName(params.style));
  if (params.speed != null) out.push(`hız ${nf(params.speed, 2)}×`);
  if (params.pitch) out.push(`perde ${params.pitch > 0 ? "+" : ""}${nf(params.pitch, 1)}`);
  if (params.pause_scale != null) out.push(`duraklama ${nf(params.pause_scale, 1)}×`);
  if (params.paragraph_pause != null) out.push(`paragraf ${nf(params.paragraph_pause, 1)} sn`);
  if (params.volume) out.push(`${params.volume > 0 ? "+" : ""}${params.volume} dB`);
  if (params.format) out.push(params.format.toUpperCase());
  if (params.sample_rate) out.push(`${params.sample_rate / 1000} kHz`);
  return out.join(", ") || "Değer yok";
}

async function presetsSection() {
  await loadVoices().catch(() => {});
  const wrap = h("div", { class: "stack" });
  const draw = async () => {
    const { presets } = await api("presets");
    wrap.replaceChildren(card("Hazır ayarlar", "API isteğinde \"preset\": \"haber\" gibi gönderilir; göndermek zorunlu değildir. İstekte açıkça yazılan alanlar (ör. speed) hazır ayardakini ezer. Her projeye bir varsayılan hazır ayar da atanabilir.",
      presets.length ? h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
        h("thead", null, h("tr", null, h("th", null, "Ad"), h("th", null, "API kimliği"), h("th", null, "Ayarlar"), h("th", null, "Varsayılan olduğu proje"), h("th"))),
        h("tbody", null, presets.map((p) => h("tr", null,
          h("td", { "data-label": "Ad" }, h("div", { class: "cell-title" }, p.name), p.description ? h("div", { class: "cell-sub" }, p.description) : null),
          h("td", { "data-label": "Kimlik" }, h("code", null, p.id)),
          h("td", { "data-label": "Ayarlar", class: "small" }, presetSummary(p.params)),
          h("td", { "data-label": "Projeler" }, p.projects_using ? `${p.projects_using} proje` : "—"),
          h("td", { class: "actions" }, isAdmin() ? [
            h("button", { class: "btn sm", on: { click: () => editPreset(p, draw) } }, icon("edit", "sm"), "Düzenle"), " ",
            h("button", { class: "btn sm ghost", title: "Sil", "aria-label": "Sil", on: { click: async () => {
              if (!(await confirmDialog({ title: `“${p.name}” silinsin mi?`, text: `Bu hazır ayarı (${p.id}) kullanan API istekleri preset_not_found hatası alır.${p.projects_using ? ` ${p.projects_using} projenin varsayılanı kaldırılır.` : ""}`, confirm: "Sil", danger: true }))) return;
              try { await api(`presets/${p.id}`, { method: "DELETE" }); toast("Hazır ayar silindi"); draw(); } catch (e) { fail(e); }
            } } }, icon("trash", "sm"))] : null)))))) :
        emptyState("sparkle", "Hazır ayar yok", "Haber, hikâye gibi sık kullandığınız okuma ayarlarını kaydedin."),
      isAdmin() ? h("button", { class: "btn primary", on: { click: () => editPreset(null, draw) } }, icon("add", "sm"), "Yeni hazır ayar") : null));
  };
  await draw();
  return wrap;
}

function editPreset(p, done) {
  const params = { ...(p?.params || {}) };
  const name = textInput(p?.name || "", { maxLength: 60, placeholder: "Örn. Kısa haber" });
  const pid = textInput(p?.id || "", { maxLength: 40, placeholder: "kisa-haber", disabled: !!p, pattern: "[a-z0-9_-]+" });
  if (!p) name.addEventListener("input", () => { if (!pid.dataset.touched) pid.value = slug(name.value); });
  pid.addEventListener("input", () => { pid.dataset.touched = "1"; });
  const desc = textInput(p?.description || "", { maxLength: 300 });
  const voice = selectInput([["", "Değiştirme (projenin sesi)"], ...(S.voices || []).map((v) => [v.id, v.name])], params.voice || "");
  const style = selectInput([["", "Değiştirme"], ...(S.styles || []).map((st) => [st.id, st.label])], params.style || "");
  const nums = PRESET_FIELDS.map(([k, label, min, max, unit]) => [k, numInput(params[k] ?? "", { min, max, step: k === "volume" ? 1 : 0.05, placeholder: "Değiştirme" }), label, unit, min, max]);
  const fmt = selectInput([["", "Değiştirme"], ["mp3", "MP3"], ["wav", "WAV"], ["ogg", "OGG"], ["flac", "FLAC"]], params.format || "");
  const sr = selectInput([["", "Değiştirme"], ["48000", "48 kHz"], ["24000", "24 kHz"], ["16000", "16 kHz"], ["8000", "8 kHz"]], params.sample_rate ? String(params.sample_rate) : "");
  modal({ title: p ? `${p.name} hazır ayarı` : "Yeni hazır ayar", wide: true, body: h("div", { class: "form" },
    h("div", { class: "form-grid" }, field("Ad", name), field("API kimliği", pid, p ? "Kimlik değiştirilemez; API isteklerinde bu yazılır." : "İstekte \"preset\" alanına yazılır.")),
    field("Açıklama", desc),
    h("p", { class: "muted small" }, "Boş bıraktığınız alanlar değiştirilmez; istekteki ya da varsayılan değer kullanılır."),
    h("div", { class: "form-grid" }, field("Ses", voice), field("Üslup", style, "Yalnız üslubu destekleyen seslerde etkilidir."),
      nums.map(([, input, label, unit, min, max]) => field(label, withSuffix(input, unit), `${nf(min, min % 1 ? 1 : 0)} ile ${nf(max, max % 1 ? 1 : 0)} arası`)),
      field("Dosya biçimi", fmt), field("Örnekleme hızı", sr))),
    actions: [{ label: "Vazgeç" }, { label: p ? "Kaydet" : "Oluştur", kind: "primary", run: async () => {
      const out = {};
      if (voice.value) out.voice = voice.value;
      if (style.value) out.style = style.value;
      for (const [k, input] of nums) if (input.value !== "") out[k] = Number(input.value.replace(",", "."));
      if (fmt.value) out.format = fmt.value;
      if (sr.value) out.sample_rate = Number(sr.value);
      const body = { name: name.value.trim(), description: desc.value.trim(), params: out };
      if (p) await api(`presets/${p.id}`, { method: "PATCH", body });
      else await api("presets", { method: "POST", body: { ...body, id: pid.value.trim() } });
      toast("Hazır ayar kaydedildi"); done();
    } }] });
}
function slug(t) {
  const map = { ç: "c", ğ: "g", ı: "i", İ: "i", ö: "o", ş: "s", ü: "u", â: "a", î: "i", û: "u" };
  return t.toLocaleLowerCase("tr-TR").replace(/[çğıİöşüâîû]/g, (c) => map[c] || c).replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
}

/* ====================================================================== Kullanıcılar */

async function pageUsers() {
  const { users } = await api("users");
  const wrap = h("section", { class: "card" });
  const draw = (us) => wrap.replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
    h("thead", null, h("tr", null, h("th", null, "Kişi"), h("th", null, "Yetki"), h("th", null, "Son giriş"), h("th", null, "Durum"), h("th"))),
    h("tbody", null, us.map((u) => h("tr", null,
      h("td", { "data-label": "Kişi" }, h("div", { class: "cell-title" }, u.name || u.email, u.id === S.me.id ? h("span", { class: "muted small" }, " (siz)") : null),
        u.name ? h("div", { class: "cell-sub" }, u.email) : null),
      h("td", { "data-label": "Yetki" }, u.role === "admin" ? "Yönetici" : "İzleyici"),
      h("td", { "data-label": "Son giriş" }, fmtAgo(u.last_login_at)),
      h("td", { "data-label": "Durum" }, u.disabled ? h("span", { class: "badge bad" }, "Kapalı") : h("span", { class: "badge ok" }, "Etkin")),
      h("td", { class: "actions" }, isAdmin() && u.id !== S.me.id ? [
        h("button", { class: "btn sm", on: { click: () => editUser(u) } }, icon("edit", "sm"), "Düzenle"), " ",
        h("button", { class: "btn sm ghost", title: "Sil", "aria-label": "Sil", on: { click: async () => {
          if (!(await confirmDialog({ title: `${u.name || u.email} silinsin mi?`, text: "Bu kişi panele bir daha giriş yapamaz.", confirm: "Sil", danger: true }))) return;
          try { await api(`users/${u.id}`, { method: "DELETE" }); toast("Kullanıcı silindi"); reload(); } catch (e) { fail(e); }
        } } }, icon("trash", "sm"))] : null)))))));
  const reload = async () => draw((await api("users")).users);
  function editUser(u) {
    const role = selectInput([["admin", "Yönetici"], ["viewer", "İzleyici"]], u?.role || "admin");
    const name = textInput(u?.name || "", { maxLength: 80, autocomplete: "off", placeholder: "Ad Soyad" });
    const email = textInput(u?.email || "", { type: "email", maxLength: 254, autocomplete: "off", placeholder: "kisi@alanadiniz.com" });
    const pass = textInput("", { type: "password", autocomplete: "new-password", placeholder: u ? "Değiştirmek için yazın" : "" });
    const dis = toggle(!!u?.disabled, "Hesap kapalı", "Kapalı hesap giriş yapamaz.");
    modal({ title: u ? "Kullanıcıyı düzenle" : "Yeni kullanıcı", body: h("div", { class: "form" },
      field("Ad Soyad", name), field("E-posta", email, "Giriş bu adresle yapılır."), field("Şifre", pass, "En az 10 karakter; büyük harf, küçük harf ve rakam."),
      field("Yetki", role, "İzleyici yalnız görüntüler; değişiklik yapamaz."), u ? dis.el : null),
      actions: [{ label: "Vazgeç" }, { label: u ? "Kaydet" : "Oluştur", kind: "primary", run: async () => {
        const body = { name: name.value.trim(), email: email.value.trim(), role: role.value };
        if (pass.value) body.password = pass.value;
        if (u) await api(`users/${u.id}`, { method: "PATCH", body: { ...body, disabled: dis.input.checked } });
        else await api("users", { method: "POST", body });
        toast("Kaydedildi"); reload();
      } }] });
    setTimeout(() => (u ? name : name).focus(), 50);
  }
  draw(users);
  return h("div", { class: "page" }, pageHead("Kullanıcılar", "Panele e-posta ve şifreyle giriş yapabilen kişiler. Yöneticiler her şeyi değiştirebilir; izleyiciler yalnız görüntüler.",
    isAdmin() ? h("button", { class: "btn primary", on: { click: () => editUser(null) } }, icon("add", "sm"), "Kullanıcı ekle") : null), wrap);
}

/* ====================================================================== İşlem kaydı */

const AUDIT_LABELS = {
  login: "Giriş yaptı", login_failed: "Hatalı giriş denemesi", password_changed: "Şifresini değiştirdi", project_created: "Proje oluşturdu",
  project_updated: "Projeyi güncelledi", project_deleted: "Projeyi sildi", key_created: "API anahtarı oluşturdu", key_revoked: "API anahtarını iptal etti",
  key_deleted: "API anahtarı kaydını sildi", webhook_secret_rotated: "Webhook imza anahtarını yeniledi", webhook_secret_viewed: "Webhook imza anahtarını görüntüledi",
  settings_updated: "Ayarları değiştirdi", voice_updated: "Ses ayarını değiştirdi", user_created: "Kullanıcı ekledi", user_updated: "Kullanıcıyı güncelledi",
  user_deleted: "Kullanıcıyı sildi", profile_updated: "Profilini güncelledi", preset_created: "Hazır ayar ekledi",
  preset_updated: "Hazır ayarı güncelledi", preset_deleted: "Hazır ayarı sildi", backup_downloaded: "Yedek indirdi", generation_deleted: "Kaydı sildi", generation_retried: "İşi yeniden denedi",
};

async function pageAudit() {
  let next = null;
  const tbody = h("tbody");
  const more = h("button", { class: "btn", hidden: true }, "Daha fazla");
  const load = async () => {
    const r = await api(`audit?limit=50${next ? `&before=${next}` : ""}`);
    next = r.next; more.hidden = !next;
    tbody.append(...r.items.map((a) => h("tr", null,
      h("td", { "data-label": "Zaman" }, fmtDate(a.ts)),
      h("td", { "data-label": "Kişi" }, a.username || h("span", { class: "faint" }, "—")),
      h("td", { "data-label": "İşlem" }, AUDIT_LABELS[a.action] || a.action, a.target ? h("div", { class: "cell-sub mono" }, a.target) : null),
      h("td", { "data-label": "Ayrıntı", class: "small muted" }, Object.keys(a.details || {}).length ? JSON.stringify(a.details).slice(0, 160) : ""),
      h("td", { "data-label": "IP", class: "small" }, a.ip || ""))));
  };
  more.onclick = () => load().catch(fail);
  await load();
  return h("div", { class: "page" }, pageHead("İşlem kaydı", "Panelde yapılan tüm önemli işlemler: girişler, anahtarlar, ayar değişiklikleri."),
    h("section", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { class: "t stack-sm" },
      h("thead", null, h("tr", null, h("th", null, "Zaman"), h("th", null, "Kişi"), h("th", null, "İşlem"), h("th", null, "Ayrıntı"), h("th", null, "IP"))), tbody))),
    h("div", { class: "row" }, h("span", { class: "spacer" }), more, h("span", { class: "spacer" })));
}

/* ====================================================================== Hesap */

async function pageAccount() {
  const { sessions } = await api("auth/sessions");
  const cur = textInput("", { type: "password", autocomplete: "current-password" });
  const nw = textInput("", { type: "password", autocomplete: "new-password" });
  const nw2 = textInput("", { type: "password", autocomplete: "new-password" });
  const pName = textInput(S.me.name || "", { maxLength: 80 });
  const pEmail = textInput(S.me.email || "", { type: "email", maxLength: 254 });
  const pPass = textInput("", { type: "password", autocomplete: "current-password", placeholder: "Yalnız e-postayı değiştirirken" });
  return h("div", { class: "page" }, pageHead("Hesabım", `${S.me.email || S.me.username} ile giriş yaptınız.`),
    card("Profil", null, h("div", { class: "form" }, h("div", { class: "form-grid" }, field("Ad Soyad", pName), field("E-posta", pEmail, "Giriş bu adresle yapılır.")),
      field("Mevcut şifre", pPass, "E-postanızı değiştirecekseniz gerekli."),
      saveBar(async (btn) => {
        busy(btn, true);
        try {
          const r = await api("auth/profile", { method: "PATCH", body: { name: pName.value.trim(), email: pEmail.value.trim(), current_password: pPass.value || null } });
          S.me = r.user; pPass.value = ""; toast("Profil kaydedildi"); renderShell(); route();
        } catch (e) { fail(e); } finally { busy(btn, false); }
      }))),
    h("div", { class: "grid2" },
      card("Şifre değiştir", "Değiştirdiğinizde diğer cihazlardaki oturumlarınız kapanır.", h("div", { class: "form" },
        field("Mevcut şifre", cur), field("Yeni şifre", nw, "En az 10 karakter; büyük harf, küçük harf ve rakam."), field("Yeni şifre (tekrar)", nw2),
        saveBar(async (btn) => {
          if (nw.value !== nw2.value) { fail(new Error("Yeni şifreler aynı değil.")); return; }
          busy(btn, true);
          try { await api("auth/password", { method: "POST", body: { current_password: cur.value, new_password: nw.value } }); toast("Şifre değiştirildi"); cur.value = nw.value = nw2.value = ""; }
          catch (e) { fail(e); } finally { busy(btn, false); }
        }))),
      card("Açık oturumlar", null, h("div", { class: "stack" }, sessions.map((s) => h("div", { class: "row small" },
        icon(s.current ? "check" : "user", "sm"), h("div", { style: { flex: 1, minWidth: 0 } }, h("div", null, s.ip || "?", s.current ? h("b", null, " (bu cihaz)") : null), h("div", { class: "cell-sub" }, `${(s.user_agent || "").slice(0, 70)}, son etkinlik ${fmtAgo(new Date(s.last_seen_at * 1000).toISOString())}`)))),
        sessions.length > 1 ? h("button", { class: "btn sm", on: { click: async () => { await api("auth/sessions/revoke-others", { method: "POST" }).catch(fail); toast("Diğer oturumlar kapatıldı"); route(); } } }, icon("logout", "sm"), "Diğer oturumları kapat") : null))));
}

/* ====================================================================== başlat */

function start() {
  renderShell();
  if (!location.hash || location.hash === "#/login") location.hash = "#/";
  route();
}

window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", async () => {
  try {
    const r = await api("auth/me");
    S.me = r.user; S.csrf = r.csrf; start();
  } catch (e) {
    if (e.status !== 401) renderLogin(e.message);
  }
});
