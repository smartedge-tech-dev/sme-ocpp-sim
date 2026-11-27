"use strict";
// SME OCPPSim - Mini CSMS panel (plan M9). Loaded after app.js and uses its helpers (api, fail, h, t, $, hhmmss,
// pretty). While the dialog is open it polls GET /api/csms (stations) and, for the selected station,
// GET /api/csms/chargers/{id} and GET /api/csms/frames?charger=. The JSON editor sends
// POST /api/csms/chargers/{id}/call; the server checks the payload against the version's schema before sending.
//
// Global entry point: openCsmsPanel(stationId?).

(() => {
  const POLL_MS = 2000;
  const FRAME_ROWS = 40;
  const P = { sel: null, version: null, timer: null, actions: {}, busy: false, again: false };

  const when = (s) => (s ? hhmmss(s * 1000) : "-");
  const enc = encodeURIComponent;

  function table(head, rows) {
    return h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ...head.map((k) => h("th", null, t(k))))),
      h("tbody", null, ...rows.map((r) => h("tr", null, ...r.map((c) => h("td", null, c == null ? "" : String(c)))))));
  }

  function section(titleKey, ...kids) {
    return h("div", { class: "csms-sec" }, h("h4", null, t(titleKey)), ...kids);
  }

  function renderStatus(st) {
    let text;
    if (st.running) text = t("csms.listening", { url: st.url, versions: st.versions.join(", ") });
    else if (st.error) text = t("csms.error", { error: st.error });
    else text = t("csms.disabled");
    $("csmsStatus").textContent = text;
    $("csmsStatus").className = st.error ? "err" : "muted";
    const rows = $("csmsRows");
    rows.replaceChildren(...st.chargers.map((c) =>
      h("tr", { class: c.id === P.sel ? "sel" : null, onclick: () => select(c.id, c.ocpp_version) },
        h("td", null, c.id), h("td", null, c.ocpp_version),
        h("td", null, c.connected ? t(`csms.boot.${c.boot_status || "none"}`) : t("csms.offline")),
        h("td", null, when(c.heartbeat_at)), h("td", null, String(c.active_transactions)))));
    $("csmsEmpty").hidden = st.chargers.length > 0;
    $("csmsRejections").replaceChildren(...st.rejections.slice(-5).reverse().map((r) =>
      h("li", null, `${when(r.ts)} ${r.id || "?"} ${r.status == null ? "WS" : r.status}: ${r.reason}`)));
  }

  function renderDetail(d) {
    const boot = d.boot ? Object.entries(d.boot).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : v]) : [];
    const statuses = Object.entries(d.statuses).map(([k, s]) => [k, s.status, s.error_code || "", when(s.received_at)]);
    const txs = d.transactions.slice().reverse().map((x) => [x.id, x.connector, x.id_tag, x.active ? t("csms.tx.active") : (x.stop_reason || ""),
      x.meter_start_wh, x.meter_stop_wh, x.energy]);
    const meter = d.meter_values.slice(-10).reverse().map((m) => [when(m.received_at), m.connector, m.transaction,
      Object.entries(m.values).map(([k, v]) => `${k}=${v}`).join(", ")]);
    const plan = d.plan.map((p) => [p.index, p.action, `${p.after} +${p.at_s}s`, t(`csms.plan.${p.status}`),
      p.error ? (typeof p.error === "object" ? JSON.stringify(p.error) : p.error) : (p.result ? JSON.stringify(p.result) : "")]);
    $("csmsDetail").replaceChildren(
      h("p", { class: "muted" }, t("csms.detail.info", { version: d.ocpp_version, remote: d.remote || "-",
        boots: d.boots, heartbeats: d.heartbeats, auth: d.basic_auth ? "Basic" : "-" })),
      section("csms.sec.boot", boot.length ? table(["common.col.name", "common.col.value"], boot) : h("p", { class: "muted" }, "-")),
      section("csms.sec.status", table(["csms.col.connector", "csms.col.status", "csms.col.error", "csms.col.time"], statuses)),
      section("csms.sec.tx", table(["csms.col.tx", "csms.col.connector", "csms.col.tag", "csms.col.state",
        "csms.col.meter_start", "csms.col.meter_stop", "csms.col.energy"], txs)),
      section("csms.sec.meter", table(["csms.col.time", "csms.col.connector", "csms.col.tx", "csms.col.values"], meter)),
      section("csms.sec.plan", table(["csms.col.index", "csms.col.action", "csms.col.when", "csms.col.status",
        "csms.col.result"], plan)),
      ...(d.warnings.length ? [section("csms.sec.warnings", h("ul", { class: "plist" },
        ...d.warnings.slice(-10).map((w) => h("li", null, `${when(w.ts)} ${w.text}`))))] : []));
  }

  function renderFrames(frames) {
    $("csmsFrames").replaceChildren(...frames.slice(-FRAME_ROWS).reverse().map((f) =>
      h("tr", { onclick: () => { $("csmsResult").textContent = pretty(f.raw); $("csmsResult").hidden = false; } },
        h("td", null, hhmmss(f.ts * 1000)), h("td", null, f.direction === "rx" ? "←" : "→"),
        h("td", null, f.message_type || ""), h("td", null, f.action || ""),
        h("td", { class: "payload" }, f.note ? `${f.note} ${f.raw}` : f.raw))));
  }

  async function loadActions(version) {
    if (!P.actions[version]) P.actions[version] = (await api("GET", `/api/csms/actions?version=${enc(version)}`)).actions;
    const sel = $("csmsAction");
    const keep = sel.value;
    sel.replaceChildren(...P.actions[version].map((a) => h("option", { value: a }, a)));
    if (P.actions[version].includes(keep)) sel.value = keep;
  }

  // One refresh at a time: a slow answer must not let the 2 s timer stack up overlapping requests (an older
  // answer could otherwise overwrite a newer one). A call while one runs (e.g. after select / send) is not lost:
  // one more refresh follows.
  async function refresh() {
    if (P.busy) {
      P.again = true;
      return;
    }
    P.busy = true;
    try {
      do {
        P.again = false;
        await refreshOnce();
      } while (P.again && P.timer);
    } finally {
      P.busy = false;
    }
  }

  async function refreshOnce() {
    try {
      const st = await api("GET", "/api/csms");
      renderStatus(st);
      const cur = st.running && P.sel ? st.chargers.find((c) => c.id === P.sel) : null;
      $("csmsSelBox").hidden = !cur;
      if (!cur) {
        P.version = null;  // the station is gone: its actions are reloaded when it (or another one) is selected
        return;
      }
      if (P.version !== cur.ocpp_version) {
        P.version = cur.ocpp_version;
        loadActions(P.version).catch(fail);
      }
      const [d, fr] = await Promise.all([api("GET", `/api/csms/chargers/${enc(P.sel)}`),
        api("GET", `/api/csms/frames?charger=${enc(P.sel)}&limit=${FRAME_ROWS}`)]);
      $("csmsSelTitle").textContent = d.id;
      $("csmsSend").disabled = !d.connected;
      renderDetail(d);
      renderFrames(fr.frames);
    } catch (e) {
      $("csmsStatus").textContent = String((e && e.message) || e);
      $("csmsStatus").className = "err";
    }
  }

  async function select(id, version) {
    P.sel = id;
    P.version = version;
    try { await loadActions(version); } catch (e) { fail(e); }
    refresh();
  }

  async function send() {
    const out = $("csmsResult");
    out.hidden = false;
    let payload;
    try {
      payload = JSON.parse($("csmsPayload").value || "{}");
    } catch (e) {
      out.textContent = t("csms.editor.bad_json", { error: e.message });
      return;
    }
    out.textContent = t("csms.editor.sending");
    try {
      const r = await api("POST", `/api/csms/chargers/${enc(P.sel)}/call`, { action: $("csmsAction").value, payload });
      out.textContent = JSON.stringify(r, null, 2);
    } catch (e) {
      out.textContent = String((e && e.message) || e);
    }
    refresh();
  }

  function stop() {
    if (P.timer) clearInterval(P.timer);
    P.timer = null;
  }

  async function openCsmsPanel(id) {
    const dlg = $("dlgCsms");
    if (id) P.sel = id;
    if (!dlg.open) dlg.showModal();
    stop();
    P.timer = setInterval(refresh, POLL_MS);
    await refresh();
  }

  $("dlgCsms").addEventListener("close", stop);
  $("csmsSend").addEventListener("click", send);
  window.openCsmsPanel = openCsmsPanel;
})();
