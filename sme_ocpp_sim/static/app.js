"use strict";
// SME OCPPSim web UI: vanilla JS, no build step. Generic chrome (header, menus, footer, theme, i18n, About, toasts)
// comes from the sme-sim-common shell (/sme-sim-common/shell/shell.js); UI strings live in i18n/{en,ja,zh}.js.
//
// Data flow: GET /api/state once, then the /ws push stream (web.py docstring): "snapshot" first, then
// "charger_state" / "values" / "frame" with push_seq; a full push replaces the client state, a gap in push_seq
// refetches the lost frames (GET /api/frames?since=; frames are de-duplicated by seq and kept sorted, so a late
// backfill is merged in). Charger rows and the selected charger's connector cards are patched in place (a card is
// rebuilt only when its state changed) so the list stays smooth with a few hundred chargers. Settings go through
// the JSON routes /api/csms-defaults and /api/chargers/{id}/settings (validated and reloaded server side); the
// configuration key table shows the run-time keys (/api/chargers/{id}/configuration). An OCPP 2.0.1 charger shows
// its Device Model variables instead (same route, read-only: no Web API route changes a variable) and gets the
// external-limit control of the Smart Charging panel (POST /api/chargers/{id}/external-limit); its connectors
// are EVSEs (one connector each), so the panel's selector is labelled EVSE.
//
// Global entry points (plan §9, used for screenshots / docs): openChargerDialog(id), openSettingsDialog(id),
// openProfileDialog(id), openFrameDetail(seq), openAddChargerDialog(); About is Shell.openAbout(). The Mini CSMS
// panel lives in csms.js (loaded after this file): openCsmsPanel(stationId).

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

// Fault codes depend on the charger's OCPP version: the fault dialog loads them from GET /api/chargers/{id} each
// time it opens (protocol values, not translated), so a version change in the settings is picked up.
// Preselected fault code: 1.6 OtherError, 2.0.1 Other (ReasonEnumType); else the first code.
const DEFAULT_FAULTS = ["OtherError", "Other"];
// Versions with a Device Model (variables instead of configuration keys) and external limits.
const DEVICE_MODEL_VERSIONS = new Set(["2.0.1"]);
const hasDeviceModel = (c) => !!c && DEVICE_MODEL_VERSIONS.has(c.ocpp_version);
const TIME_SCALES = [1, 2, 5, 10, 60];
const MAX_FRAMES = 2000;          // frames kept client side
const MAX_FRAME_ROWS = 500;       // frame rows in the DOM
const MAX_HISTORY = 3600;         // chart samples per connector
// Chart window (wall clock, the clock charging schedules run on; time_scale only speeds up the power model).
// The history is kept client-side only, from the moment the page started sampling, so it does not survive a
// reload. The window therefore zooms with the history: the past span grows from CHART_MIN_PAST_MS up to
// CHART_PAST_MS, and the future span is twice the past span, clamped to [CHART_MIN_FUTURE_MS, CHART_FUTURE_MS].
const CHART_PAST_MS = 20 * 60e3, CHART_FUTURE_MS = 40 * 60e3;
const CHART_MIN_PAST_MS = 30e3, CHART_MIN_FUTURE_MS = 90e3;
const CHART_TICKS_MS = [10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3];

const t = Shell.t;
const tk = (prefix, value) => t(`${prefix}.${value}`);   // enumerated keys (status.*, conn.*)
const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c);
  return el;
}

const kw = (w) => (w == null ? "-" : (w / 1000).toFixed(2));
const kwh = (wh) => (wh == null ? "-" : (wh / 1000).toFixed(3));
const pct = (p) => (p == null ? "-" : `${Math.round(p)} %`);
const hhmmss = (ms) => new Date(ms).toLocaleTimeString([], { hour12: false });
const pretty = (raw) => { try { return JSON.stringify(JSON.parse(raw), null, 2); } catch { return String(raw); } };

// ------------------------------------------------------------------ state

const S = {
  chargers: new Map(),   // id -> snapshot (values patched in by "values" pushes)
  rows: new Map(),       // id -> {tr, badge, version, count, power, chk}
  sel: null,             // selected charger id
  checked: new Set(),
  frames: [],            // sorted by seq, newest last
  frameSet: new Set(),   // seqs in S.frames (de-duplication)
  frameSeq: 0,           // highest frame seq seen
  hist: new Map(),       // "id/connector" -> [{t, p, soc, lim}]
  composite: null,       // {key, start(ms), duration(s|null), periods}
  running: true,
  timeScale: 1,
  stats: null,
};

// ------------------------------------------------------------------ API helpers

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.body = JSON.stringify(body);
    opt.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(url, opt);
  const text = await r.text();
  let data = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* plain text */ }
  if (!r.ok) {
    const msg = data && typeof data === "object" ? (data.error || data.detail) : data;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg || r.status));
  }
  return data;
}
const fail = (e) => Shell.toast(String((e && e.message) || e), "error");
const chargerUrl = (id, what) => `/api/chargers/${enc(id)}/${what}`;

async function op(id, name, body) {
  try { await api("POST", chargerUrl(id, name), body || {}); } catch (e) { fail(e); }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ------------------------------------------------------------------ charger list (rows patched in place)

function connState(c) {
  if (!c.connected && c.gave_up) return ["gave_up", "b-bad"];   // held by the engine until Connect
  if (c.enabled === false) return ["held", "b-off"];
  if (!c.connected) return c.last_error ? ["error", "b-bad"] : ["connecting", "b-off"];
  const reg = String(c.registration || "").toLowerCase();
  if (reg === "accepted") return ["online", "b-ok"];
  if (reg === "pending" || reg === "rejected") return [reg, "b-warn"];
  return ["booting", "b-warn"];
}
// last_error is a string or an object ({type, message, ...}) depending on where the error came from.
const errText = (e) => (e == null ? "" : typeof e === "string" ? e
  : e.message || e.detail || [e.type, e.reason].filter(Boolean).join(": ") || JSON.stringify(e));
const totalPower = (c) => (c.connectors || []).reduce((s, x) => s + (x.power_w || 0), 0);

function setBadge(el, c) {
  const [key, cls] = connState(c);
  el.className = `badge ${cls}`;
  el.textContent = tk("conn", key);
  el.title = key === "gave_up"
    ? t(c.gave_up.reason === "boot_max_retries" ? "conn.gave_up.title_boot" : "conn.gave_up.title",
      { attempts: c.gave_up.attempts, kind: c.gave_up.last_kind || "?" })
    : errText(c.last_error);
  if (c.impaired) {  // impairments are on: mark the badge so a CSMS timeout or a drop is not a surprise
    el.textContent += " !";
    el.title = [el.title, t("imp.badge")].filter(Boolean).join(" / ");
  }
}

function makeRow(id) {
  const chk = h("input", { type: "checkbox", "aria-label": id, onclick: (e) => e.stopPropagation(),
    onchange: (e) => { if (e.target.checked) S.checked.add(id); else S.checked.delete(id); syncSelAll(); } });
  const r = { chk, badge: h("span", { class: "badge" }), version: h("td"), count: h("td", { class: "num" }),
    power: h("td", { class: "num" }) };
  r.tr = h("tr", { "data-id": id, onclick: () => selectCharger(id) },
    h("td", null, chk), h("td", null, id), h("td", null, r.badge), r.version, r.count, r.power);
  S.rows.set(id, r);
  return r;
}

function patchRow(c) {
  const r = S.rows.get(c.id) || makeRow(c.id);
  setBadge(r.badge, c);
  r.version.textContent = c.ocpp_version || "";
  r.count.textContent = String((c.connectors || []).length);
  patchPower(c);
  r.tr.classList.toggle("sel", c.id === S.sel);
  r.tr.hidden = !matchesFilter(c);
  return r;
}
function patchPower(c) {
  const r = S.rows.get(c.id);
  if (r) r.power.textContent = kw(totalPower(c));
}

function matchesFilter(c) {
  const q = $("chargerFilter").value.trim().toLowerCase();
  if (!q) return true;
  return [c.id, c.ocpp_version, tk("conn", connState(c)[0])].some((s) => String(s || "").toLowerCase().includes(q))
    || (c.connectors || []).some((x) => String(x.status || "").toLowerCase().includes(q));
}

// A full list (snapshot / full charger_state): rows are reused, missing chargers removed, order follows the list.
function replaceChargers(list) {
  const keep = new Set(list.map((c) => c.id));
  for (const id of [...S.chargers.keys()]) {
    if (keep.has(id)) continue;
    S.chargers.delete(id); S.checked.delete(id);
    const r = S.rows.get(id); if (r) r.tr.remove();
    S.rows.delete(id);
  }
  const body = $("chargerRows");
  for (const c of list) { S.chargers.set(c.id, c); body.append(patchRow(c).tr); }
  afterListChange();
  if (S.sel && !S.chargers.has(S.sel)) selectCharger(null);
  else if (!S.sel && list.length) selectCharger(list[0].id);
  else if (S.sel) renderDetail();
}

function updateChargers(list) {
  const body = $("chargerRows");
  let added = false;
  for (const c of list) {
    if (!S.rows.has(c.id)) { body.append(patchRow(c).tr); added = true; } else patchRow(c);
    S.chargers.set(c.id, c);
    if (c.id === S.sel) renderDetail();
  }
  if (added) afterListChange();
}

function afterListChange() {
  $("chargerEmpty").hidden = S.chargers.size > 0;
  syncCount();
  syncSelAll();
  syncChargerFilter();
}
function syncCount() {
  const st = S.stats;
  $("chargerCount").textContent = st ? t("chargers.count_online", { n: st.chargers, online: st.online ?? st.connected })
    : t("chargers.count", { n: S.chargers.size });
}
const visibleIds = () => [...S.rows].filter(([, r]) => !r.tr.hidden).map(([id]) => id);
function syncSelAll() {
  const all = $("selAll"), vis = visibleIds();
  all.checked = vis.length > 0 && vis.every((id) => S.checked.has(id));
  all.indeterminate = S.checked.size > 0 && !all.checked;
}

const VALUE_KEYS = ["power_w", "meter_wh", "soc_pct"];
function applyValues(values, full) {
  for (const [id, conns] of Object.entries(values || {})) {
    const c = S.chargers.get(id);
    if (!c) continue;
    for (const x of c.connectors || []) {
      const v = conns.find((k) => k.id === x.id);
      if (v) Object.assign(x, v);
      else if (full) for (const k of VALUE_KEYS) x[k] = null;   // a full push replaces every value
    }
    patchPower(c);
    if (id === S.sel) { patchCards(c); sample(c); }
  }
}

// Checked chargers, or (nothing checked) the rows the filter shows, after a confirmation for more than one.
async function bulk(action) {
  const ids = S.checked.size ? [...S.checked] : visibleIds();
  if (!ids.length) return;
  if (!S.checked.size && ids.length > 1
    && !window.confirm(t(action === "connect" ? "bulk.confirm.connect" : "bulk.confirm.disconnect", { n: ids.length }))) return;
  try {
    await api("POST", `/api/chargers/bulk/${action}`, { ids });
    Shell.toast(t(action === "connect" ? "bulk.done.connect" : "bulk.done.disconnect", { n: ids.length }), "ok");
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ charger detail

function selectCharger(id, scroll) {
  if (id != null && !S.chargers.has(id)) return;   // unknown id: keep the current selection
  const prev = S.sel && S.rows.get(S.sel);
  if (prev) prev.tr.classList.remove("sel");
  S.sel = id == null ? null : id;
  const r = S.sel && S.rows.get(S.sel);
  if (r) { r.tr.classList.add("sel"); if (scroll) r.tr.scrollIntoView({ block: "nearest" }); }
  S.composite = null;
  renderDetail(true);
  if (S.sel) { loadSmartCharging(); loadKeys(); }
  if (scroll) $("detail").scrollIntoView({ block: "nearest" });
}

function statusClass(s) {
  if (s === "Available") return "b-ok";
  if (s === "Faulted") return "b-bad";
  if (s === "Unavailable") return "b-off";
  return "b-warn";
}

function renderDetail(fresh) {
  const c = S.sel && S.chargers.get(S.sel);
  $("detailEmpty").hidden = !!c;
  $("detailBody").hidden = !c;
  if (!c) return;
  $("dTitle").textContent = c.id;
  setBadge($("dBadge"), c);
  const info = [`OCPP ${c.ocpp_version}`];
  if (c.registration) info.push(t("detail.registration", { s: c.registration }));
  if (c.last_heartbeat) info.push(t("detail.heartbeat", { t: hhmmss(Date.parse(c.last_heartbeat)) }));
  if (c.tx_queue) info.push(t("detail.tx_queue", { n: c.tx_queue }));
  if (c.local_list_version) info.push(t("detail.local_list", { n: c.local_list_version }));
  if (c.firmware && c.firmware.status) info.push(t("detail.firmware", { s: c.firmware.status }));
  if (c.log && c.log.status) info.push(t("detail.log", { s: c.log.status }));
  if (c.security_profile != null) info.push(t("detail.security_profile", { n: c.security_profile }));
  const sec = c.security || {};
  if (sec.client_certificate) info.push(t("detail.client_cert", { s: sec.client_certificate }));
  $("dInfo").textContent = info.join(" · ");
  $("dError").textContent = c.last_error ? t("detail.last_error", { e: errText(c.last_error) }) : "";
  $("dConnect").disabled = c.enabled !== false;
  $("dDisconnect").disabled = c.enabled === false;
  renderVersionParts(c);
  renderConnectors(c, fresh);
  drawChart();
}

// The parts that depend on the OCPP version: keys or variables, the external limit, "Connector" or "EVSE".
function renderVersionParts(c) {
  const dm = hasDeviceModel(c);
  const box = $("detailBody");
  if (box.dataset.sel !== c.id) {   // another charger: drop the rows of the previous one until loadKeys answers
    box.dataset.sel = c.id;
    $("keyRows").replaceChildren();
    $("varRows").replaceChildren();
  }
  if (box.dataset.dm === String(dm)) return;
  box.dataset.dm = String(dm);
  $("keysBox").open = false;
  $("varsBox").open = false;
  $("keysBox").hidden = dm;
  $("varsBox").hidden = !dm;
  $("extForm").hidden = !dm;
  $("extHint").hidden = !dm;
  const label = $("scConnectorLabel");
  label.dataset.i18n = dm ? "sc.evse" : "sc.connector";
  label.textContent = t(label.dataset.i18n);
  if (dm) { $("keyRows").replaceChildren(); } else { $("varRows").replaceChildren(); }
}

// The cards and the connector select are rebuilt only when the charger or its connector set changes; otherwise a
// card is replaced only when its state (status, plug, transaction, error) changed.
const cardSig = (x) => JSON.stringify([x.status, x.plugged, x.error_code, x.transaction, x.pending_tag,
  x.reservation]);
function renderConnectors(c, fresh) {
  const box = $("connectors"), conns = c.connectors || [];
  const sig = JSON.stringify([c.id, conns.map((x) => x.id)]);
  if (fresh || box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.replaceChildren(...conns.map((x) => card(c, x)));
    const sel = $("scConnector");
    const want = fresh ? "1" : sel.value;
    sel.replaceChildren(...conns.map((x) => h("option", { value: x.id }, String(x.id))));
    sel.value = conns.some((x) => String(x.id) === want) ? want : sel.options[0] && sel.options[0].value;
    const ext = $("extTarget"), extWant = fresh ? "0" : ext.value;
    ext.replaceChildren(h("option", { value: "0" }, t("sc.ext.station")),
      ...conns.map((x) => h("option", { value: x.id }, t("sc.ext.evse", { n: x.id }))));
    ext.value = conns.some((x) => String(x.id) === extWant) ? extWant : "0";
    return;
  }
  for (const x of conns) {
    const el = box.querySelector(`.card[data-conn="${CSS.escape(String(x.id))}"]`);
    if (el && el.dataset.sig === cardSig(x)) continue;
    const next = card(c, x);
    if (el) el.replaceWith(next); else box.append(next);
  }
}

function card(c, x) {
  const tx = x.transaction;
  const tag = (tx && (tx.id_tag || tx.idTag)) || x.pending_tag;
  const txId = tx && (tx.tx_id ?? tx.transaction_id ?? tx.id ?? tx.transactionId);
  const btn = (key, fn, opts) => h("button", { type: "button", class: `btn small${opts && opts.danger ? " danger" : ""}`,
    disabled: opts && opts.disabled, onclick: fn }, t(key));
  const id = c.id, conn = x.id;
  const kv = (key, val, k) => [h("dt", null, t(key)), h("dd", k ? { "data-k": k } : null, val)];
  const bar = h("i", { "data-k": "socbar" });
  bar.style.width = `${x.soc_pct || 0}%`;
  return h("div", { class: "card", "data-conn": conn, "data-sig": cardSig(x) },
    h("div", { class: "card-head" }, t("detail.connector", { n: conn }),
      h("span", { class: `badge ${statusClass(x.status)}` }, tk("status", x.status)),
      x.plugged ? h("span", { class: "muted" }, t("detail.plugged")) : null),
    h("div", { class: "soc" }, bar),
    h("dl", { class: "card-kv" },
      x.error_code && x.error_code !== "NoError" ? kv("detail.error_code", x.error_code) : null,
      kv("detail.id_tag", tag || "-"),
      kv("detail.transaction", txId != null ? String(txId) : "-"),
      x.reservation ? kv("detail.reservation", `#${x.reservation.id} ${x.reservation.id_tag}`) : null,
      kv("detail.power", `${kw(x.power_w)} kW`, "power"),
      kv("detail.energy", `${kwh(x.meter_wh)} kWh`, "energy"),
      kv("detail.soc", pct(x.soc_pct), "soc")),
    h("div", { class: "card-ops" },
      btn("op.plug", () => op(id, "plug", { connector: conn }), { disabled: x.plugged }),
      btn("op.unplug", () => op(id, "unplug", { connector: conn }), { disabled: !x.plugged }),
      btn("op.swipe", () => tagThen(id, conn, "swipe")),
      btn("op.start", () => tagThen(id, conn, "start"), { disabled: !!tx }),
      btn("op.stop", () => op(id, "stop", { connector: conn }), { disabled: !tx }),
      x.status === "Faulted"
        ? btn("op.clear_fault", () => op(id, "clear-fault", { connector: conn }))
        : btn("op.fault", () => faultThen(id, conn), { danger: true })));
}

// Values pushes only touch the numbers of the selected charger's cards.
function patchCards(c) {
  for (const x of c.connectors || []) {
    const el = document.querySelector(`#connectors .card[data-conn="${CSS.escape(String(x.id))}"]`);
    if (!el) continue;
    const set = (k, v) => { const e = el.querySelector(`[data-k="${k}"]`); if (e) e.textContent = v; };
    set("power", `${kw(x.power_w)} kW`);
    set("energy", `${kwh(x.meter_wh)} kWh`);
    set("soc", pct(x.soc_pct));
    const bar = el.querySelector('[data-k="socbar"]');
    if (bar) bar.style.width = `${x.soc_pct || 0}%`;
  }
}

function ask(dlg) {
  return new Promise((resolve) => {
    dlg.returnValue = "";
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "default"), { once: true });
    dlg.showModal();
  });
}

async function tagThen(id, conn, action) {
  $("tagTitle").textContent = t(action === "swipe" ? "op.swipe.title" : "op.start.title", { id, n: conn });
  const input = $("tagValue");
  input.value = input.value || "TAG-0001";
  checkTag();
  if (!(await ask($("dlgTag")))) return;
  const tag = input.value.trim();
  if (tag) op(id, action, { connector: conn, id_tag: tag });
  else Shell.toast(t("op.id_tag.required"), "error");
}
// Form validation blocks the OK button while the tag is empty or blank.
function checkTag() {
  const input = $("tagValue");
  input.setCustomValidity(input.value.trim() ? "" : t("op.id_tag.required"));
}

async function faultThen(id, conn) {
  let codes;
  try {
    codes = (await api("GET", `/api/chargers/${enc(id)}`)).fault_codes || [];
  } catch (e) { fail(e); return; }
  $("faultCode").replaceChildren(...codes.map((c) => h("option", { value: c }, c)));
  const def = DEFAULT_FAULTS.find((c) => codes.includes(c));
  if (def) $("faultCode").value = def;
  $("faultTitle").textContent = t("op.fault.title", { id, n: conn });
  if (await ask($("dlgFault"))) op(id, "fault", { connector: conn, error_code: $("faultCode").value });
}

async function loadKeys() {
  const id = S.sel;
  const rows = $("keyRows");
  if (!id) return;
  try {
    const [conf, set] = await Promise.all([api("GET", chargerUrl(id, "configuration")), api("GET", chargerUrl(id, "settings"))]);
    if (id !== S.sel) return;
    if (conf.variables) {
      renderVariables(conf.variables);
    } else {
      const keys = conf.keys || [];
      rows.replaceChildren(...(keys.length ? keys.map((k) => h("tr", null, h("td", null, k.key), h("td", null, k.value ?? ""),
      h("td", { class: "muted" }, t(k.readonly ? "keys.readonly" : "keys.readwrite"))))
      : [h("tr", null, h("td", { colspan: 3, class: "muted" }, t("keys.none")))]));
    }
    const tags = set.id_tags || [];
    $("tagList").replaceChildren(...tags.map((v) => h("option", { value: v })));
    if (tags.length) $("tagValue").value = tags[0];
  } catch (e) {
    if (id !== S.sel) return;
    const err = (n) => [h("tr", null, h("td", { colspan: n, class: "err" }, String(e.message)))];
    if (hasDeviceModel(S.chargers.get(id))) $("varRows").replaceChildren(...err(5));
    else rows.replaceChildren(...err(3));
  }
}

// One row per Device Model variable: the Actual value (WriteOnly values are null), the other attributes below it.
const MUTABILITY = { ReadOnly: "keys.readonly", ReadWrite: "keys.readwrite", WriteOnly: "vars.writeonly" };
function renderVariables(vars) {
  const comp = (v) => [v.component, v.component_instance && `[${v.component_instance}]`,
    v.evse_id != null && ` EVSE ${v.evse_id}`, v.connector_id != null && `, connector ${v.connector_id}`].filter(Boolean).join("");
  const name = (v) => v.variable + (v.variable_instance ? `[${v.variable_instance}]` : "");
  const row = (v) => {
    const a = v.attributes || {};
    const value = h("td", null, v.mutability === "WriteOnly" ? h("span", { class: "muted" }, t("vars.hidden")) : (a.Actual ?? ""));
    const more = ["Target", "MinSet", "MaxSet"].filter((k) => k in a).map((k) => `${k}: ${a[k] ?? ""}`);
    if (more.length) value.append(h("div", { class: "muted" }, more.join(" · ")));
    const type = [v.data_type, v.unit].filter(Boolean).join(", ");
    return h("tr", null, h("td", null, comp(v)), h("td", null, name(v)), value, h("td", { class: "attr" }, type),
      h("td", { class: "muted" }, t(MUTABILITY[v.mutability] || "keys.readwrite")));
  };
  $("varRows").replaceChildren(...(vars.length ? vars.map(row)
    : [h("tr", null, h("td", { colspan: 5, class: "muted" }, t("vars.none")))]));
}

// ------------------------------------------------------------------ Smart Charging (profiles + one time axis)

const histKey = () => `${S.sel}/${$("scConnector").value}`;

function sample(c) {
  const conn = Number($("scConnector").value);
  const x = (c.connectors || []).find((k) => k.id === conn);
  if (!x) return;
  const key = `${c.id}/${conn}`;
  const arr = S.hist.get(key) || [];
  arr.push({ t: Date.now(), p: x.power_w || 0, soc: x.soc_pct, lim: limitAt(Date.now()) });
  if (arr.length > MAX_HISTORY) arr.splice(0, arr.length - MAX_HISTORY);
  S.hist.set(key, arr);
  scheduleDraw();
}

function limitAt(ms) {
  const cs = S.composite;
  if (!cs || cs.key !== histKey() || ms < cs.start) return null;
  if (cs.duration != null && ms > cs.start + cs.duration * 1000) return null;
  let lim = null;
  for (const p of cs.periods) if (cs.start + p.start_period * 1000 <= ms) lim = p.limit;
  return lim;
}

async function loadSmartCharging() {
  const id = S.sel, conn = $("scConnector").value;
  if (!id || !conn) return;
  try {
    const [prof, cs] = await Promise.all([
      api("GET", `${chargerUrl(id, "profiles")}`),
      api("GET", `${chargerUrl(id, "composite")}?connector=${enc(conn)}&duration=${CHART_FUTURE_MS / 1000}&unit=W`),
    ]);
    if (id !== S.sel || conn !== $("scConnector").value) return;   // stale answer
    S.composite = { key: `${id}/${conn}`, start: Date.parse(cs.start || cs.schedule_start), duration: cs.duration,
      periods: cs.periods || [] };
    renderProfileList(prof.profiles || []);
    const c = S.chargers.get(id);
    if (c) sample(c);
  } catch (e) {
    if (id === S.sel && conn === $("scConnector").value) $("scList").replaceChildren(h("li", { class: "err" }, String(e.message)));
  }
}

// External (EMS) limit of a 2.0.1 charger: limit_w null removes it. The answer has no data; the profile list
// and the chart are refreshed so the new ChargingStationExternalConstraints profile shows at once.
async function setExternalLimit(limitW) {
  const id = S.sel;
  if (!id) return;
  try {
    await api("POST", chargerUrl(id, "external-limit"), { connector: Number($("extTarget").value), limit_w: limitW });
    Shell.toast(t(limitW == null ? "sc.ext.cleared" : "sc.ext.applied"), "ok");
  } catch (e) { fail(e); return; }
  S.composite = null;
  loadSmartCharging();
}

function profileLine(p) {
  const sch = p.schedule || {};
  return t("sc.profile.line", { id: p.profile_id, purpose: p.purpose, kind: p.kind, stack: p.stack_level,
    conn: p.connector_id, unit: sch.unit, n: (sch.periods || []).length });
}
function renderProfileList(list) {
  $("scList").replaceChildren(...(list.length
    ? list.map((p) => h("li", null, profileLine(p)))
    : [h("li", { class: "muted" }, t("sc.no_profiles"))]));
}

let drawPending = false;
function scheduleDraw() {
  if (drawPending) return;
  drawPending = true;
  requestAnimationFrame(() => { drawPending = false; drawChart(); });
}

function drawChart() {
  const box = $("scChart");
  if (!S.sel) { box.replaceChildren(); return; }
  const W = 800, H = 200, L = 46, R = 40, T = 10, B = 22;
  const now = Date.now();
  const all = S.hist.get(histKey()) || [];
  const past = Math.min(CHART_PAST_MS, Math.max(CHART_MIN_PAST_MS, all.length ? now - all[0].t : 0));
  const t0 = now - past, t1 = now + Math.min(CHART_FUTURE_MS, Math.max(CHART_MIN_FUTURE_MS, 2 * past));
  const hist = all.filter((s) => s.t >= t0);
  const cs = S.composite && S.composite.key === histKey() ? S.composite : null;
  // Future limit steps from the composite schedule.
  const steps = [];
  if (cs) {
    const end = cs.duration != null ? Math.min(t1, cs.start + cs.duration * 1000) : t1;
    cs.periods.forEach((p, i) => {
      const a = Math.max(now, cs.start + p.start_period * 1000);
      const nx = cs.periods[i + 1];
      const b = Math.min(end, nx ? cs.start + nx.start_period * 1000 : end);
      if (b > a) steps.push([a, b, p.limit]);
    });
  }
  const c = S.chargers.get(S.sel);
  const maxW = Math.max(1000, ...hist.map((s) => Math.max(s.p, s.lim || 0)), ...steps.map((s) => s[2]),
    (c && totalPower(c)) || 0) * 1.1;
  const x = (ms) => L + ((ms - t0) / (t1 - t0)) * (W - L - R);
  const y = (w) => T + (1 - w / maxW) * (H - T - B);
  const ys = (soc) => T + (1 - soc / 100) * (H - T - B);
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text != null) e.textContent = text;
    return e;
  };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": t("sc.chart.aria") });
  for (let i = 0; i <= 4; i++) {
    const w = (maxW / 4) * i, yy = y(w);
    svg.append(el("line", { class: "grid", x1: L, x2: W - R, y1: yy, y2: yy }),
      el("text", { class: "axis", x: L - 4, y: yy + 3, "text-anchor": "end" }, (w / 1000).toFixed(1)),
      el("text", { class: "axis", x: W - R + 4, y: yy + 3 }, `${25 * i}%`));
  }
  // Wall-clock tick labels: the smallest round step giving at most 8 labels; seconds shown below one minute.
  const step = CHART_TICKS_MS.find((s) => (t1 - t0) / s <= 8) || CHART_TICKS_MS[CHART_TICKS_MS.length - 1];
  const fmt = step < 60e3 ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
    : { hour: "2-digit", minute: "2-digit", hour12: false };
  for (let ms = Math.ceil(t0 / step) * step; ms <= t1; ms += step) {
    svg.append(el("text", { class: "axis", x: x(ms), y: H - 6, "text-anchor": "middle" },
      new Date(ms).toLocaleTimeString([], fmt)));
  }
  svg.append(el("text", { class: "axis", x: 2, y: T + 3 }, "kW"),
    el("line", { class: "now", x1: x(now), x2: x(now), y1: T, y2: H - B }));
  // Limit: recorded past values, then the composite steps (a step path).
  let dl = "";
  let prevY = null;
  for (const s of hist) {
    if (s.lim == null) { prevY = null; continue; }
    dl += prevY == null ? `M${x(s.t)},${y(s.lim)}` : `H${x(s.t)}V${y(s.lim)}`;
    prevY = s.lim;
  }
  for (const [a, b, lim] of steps) dl += `${dl ? "L" : "M"}${x(a)},${y(lim)}H${x(b)}`;
  if (dl) svg.append(el("path", { class: "l-limit", d: dl }));
  if (hist.length) {
    svg.append(el("path", { class: "l-power", d: hist.map((s, i) => `${i ? "L" : "M"}${x(s.t)},${y(s.p)}`).join("") }));
    const soc = hist.filter((s) => s.soc != null);
    if (soc.length) svg.append(el("path", { class: "l-soc", d: soc.map((s, i) => `${i ? "L" : "M"}${x(s.t)},${ys(s.soc)}`).join("") }));
  }
  box.replaceChildren(svg);
}

// ------------------------------------------------------------------ frame log

function frameFilter() {
  return { charger: $("fCharger").value, action: $("fAction").value.trim(), dir: $("fDir").value };
}
function frameMatches(f, flt) {
  return (!flt.charger || f.charger === flt.charger) && (!flt.dir || f.direction === flt.dir)
    && (!flt.action || String(f.action || "").toLowerCase().includes(flt.action.toLowerCase()));
}

function frameRow(f) {
  const dir = f.direction === "tx" ? "→" : "←";
  return h("tr", { "data-seq": f.seq, onclick: (e) => toggleFrame(e.currentTarget, f), ondblclick: () => openFrameDetail(f.seq) },
    h("td", { class: "num" }, String(f.seq)), h("td", null, hhmmss(f.ts * 1000)), h("td", null, f.charger),
    h("td", { class: `d-${f.direction}`, title: tk("frames.dir", f.direction) }, dir), h("td", null, f.message_type || "?"),
    h("td", null, f.action || ""), h("td", { class: "payload" }, f.note ? `${f.note} ${f.raw}` : f.raw));
}

function toggleFrame(tr, f) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("xrow")) { next.remove(); return; }
  tr.after(h("tr", { class: "xrow" }, h("td", { colspan: 7 },
    h("pre", { class: "json" }, pretty(f.raw)),
    h("div", { class: "card-ops" },
      h("button", { type: "button", class: "btn small", onclick: () => copyText(f.raw) }, t("frames.copy")),
      h("button", { type: "button", class: "btn small", onclick: () => openFrameDetail(f.seq) }, t("frames.details"))))));
}

// navigator.clipboard needs a secure origin (https / localhost); elsewhere (http://<lan-ip>) use execCommand.
function copyText(text) {
  const ok = () => Shell.toast(t("frames.copied"), "ok");
  const legacy = () => {
    const ta = h("textarea", { readonly: true });
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    (document.querySelector("dialog[open]") || document.body).append(ta);   // a modal dialog makes the body inert
    ta.select();
    let done = false;
    try { done = document.execCommand("copy"); } catch { /* reported below */ }
    ta.remove();
    if (done) ok(); else Shell.toast(t("frames.copy_failed"), "error");
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(ok, legacy);
  else legacy();
}

function addFrames(list) {
  const fresh = list.filter((f) => !S.frameSet.has(f.seq)).sort((a, b) => a.seq - b.seq);
  if (!fresh.length) return;
  const tail = S.frames.length ? S.frames[S.frames.length - 1].seq : 0;
  const atTail = fresh[0].seq > tail;
  for (const f of fresh) { S.frameSet.add(f.seq); S.frames.push(f); }
  if (!atTail) S.frames.sort((a, b) => a.seq - b.seq);   // backfill that arrived after newer frames
  S.frameSeq = Math.max(S.frameSeq, S.frames[S.frames.length - 1].seq);
  if (S.frames.length > MAX_FRAMES) for (const f of S.frames.splice(0, S.frames.length - MAX_FRAMES)) S.frameSet.delete(f.seq);
  if (!atTail) { renderFrames(); return; }
  list = fresh;
  const flt = frameFilter();
  const body = $("frameRows");
  const rows = list.filter((f) => frameMatches(f, flt)).map(frameRow);
  if (!rows.length) return;
  body.append(...rows);
  while (body.rows.length > MAX_FRAME_ROWS) body.rows[0].remove();
  if ($("fFollow").checked) $("frameWrap").scrollTop = $("frameWrap").scrollHeight;
}

function renderFrames() {
  const flt = frameFilter();
  $("frameRows").replaceChildren(...S.frames.filter((f) => frameMatches(f, flt)).slice(-MAX_FRAME_ROWS).map(frameRow));
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(flt)) if (v) q.set(k, v);
  $("fExport").href = `/api/frames.jsonl${q.toString() ? `?${q}` : ""}`;
  if ($("fFollow").checked) $("frameWrap").scrollTop = $("frameWrap").scrollHeight;
}

function syncChargerFilter() {
  const sel = $("fCharger"), cur = sel.value;
  const ids = [...S.chargers.keys()];
  if (sel.options.length - 1 === ids.length && ids.every((id, i) => sel.options[i + 1].value === id)) return;
  sel.replaceChildren(h("option", { value: "" }, t("frames.all_chargers")), ...ids.map((id) => h("option", { value: id }, id)));
  sel.value = ids.includes(cur) ? cur : "";
}

async function fetchFrames(since) {
  try {
    const r = await api("GET", `/api/frames?since=${since}&limit=${MAX_FRAMES}`);
    addFrames(r.frames || []);
  } catch { /* next push retries */ }
}

// ------------------------------------------------------------------ dialogs (global entry points, plan §9)

function openChargerDialog(id) { if (S.chargers.has(id)) selectCharger(id, true); }

function versionOptions(sel, withDefault) {
  sel.replaceChildren(
    ...(withDefault ? [h("option", { value: "" }, t("settings.default"))] : []),
    h("option", { value: "1.6" }, "1.6"),
    h("option", { value: "2.0.1" }, "2.0.1"),
    h("option", { value: "2.1", disabled: true }, t("settings.version.v21")));
}

// The dialog state: the csms_defaults block and the charger settings as loaded (masked password).
const SET = { id: null, defaults: null, charger: null };
async function openSettingsDialog(id) {
  id = id === undefined ? S.sel : id;
  $("setErr").textContent = "";
  SET.id = id && S.chargers.has(id) ? id : null;
  try {
    [SET.defaults, SET.charger] = await Promise.all([api("GET", "/api/csms-defaults"),
      SET.id ? api("GET", chargerUrl(SET.id, "settings")) : null]);
  } catch (e) { fail(e); return; }
  const d = SET.defaults;
  $("setUrl").value = d.url || "";
  $("setVersion").value = d.ocpp_version || "1.6";
  $("setProfile").value = String(d.security_profile ?? 0);
  $("setChargerBox").hidden = !SET.id;
  const c = SET.charger;
  if (c) {
    $("setChargerLegend").textContent = t("settings.charger", { id: SET.id });
    $("setCUrl").value = c.csms_url || "";
    $("setCUrl").placeholder = (c.effective && c.effective.csms_url) || "";
    $("setCVersion").value = c.ocpp_version || "";
    $("setPassword").value = c.password || "";   // the mask "********" keeps the stored password (web.py)
    $("setCa").value = c.ca_file || "";
    $("setCert").value = c.client_cert_file || "";
    $("setKey").value = c.client_key_file || "";
  }
  $("dlgSettings").showModal();
}

// Only changed fields are sent: PATCH for the charger, PUT of the whole (loaded + edited) csms_defaults block.
async function applySettings() {
  const c = SET.charger;
  if (SET.id && c) {
    const want = { csms_url: $("setCUrl").value.trim() || null, ocpp_version: $("setCVersion").value || null,
      password: $("setPassword").value || null, ca_file: $("setCa").value.trim() || null,
      client_cert_file: $("setCert").value.trim() || null, client_key_file: $("setKey").value.trim() || null };
    const patch = {};
    for (const [k, v] of Object.entries(want)) if (v !== (c[k] ?? null)) patch[k] = v;
    if (Object.keys(patch).length) SET.charger = await api("PATCH", chargerUrl(SET.id, "settings"), patch);
  }
  const d = SET.defaults;
  const next = { ...d, url: $("setUrl").value.trim() || null, ocpp_version: $("setVersion").value,
    security_profile: Number($("setProfile").value) };
  if (["url", "ocpp_version", "security_profile"].some((k) => next[k] !== d[k])) {
    SET.defaults = await api("PUT", "/api/csms-defaults", next);
  }
  if (SET.id === S.sel) loadKeys();
}

async function openProfileDialog(id) {
  id = id === undefined ? S.sel : id;
  if (!id) return;
  $("profTitle").textContent = t("profiles.title", { id });
  const body = $("profBody");
  body.replaceChildren(h("p", { class: "muted" }, "…"));
  $("dlgProfiles").showModal();
  try {
    const list = (await api("GET", chargerUrl(id, "profiles"))).profiles || [];
    body.replaceChildren(...(list.length
      ? list.map((p) => h("div", null, h("p", null, profileLine(p)), h("pre", { class: "json" }, JSON.stringify(p, null, 2))))
      : [h("p", { class: "muted" }, t("sc.no_profiles"))]));
  } catch (e) { body.replaceChildren(h("p", { class: "err" }, String(e.message))); }
}

async function openFrameDetail(seq) {
  const n = Number(seq);
  let f = S.frames.find((x) => x.seq === n);
  if (!f && n >= 1) {
    try { f = ((await api("GET", `/api/frames?since=${n - 1}&until=${n}&limit=1`)).frames || []).find((x) => x.seq === n); }
    catch (e) { fail(e); return; }
  }
  if (!f) { Shell.toast(t("frames.not_found", { seq }), "error"); return; }
  $("frameTitle").textContent = t("frames.detail.title", { seq: f.seq, charger: f.charger,
    dir: tk("frames.dir", f.direction), action: f.action || f.message_type || "?" });
  $("frameJson").textContent = pretty(f.raw);
  $("frameCopy").onclick = () => copyText(f.raw);
  $("dlgFrame").showModal();
}

// Add charger(s): plain defaults or a charger template, one or N at once (POST /api/chargers/bulk/create).
// With a template picked, the (disabled) manual fields show its values; ADD.manual keeps what was typed for
// "no template".
const ADD = { templates: [], manual: null };
const ADD_FIELDS = { addModel: "model", addConnectors: "connectors", addPower: "max_power_w" };

function syncAddTemplate() {
  const tpl = ADD.templates.find((x) => x.name === $("addTemplate").value);
  if (tpl) {
    if (!ADD.manual) ADD.manual = Object.fromEntries(Object.keys(ADD_FIELDS).map((id) => [id, $(id).value]));
    for (const [id, key] of Object.entries(ADD_FIELDS)) {
      const v = (tpl.fields || {})[key];
      if (v !== undefined && v !== null) $(id).value = v;
    }
  } else if (ADD.manual) {
    for (const [id, v] of Object.entries(ADD.manual)) $(id).value = v;
    ADD.manual = null;
  }
  for (const id of Object.keys(ADD_FIELDS)) $(id).disabled = !!tpl;
  const lang = (document.documentElement.lang || "en").slice(0, 2);
  $("addTemplateInfo").textContent = tpl ? ((tpl.descriptions || {})[lang] || tpl.description || tpl.title) : "";
}

async function openAddChargerDialog() {
  $("addErr").textContent = "";
  let n = S.chargers.size + 1, id;
  do { id = `CP-${String(n++).padStart(4, "0")}`; } while (S.chargers.has(id));
  $("addId").value = id;
  $("addCount").value = 1;
  try { ADD.templates = await api("GET", "/api/templates"); } catch (e) { ADD.templates = []; fail(e); }
  const sel = $("addTemplate"), keep = sel.value;
  sel.replaceChildren(new Option(t("add.template.none"), ""),
    ...ADD.templates.map((x) => new Option(`${x.title} (${x.name})`, x.name)));
  sel.value = ADD.templates.some((x) => x.name === keep) ? keep : "";
  syncAddTemplate();
  $("dlgAdd").showModal();
}

async function addCharger() {
  const tags = $("addTags").value.split(",").map((s) => s.trim()).filter(Boolean);
  const body = { id: $("addId").value.trim(), count: Number($("addCount").value) || 1, fields: {} };
  if ($("addTemplate").value) body.template = $("addTemplate").value;
  else Object.assign(body.fields, { model: $("addModel").value.trim() || "SIM-AC6",
    connectors: Number($("addConnectors").value), max_power_w: Number($("addPower").value) });
  if ($("addPassword").value) body.password = $("addPassword").value;
  if (tags.length) body.fields.id_tags = tags;
  const r = await api("POST", "/api/chargers/bulk/create", body);
  $("addPassword").value = "";
  const ids = r.ids || [body.id];
  Shell.toast(ids.length > 1 ? t("add.done_n", { n: ids.length, first: ids[0], last: ids[ids.length - 1] })
    : t("add.done", { id: ids[0] }), "ok");
  selectWhenListed(ids[0]);
}

// ------------------------------------------------------------------ Excel import / charger templates

function showImportMessages(title, msgs) {
  $("importTitle").textContent = title;
  $("importErrors").replaceChildren(...msgs.map((m) => { const li = document.createElement("li"); li.textContent = m; return li; }));
  $("dlgImport").showModal();
}

async function importXlsx(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const r = await fetch("/api/config/import", { method: "POST", body: form });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const errs = data.errors || [data.error || String(r.status)];
      showImportMessages(t("xlsx.import.failed", { n: errs.length, file: file.name }), errs);
      return;
    }
    Shell.toast(t("common.config.import_xlsx.ok"), "ok");
    if ((data.warnings || []).length) showImportMessages(t("xlsx.import.warnings", { n: data.warnings.length }), data.warnings);
    await loadState();
  } catch (e) { fail(e); }
}

async function reloadTemplates() {
  try {
    const r = await api("POST", "/api/templates/reload");
    Shell.toast(t("common.device_templates.reloaded", { n: (r.loaded || []).length, dir: r.dir }), "ok");
    if ((r.errors || []).length) showImportMessages(t("common.device_templates.errors.title"),
      r.errors.map((e) => `${e.path}: ${e.error}`));
  } catch (e) { fail(e); }
}

async function saveAsTemplate() {
  if (!S.sel) { Shell.toast(t("templates.select_first"), "info"); return; }
  const name = (window.prompt(t("common.device_templates.save.prompt"), "") || "").trim();
  if (!name) return;
  const url = `/api/templates/from-device/${enc(S.sel)}`;
  try {
    let r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    if (r.status === 409 && window.confirm(t("common.device_templates.save.exists", { name }))) {
      r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, overwrite: true }) });
    }
    const data = await r.json().catch(() => ({}));
    if (r.status === 409) return;
    if (!r.ok) throw new Error(data.error || String(r.status));
    Shell.toast(t("common.device_templates.saved", { name, path: data.path || "" }), "ok");
  } catch (e) { fail(e); }
}

// A new charger reaches the list with the next push; poll /api/state briefly, then select it (or say so).
async function selectWhenListed(id) {
  for (let i = 0; i < 12 && !S.chargers.has(id); i++) {
    await sleep(250);
    if (!S.chargers.has(id)) await loadState();
  }
  if (S.chargers.has(id)) openChargerDialog(id);
  else Shell.toast(t("add.pending", { id }), "info");
}

const BLOCK_MODES = ["NotImplemented", "NotSupported", "NoReply"];

// Number inputs of the impairments dialog: element id -> field (null when empty, for the optional ones).
const IMP_NUMBERS = { impLatency: "latency_ms", impJitter: "jitter_ms", impSeed: "seed", impDropEvery: "drop_every_s",
  impDropJitter: "drop_jitter_s", impDown: "down_s" };
const IMP_OPTIONAL = new Set(["seed", "drop_every_s"]);
// Number inputs of impairments.meter: element id -> field.
const IMP_METER = { impMeterDrop: "drop_ratio", impMeterReorder: "reorder_window", impVoltNoise: "voltage_noise_v" };

async function openImpairDialog() {
  const id = S.sel;
  if (!id) return;
  let detail, imp;
  try {
    [detail, imp] = await Promise.all([api("GET", `/api/chargers/${enc(id)}`), api("GET", chargerUrl(id, "impairments"))]);
  } catch (e) { fail(e); return; }
  const block = imp.block || {};
  const delays = imp.response_delay_ms || {};
  const meter = imp.meter || {};
  $("impairTitle").textContent = t("imp.title", { id });
  $("impairErr").textContent = "";
  for (const [el, k] of Object.entries(IMP_NUMBERS)) $(el).value = imp[k] == null ? "" : imp[k];
  $("impDirection").value = imp.direction || "both";
  $("impDropMode").value = imp.drop_mode || "abort";
  $("impRemoteStart").value = imp.remote_start || "normal";
  for (const [el, k] of Object.entries(IMP_METER)) $(el).value = meter[k] ? meter[k] : "";
  $("impEndAfterStop").checked = !!meter.end_after_stop;
  $("impDropNow").disabled = !detail.connected;
  $("blockRows").replaceChildren(...(detail.csms_actions || []).map((a) => h("tr", null, h("td", null, a),
    h("td", null, h("select", { "data-action": a },
      h("option", { value: "" }, t("imp.mode.none")),
      ...BLOCK_MODES.map((m) => h("option", { value: m, selected: block[a] === m }, t(`imp.mode.${m}`))))),
    h("td", null, h("input", { type: "number", min: "0", max: "600000", step: "any", "data-delay": a,
      value: delays[a] == null ? "" : delays[a] })))));
  $("impairForm").dataset.id = id;
  $("dlgImpair").showModal();
}

function impairBody() {
  const body = { block: {}, direction: $("impDirection").value, drop_mode: $("impDropMode").value,
    remote_start: $("impRemoteStart").value, meter: { end_after_stop: $("impEndAfterStop").checked },
    response_delay_ms: {} };
  for (const sel of $("blockRows").querySelectorAll("select")) if (sel.value) body.block[sel.dataset.action] = sel.value;
  for (const inp of $("blockRows").querySelectorAll("input[data-delay]")) {
    const v = inp.value.trim();
    if (v !== "" && Number(v) > 0) body.response_delay_ms[inp.dataset.delay] = Number(v);
  }
  for (const [el, k] of Object.entries(IMP_METER)) {
    const v = $(el).value.trim();
    if (v === "") continue;
    const n = Number(v);  // drop_ratio is a share: clamped to 0 - 1 before it is sent
    body.meter[k] = k === "drop_ratio" ? Math.min(1, Math.max(0, n)) : n;
  }
  for (const [el, k] of Object.entries(IMP_NUMBERS)) {
    const v = $(el).value.trim();
    if (v !== "") body[k] = Number(v);
    else if (IMP_OPTIONAL.has(k)) body[k] = null;
  }
  return body;
}

async function saveImpair() {
  const id = $("impairForm").dataset.id;
  const body = impairBody();
  await api("PUT", chargerUrl(id, "impairments"), body);
  Shell.toast(t("imp.saved", { id, n: Object.keys(body.block).length }), "ok");
}

async function dropNow() {
  const id = $("impairForm").dataset.id;
  const v = $("impDown").value.trim();
  const body = { mode: $("impDropMode").value };
  if (v !== "") body.reconnect_after_s = Number(v);
  try {
    const r = await api("POST", chargerUrl(id, "drop"), body);
    Shell.toast(t("imp.dropped", { id, s: r.reconnect_after_s }), "ok");
  } catch (e) { $("impairErr").textContent = String((e && e.message) || e); }
}

function openDataTransfer() {
  $("dtErr").textContent = "";
  $("dtResult").hidden = true;
  $("dlgDataTransfer").showModal();
}

async function sendDataTransfer() {
  const body = { vendor_id: $("dtVendor").value.trim(), data: $("dtData").value };
  if ($("dtMessage").value.trim()) body.message_id = $("dtMessage").value.trim();
  const r = await api("POST", chargerUrl(S.sel, "datatransfer"), body);
  $("dtResult").textContent = JSON.stringify(r, null, 2);
  $("dtResult").hidden = false;
}

// Dialog forms: the default button runs `fn` and closes on success; Cancel / Esc close without it.
function bindForm(formId, errId, fn, keepOpen) {
  const form = $(formId);
  form.addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    $(errId).textContent = "";
    try {
      await fn();
      if (!keepOpen) form.closest("dialog").close("default");
    } catch (err) { $(errId).textContent = String(err.message || err); }
  });
}

Object.assign(window, { openChargerDialog, openSettingsDialog, openProfileDialog, openFrameDetail, openAddChargerDialog });

// ------------------------------------------------------------------ live updates

function applyState(st) {
  if (!st) return;
  S.running = st.running !== false;
  S.timeScale = st.time_scale || 1;
  syncSim();
  replaceChargers(st.chargers || []);
}

async function loadState() {
  try { applyState(await api("GET", "/api/state")); } catch { /* server not reachable: keep the placeholder */ }
}

// A push_seq gap: frames are always refetched; state too unless the message itself is a full one.
let refetchTimer = null, refetchState = false;
function onGap(full) {
  refetchState = refetchState || !full;
  clearTimeout(refetchTimer);
  refetchTimer = setTimeout(() => {
    if (refetchState) loadState();
    refetchState = false;
    fetchFrames(S.frameSeq);
  }, 200);
}

function handle(m) {
  switch (m.type) {
    case "snapshot":   // first message of a (re)connect, and after a reload
      if (m.data) applyState(m.data); else loadState();
      fetchFrames(S.frameSeq);
      break;
    case "charger_state":
      if (m.full) replaceChargers(m.chargers || []); else updateChargers(m.chargers || []);
      break;
    case "values":
      applyValues(m.values, m.full);
      break;
    case "frame": {
      const fr = m.frames || [];
      if (fr.length && fr[0].seq > S.frameSeq + 1 && S.frameSeq) fetchFrames(S.frameSeq);
      addFrames(fr);
      break;
    }
    case "stats":   // BaseEngine pushes stats_snapshot() every stats_interval
      S.stats = m.stats || null;
      syncCount();
      break;
    default:
      break;
  }
}

function setWs(state) {
  const el = $("wsBadge");
  if (!el) return;
  el.className = `badge ${state === "connected" ? "b-ok" : state === "connecting" ? "b-warn" : "b-bad"}`;
  el.textContent = tk("common.backend", state);
}

// Reconnect with exponential back-off and jitter (1 s doubling up to 30 s, reset once connected).
let wsDelay = 1000;
function connectWs() {
  setWs("connecting");
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  let last = 0;
  ws.onopen = () => { setWs("connected"); wsDelay = 1000; };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.push_seq) {
      if (last && m.push_seq !== last + 1) onGap(m.full);
      last = m.push_seq;
    }
    handle(m);
  };
  ws.onclose = () => {
    setWs("disconnected");
    setTimeout(connectWs, wsDelay * (0.5 + Math.random() / 2));
    wsDelay = Math.min(30000, wsDelay * 2);
  };
}

// ------------------------------------------------------------------ simulation / menus

function syncSim() {
  const b = $("simBtn");
  if (b) {
    b.textContent = t(S.running ? "common.sim.pause" : "common.sim.run");
    $("simState").textContent = t(S.running ? "common.sim.running" : "common.sim.paused");
    $("simScale").value = String(S.timeScale);
  }
  Shell.updateItem("sim.toggle", { label: S.running ? "common.sim.pause" : "common.sim.run" });
  for (const s of TIME_SCALES) Shell.updateItem(`sim.scale.${s}`, { checked: S.timeScale === s });
}

async function setSim(body) {
  try {
    const r = await api("POST", "/api/simulation", body);
    S.running = r.running !== false;
    S.timeScale = r.time_scale || S.timeScale;
    syncSim();
  } catch (e) { fail(e); }
}

async function saveConfig() {
  try {
    const r = await api("POST", "/api/config/save");
    Shell.toast(t("common.config.saved", { path: r.path || "" }), "ok");
  } catch (e) { fail(e); }
}

Shell.init({
  ...PRODUCT,
  vendor: Shell.vendors.smartEdge,
  about: [["about.stack", t("about.stack.value")]],
  menus: () => [
    { id: "file", label: "common.menu.file", items: [
      { id: "file.add", label: "menu.add_charger", onClick: openAddChargerDialog },
      { id: "file.settings", label: "menu.settings", onClick: () => openSettingsDialog() },
      { separator: true },
      { id: "file.save", label: "common.config.save", onClick: saveConfig },
      { id: "file.export", label: "menu.export_frames", href: "/api/frames.jsonl", download: "frames.jsonl" },
      { separator: true },
      { id: "file.xlsx_export", label: "common.config.export_xlsx", href: "/api/config/xlsx", download: "sme-ocpp-sim-chargers.xlsx" },
      { id: "file.xlsx_import", label: "common.config.import_xlsx", onClick: () => $("xlsxFile").click() },
      { id: "file.xlsx_template", label: "common.config.xlsx_template", href: "/api/config/xlsx-template", download: "sme-ocpp-sim-template.xlsx" },
      { separator: true },
      { id: "file.templates", label: "common.device_templates.menu", submenu: [
        { id: "file.templates.save", label: "common.device_templates.save", title: t("common.device_templates.save.title"), onClick: saveAsTemplate },
        { id: "file.templates.reload", label: "common.device_templates.reload", onClick: reloadTemplates },
      ] },
    ] },
    { id: "sim", label: "common.menu.sim", items: [
      { id: "sim.toggle", label: "common.sim.pause", onClick: () => setSim({ running: !S.running }) },
      { id: "sim.scale", label: "common.sim.time_scale", submenu: TIME_SCALES.map((s) => (
        { id: `sim.scale.${s}`, label: `×${s}`, radio: true, checked: s === 1, onClick: () => setSim({ time_scale: s }) })) },
      { separator: true },
      { id: "sim.connect_all", label: "menu.connect_all", onClick: () => api("POST", "/api/chargers/bulk/connect", { all: true }).catch(fail) },
      { id: "sim.disconnect_all", label: "menu.disconnect_all", onClick: () => api("POST", "/api/chargers/bulk/disconnect", { all: true }).catch(fail) },
      { separator: true },
      { id: "sim.csms", label: "menu.mini_csms", onClick: () => window.openCsmsPanel() },
    ] },
    { id: "view", label: "common.menu.view", items: [Shell.themeItem(), Shell.langItem()] },
    Shell.helpMenu(),
  ],
});

Shell.setStatus(`<span class="simstat"><span id="simState"></span>
  <select id="simScale">${TIME_SCALES.map((s) => `<option value="${s}">×${s}</option>`).join("")}</select>
  <button type="button" class="btn small" id="simBtn"></button>
  <span id="wsBadge" class="badge"></span></span>`);
$("simScale").setAttribute("aria-label", t("common.sim.time_scale"));
$("simBtn").addEventListener("click", () => setSim({ running: !S.running }));
$("simScale").addEventListener("change", (e) => setSim({ time_scale: Number(e.target.value) }));

// ------------------------------------------------------------------ wiring

versionOptions($("setVersion"), false);
versionOptions($("setCVersion"), true);
$("setPassword").placeholder = t("settings.password.ph");

$("chargerFilter").addEventListener("input", () => {
  for (const c of S.chargers.values()) S.rows.get(c.id).tr.hidden = !matchesFilter(c);
  syncSelAll();
});
// Select all = the rows the filter shows; hidden rows keep their state.
$("selAll").addEventListener("change", (e) => {
  for (const id of visibleIds()) {
    if (e.target.checked) S.checked.add(id); else S.checked.delete(id);
    S.rows.get(id).chk.checked = e.target.checked;
  }
  syncSelAll();
});
$("bulkConnect").addEventListener("click", () => bulk("connect"));
$("bulkDisconnect").addEventListener("click", () => bulk("disconnect"));
$("btnAdd").addEventListener("click", openAddChargerDialog);
$("dConnect").addEventListener("click", () => op(S.sel, "connect"));
$("dDisconnect").addEventListener("click", () => op(S.sel, "disconnect"));
$("dDataTransfer").addEventListener("click", openDataTransfer);
$("dSettings").addEventListener("click", () => openSettingsDialog(S.sel));
$("dImpair").addEventListener("click", openImpairDialog);
$("impDropNow").addEventListener("click", dropNow);
$("scProfiles").addEventListener("click", () => openProfileDialog(S.sel));
$("scConnector").addEventListener("change", () => { S.composite = null; drawChart(); loadSmartCharging(); });
for (const id of ["fCharger", "fDir"]) $(id).addEventListener("change", renderFrames);
$("fAction").addEventListener("input", renderFrames);
// Clear drops the frames kept client side too (newer ones still arrive; Details refetches older ones).
$("fClear").addEventListener("click", () => { S.frames = []; S.frameSet.clear(); $("frameRows").replaceChildren(); });
$("tagValue").addEventListener("input", checkTag);
$("keysBox").addEventListener("toggle", () => { if ($("keysBox").open) loadKeys(); });
$("varsBox").addEventListener("toggle", () => { if ($("varsBox").open) loadKeys(); });
$("extForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("extLimit").valueAsNumber;
  if (Number.isFinite(v)) setExternalLimit(v);
});
$("extClear").addEventListener("click", () => setExternalLimit(null));
bindForm("setForm", "setErr", applySettings);
bindForm("addForm", "addErr", addCharger);
$("addTemplate").addEventListener("change", syncAddTemplate);
$("xlsxFile").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (file) importXlsx(file);
});
bindForm("dtForm", "dtErr", sendDataTransfer, true);
bindForm("impairForm", "impairErr", saveImpair);

// Composite schedule and chart refresh for the selected charger (the "now" line moves even without pushes).
// Paused while the tab is hidden; refreshed at once when it becomes visible again.
setInterval(() => { if (S.sel && !document.hidden) loadSmartCharging(); }, 15000);
setInterval(() => { if (S.sel && !document.hidden) scheduleDraw(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.sel) loadSmartCharging(); });

fetch("/api/version").then((r) => (r.ok ? r.json() : null))
  .then((v) => { if (v && v.version) Shell.setVersion(v.version); }).catch(() => {});
loadState().then(() => fetchFrames(0)).then(renderFrames);
connectWs();
