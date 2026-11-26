"use strict";
// SME OCPPSim web UI: vanilla JS, no build step. Generic chrome (header, menus, footer, theme, i18n)
// comes from the sme-sim-common shell (/sme-sim-common/shell/shell.js); UI strings live in i18n/{en,ja,zh}.js.
// M0 scaffold: shows the (empty) charger list from GET /api/state. Charger panels, frame log and
// Smart Charging views follow in M7.

// ------------------------------------------------------------------ product config (single spot)

const REPO_URL = "https://github.com/smartedge-tech-dev/sme-ocpp-sim";
const PRODUCT = {
  product: "SME OCPPSim",
  tagline: "OCPP CHARGING STATION SIMULATOR",
  license: "MIT",
  year: 2026,
  links: {
    repo: REPO_URL,
    docs: `${REPO_URL}#readme`,
    issues: `${REPO_URL}/issues/new`,
    license: `${REPO_URL}/blob/main/LICENSE`,
  },
  // Charging-post mark; navy parts use currentColor, the accent block stays brand blue.
  logoSvg: `<svg viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
    <path d="M7 34V8h14v26M11 13h6v7h-6zM4 34h24M21 16h4v10a3 3 0 0 0 6 0V21" stroke="currentColor" stroke-width="2.4"/>
    <rect x="25.5" y="6" width="10" height="10" fill="#087def"/></svg>`,
};

const t = Shell.t;
const $ = (id) => document.getElementById(id);

function renderChargers(snap) {
  const list = $("chargerList");
  const chargers = (snap && snap.chargers) || [];
  list.replaceChildren(...chargers.map((c) => {
    const li = document.createElement("li");
    li.textContent = String(c.id ?? "");
    return li;
  }));
  $("chargerEmpty").hidden = chargers.length > 0;
}

async function loadState() {
  try {
    const r = await fetch("/api/state");
    if (r.ok) renderChargers(await r.json());
  } catch { /* server not reachable: keep the placeholder */ }
}

Shell.init({
  ...PRODUCT,
  vendor: Shell.vendors.smartEdge,
  about: [["about.stack", t("about.stack.value")]],
  menus: () => [
    { id: "view", label: "common.menu.view", items: [Shell.themeItem(), Shell.langItem()] },
    Shell.helpMenu(),
  ],
});

fetch("/api/version").then((r) => (r.ok ? r.json() : null))
  .then((v) => { if (v && v.version) Shell.setVersion(v.version); }).catch(() => {});
loadState();
