// Hugeicons (ücretsiz, stroke rounded, MIT) setinden kullanılan ikonlarla tek bir SVG sprite üretir.
//   node tools/build_icons.cjs <@hugeicons/core-free-icons/dist/cjs yolu> app/static/icons.svg
const fs = require("fs"), path = require("path");
const [src, out] = process.argv.slice(2);
const NAMES = {
  dashboard: "DashboardSquare01Icon", projects: "Folder01Icon", key: "Key01Icon", requests: "Task01Icon",
  wave: "AudioWave01Icon", mic: "Mic01Icon", settings: "Settings01Icon", users: "UserMultipleIcon",
  shield: "Shield01Icon", audit: "Note01Icon", logout: "Logout01Icon", moon: "Moon02Icon", sun: "Sun03Icon",
  add: "Add01Icon", trash: "Delete02Icon", copy: "Copy01Icon", download: "Download01Icon", play: "PlayIcon",
  pause: "PauseIcon", search: "Search01Icon", filter: "FilterIcon", refresh: "Refresh01Icon", close: "Cancel01Icon",
  check: "Tick02Icon", alert: "Alert02Icon", info: "InformationCircleIcon", right: "ArrowRight01Icon",
  left: "ArrowLeft01Icon", more: "MoreHorizontalIcon", link: "Link01Icon", clock: "Clock01Icon", pin: "PinIcon",
  eye: "ViewIcon", eyeoff: "ViewOffIcon", menu: "Menu01Icon", database: "Database01Icon", webhook: "WebhookIcon",
  server: "ServerStack01Icon", code: "SourceCodeIcon", ok: "CheckmarkCircle02Icon", fail: "CancelCircleIcon",
  edit: "Edit02Icon", send: "SentIcon", help: "HelpCircleIcon", chart: "ChartHistogramIcon", lock: "SquareLock02Icon",
  calendar: "Calendar03Icon", volume: "VolumeHighIcon", text: "TextIcon", stop: "StopIcon", retry: "RepeatIcon",
  external: "LinkSquare02Icon", storage: "HardDriveIcon", queue: "Layers01Icon", ban: "UnavailableIcon",
  user: "UserIcon", book: "BookOpen01Icon", sparkle: "SparklesIcon", down: "ArrowDown01Icon",
};
const attr = (k) => k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
let symbols = [], missing = [];
for (const [id, name] of Object.entries(NAMES)) {
  const f = path.join(src, name + ".js");
  if (!fs.existsSync(f)) { missing.push(name); continue; }
  const data = require(f);
  const inner = data.map(([tag, a]) => "<" + tag + " " + Object.entries(a).filter(([k]) => k !== "key")
    .map(([k, v]) => `${attr(k)}="${v}"`).join(" ") + "/>").join("");
  symbols.push(`<symbol id="i-${id}" viewBox="0 0 24 24" fill="none">${inner}</symbol>`);
}
if (missing.length) { console.error("Eksik:", missing.join(", ")); process.exit(1); }
fs.writeFileSync(out, `<svg xmlns="http://www.w3.org/2000/svg">\n<!-- Hugeicons free (stroke rounded), MIT License, https://hugeicons.com -->\n${symbols.join("\n")}\n</svg>\n`);
console.log(symbols.length, "ikon →", out);
