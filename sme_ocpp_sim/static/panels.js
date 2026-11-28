"use strict";
// SME OCPPSim - Scenario and Traffic panels (v0.3 M9). Loaded after app.js and uses its helpers (api, fail, h, t,
// $, enc, kw). Both dialogs poll only while they are open.
//
// Scenario: GET /api/scenarios/builtin (+ /{name} for the YAML text), POST /api/scenarios/run (text body),
// POST /api/scenarios/abort, GET /api/scenarios/current?steps=true (live step list),
// GET /api/scenarios/result?format=json|junit (downloads).
// Traffic: GET /api/traffic, POST /api/traffic/start | /stop, GET /api/traffic/plan?hours=&limit=.
//
// Global entry points: openScenarioPanel(), openTrafficPanel().

(() => {
  const POLL_MS = 1000;
  const PLAN_LIMIT = 200;
  const RECENT_ROWS = 20;
  const STEP_CLASS = { pass: "b-ok", running: "b-warn", fail: "b-bad", error: "b-bad", aborted: "b-bad" };
  const COUNTERS = ["arrivals", "started", "completed", "rejected", "skipped", "aborted", "failed", "preempted",
    "cancelled"];

  // Sim seconds since the start as h:mm:ss.
  const dur = (s) => {
    if (s == null) return "-";
    const v = Math.max(0, Math.round(s));
    return `${Math.floor(v / 3600)}:${String(Math.floor(v / 60) % 60).padStart(2, "0")}:${String(v % 60).padStart(2, "0")}`;
  };
  const iso = (s) => (s ? String(s).replace("T", " ").replace(/(\.\d+)?(Z|[+-]\d\d:\d\d)$/, "") : "-");
  const badge = (key, cls) => h("span", { class: `badge ${cls || ""}` }, t(key));

  // A table of i18n headers and cell rows; wrapLast lets the last column (messages) wrap.
  function table(head, rows, wrapLast) {
    return h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ...head.map((k) => h("th", null, t(k))))),
      h("tbody", null, ...rows.map((r) => h("tr", null, ...r.map((c, i) =>
        h("td", wrapLast && i === r.length - 1 ? { class: "wrap" } : null, c == null ? "" : c))))));
  }

  // Poller: one timer per dialog, no overlapping requests.
  function poller(dlgId, refresh) {
    const P = { timer: null, busy: false };
    const tick = async () => {
      if (P.busy) return;
      P.busy = true;
      try { await refresh(); } finally { P.busy = false; }
    };
    const stop = () => { if (P.timer) clearInterval(P.timer); P.timer = null; };
    $(dlgId).addEventListener("close", stop);
    return async () => {
      const dlg = $(dlgId);
      if (!dlg.open) dlg.showModal();
      stop();
      P.timer = setInterval(tick, POLL_MS);
      await tick();
    };
  }

  // ------------------------------------------------------------------ scenario

  const SC = { builtins: null, state: null };

  async function loadBuiltins() {
    if (SC.builtins) return;
    try {
      SC.builtins = (await api("GET", "/api/scenarios/builtin")).scenarios || [];
    } catch (e) { SC.builtins = []; fail(e); }
    const sel = $("scnBuiltin");
    sel.replaceChildren(h("option", { value: "" }, t("scn.builtin.pick")),
      ...SC.builtins.map((b) => h("option", { value: b.name, title: b.description || "" },
        b.title && b.title !== b.name ? `${b.name} - ${b.title}` : b.name)));
  }

  async function loadBuiltin() {
    const name = $("scnBuiltin").value;
    const info = (SC.builtins || []).find((b) => b.name === name);
    $("scnDesc").textContent = (info && info.description) || "";
    if (!name) return;
    try {
      $("scnText").value = await api("GET", `/api/scenarios/builtin/${enc(name)}`);
    } catch (e) { fail(e); }
  }

  function loadFile(file) {
    if (!file) return;
    file.text().then((text) => { $("scnText").value = text; $("scnBuiltin").value = ""; $("scnDesc").textContent = ""; })
      .catch(fail);
  }

  async function runScenario() {
    const text = $("scnText").value.trim();
    if (!text) { Shell.toast(t("scn.empty_text"), "error"); return; }
    try {
      const r = await fetch("/api/scenarios/run", { method: "POST", headers: { "Content-Type": "text/plain; charset=utf-8" },
        body: text });
      const data = await r.json().catch(() => null);
      if (!r.ok) throw new Error((data && (data.error || data.detail)) || r.status);
      Shell.toast(t("scn.started", { name: data.name }), "ok");
    } catch (e) { fail(e); }
    await refreshScenario();
  }

  async function abortScenario() {
    try { await api("POST", "/api/scenarios/abort"); } catch (e) { fail(e); }
    await refreshScenario();
  }

  function renderScenario(st) {
    SC.state = st;
    const running = st.state === "running";
    $("scnRun").disabled = running;
    $("scnAbort").disabled = !running;
    const done = st.state === "done";
    for (const id of ["scnJson", "scnJunit"]) $(id).classList.toggle("disabled", !done);
    let line;
    if (st.state === "idle") line = t("scn.state.idle");
    else {
      const counts = st.steps || {};
      line = t("scn.summary", { name: st.name, state: t(`scn.state.${st.state}`),
        result: st.result ? t(`scn.result.${st.result}`) : "-", wall: (st.wall_s || 0).toFixed(1), sim: dur(st.sim_s),
        pass: counts.pass || 0, fail: (counts.fail || 0) + (counts.error || 0), total: (st.step_list || []).length });
    }
    $("scnStatus").textContent = line;
    $("scnStatus").className = st.result && st.result !== "pass" ? "err" : "muted";
    $("scnError").textContent = st.error || "";
    $("scnPrompts").replaceChildren(...(st.prompts || []).map((p) => h("li", null, h("b", null, p.track), " ", p.prompt)));
    const steps = st.step_list || [];
    $("scnSteps").replaceChildren(steps.length ? table(
      ["scn.col.track", "scn.col.step", "scn.col.kind", "scn.col.status", "scn.col.sim", "scn.col.message"],
      steps.map((s) => [s.track, s.id, s.kind, badge(`scn.status.${s.status}`, STEP_CLASS[s.status]),
        s.t_sim == null ? "-" : dur(s.t_sim), s.message || ""]), true)
      : h("p", { class: "empty" }, t("scn.no_steps")));
  }

  async function refreshScenario() {
    try { renderScenario(await api("GET", "/api/scenarios/current?steps=true")); } catch (e) { $("scnError").textContent = e.message; }
  }

  const openScenario = poller("dlgScenario", refreshScenario);
  async function openScenarioPanel() {
    await openScenario();
    await loadBuiltins();
  }

  // ------------------------------------------------------------------ traffic

  async function refreshTraffic() {
    let st;
    try { st = await api("GET", "/api/traffic"); } catch (e) { $("trfError").textContent = e.message; return; }
    const running = st.state === "running";
    $("trfStart").disabled = running;
    $("trfStop").disabled = !running;
    const cfg = st.config || {};
    $("trfStatus").textContent = t("trf.summary", { state: t(`trf.state.${st.state}`),
      enabled: t(st.enabled ? "trf.enabled.yes" : "trf.enabled.no"), seed: cfg.seed == null ? "-" : cfg.seed,
      sim: running ? iso(st.sim_time) : "-" });
    $("trfError").textContent = st.error || "";
    const stats = st.stats || {};
    $("trfCounters").replaceChildren(
      h("div", { class: "kv" }, h("span", null, t("trf.active")), h("b", null, String(st.active ?? 0))),
      h("div", { class: "kv" }, h("span", null, t("trf.power")), h("b", null, `${kw(st.power_w ?? 0)} kW`)),
      ...COUNTERS.map((k) => h("div", { class: "kv" }, h("span", null, t(`trf.stat.${k}`)), h("b", null, String(stats[k] ?? 0)))));
    const recent = (st.recent || []).slice(-RECENT_ROWS).reverse();
    $("trfRecent").replaceChildren(recent.length ? table(
      ["trf.col.time", "trf.col.charger", "trf.col.connector", "trf.col.event", "trf.col.id_tag", "trf.col.ev_model"],
      recent.map((e) => [dur(e.t_s), e.charger, String(e.connector), e.note ? `${e.event} (${e.note})` : e.event,
        e.id_tag, e.ev_model]))
      : h("p", { class: "empty" }, t("trf.no_events")));
  }

  async function trafficAction(what) {
    try { await api("POST", `/api/traffic/${what}`); } catch (e) { fail(e); }
    await refreshTraffic();
  }

  async function previewPlan() {
    const hours = Number($("trfHours").value) || 1;
    const box = $("trfPlan");
    try {
      const p = await api("GET", `/api/traffic/plan?hours=${enc(hours)}&limit=${PLAN_LIMIT}`);
      $("trfPlanInfo").textContent = t(p.truncated ? "trf.plan.info_cut" : "trf.plan.info",
        { n: p.count, hours: p.hours, start: iso(p.start), shown: p.arrivals.length });
      box.replaceChildren(p.arrivals.length ? table(
        ["trf.col.at", "trf.col.charger", "trf.col.connector", "trf.col.id_tag", "trf.col.duration", "trf.col.ev_model",
          "trf.col.soc"],
        p.arrivals.map((a) => [iso(a.at), a.charger, String(a.connector), a.id_tag, dur(a.duration_s), a.ev_model,
          `${Math.round(a.initial_soc)} → ${Math.round(a.target_soc)} %`]))
        : h("p", { class: "empty" }, t("trf.plan.none")));
    } catch (e) {
      $("trfPlanInfo").textContent = "";
      box.replaceChildren(h("p", { class: "err" }, e.message));
    }
  }

  const openTraffic = poller("dlgTraffic", refreshTraffic);
  async function openTrafficPanel() {
    await openTraffic();
    if (!$("trfPlan").childElementCount) await previewPlan();
  }

  // ------------------------------------------------------------------ wiring

  $("scnBuiltin").addEventListener("change", loadBuiltin);
  $("scnText").addEventListener("input", () => { $("scnBuiltin").value = ""; $("scnDesc").textContent = ""; });
  $("scnFile").addEventListener("change", (e) => { loadFile(e.target.files[0]); e.target.value = ""; });
  $("scnOpen").addEventListener("click", () => $("scnFile").click());
  $("scnRun").addEventListener("click", runScenario);
  $("scnAbort").addEventListener("click", abortScenario);
  $("trfStart").addEventListener("click", () => trafficAction("start"));
  $("trfStop").addEventListener("click", () => trafficAction("stop"));
  $("trfPreview").addEventListener("click", previewPlan);
  Object.assign(window, { openScenarioPanel, openTrafficPanel });
})();
