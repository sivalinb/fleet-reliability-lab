/* Fleet Reliability Observatory. No framework or build step.
 * Hardware telemetry is synthetic. Replay data is a checked-in execution record.
 * Local credentials are deliberately kept in memory, never browser storage. */
"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const state = { evidence: null, scenario: "optic", frame: 0, playing: false, timer: null, motion: !reducedMotion.matches, api: null, apiSnapshot: null, apiTargetAssetId: null, busy: false };
  const scenarioInfo = {
    optic: { title: "Optical gray failure", takeaway: "A busy GPU can still be waiting on the fabric. Correlate component evidence with workload progress, then verify both after repair.", caption: "Follow the link symptom all the way to useful compute.", image: "optic-repair-journey.webp", metric: "fabric_errors", metricName: "Fabric errors · cumulative", unit: "synthetic counter" },
    burnin: { title: "Burn-in qualification gate", takeaway: "Capacity enters service only after qualification. An unsupported physical GPU burn-in stays explicitly skipped.", caption: "The machine has arrived. It still has to earn its place.", image: "burn-in-laboratory.webp", metric: "qualification", metricName: "Passing check records", unit: "cumulative · simulated" },
    thermal: { title: "Thermal throttling", takeaway: "Utilization is not useful output. A controlled remote action must improve both hardware health and the affected workload.", caption: "Find the thermal limit behind a slower workload.", image: "fleet-observatory.webp", metric: "temperature", metricName: "GPU temperature", unit: "synthetic °C" },
    telemetry: { title: "Telemetry loss", takeaway: "Treat missing data as unknown. Restore visibility before allowing a hardware diagnosis or declaring recovery.", caption: "When the signal disappears, uncertainty becomes visible.", image: "fleet-observatory.webp", metric: "freshness", metricName: "Time since last observation", unit: "lab clock seconds" },
    "worker-crash": { title: "Workflow worker restart", takeaway: "A worker may stop after committing an effect. Durable state and idempotent effects make resumption safe.", caption: "The worker stops. The repair intent survives.", image: "optic-repair-journey.webp", metric: "tickets", metricName: "Repair tickets created", unit: "persisted rows" },
    storage: { title: "Storage latency", takeaway: "Storage is part of the GPU performance path. Track the effect on workload progress and validate the replacement.", caption: "A data bottleneck can strand expensive compute.", image: "optic-repair-journey.webp", metric: "storage_latency_ms", metricName: "Storage latency", unit: "synthetic ms" }
  };
  const tech = {
    collect: { label: "THE COLLECTION PLANE", title: "Identity is the join.", description: "Preserve asset, tenant, cluster, workload, and time relationships. Metrics, logs, and spans become useful together when the investigator can follow the same incident across them.", example: "asset_id → incident_id → trace_id\nobserved_at ≠ ingested_at\nsource = simulated hardware", boundary: "Lab choice: bounded metric labels; richer incident context in structured events. OpenTelemetry is the optional delivery path, not evidence that physical hardware was measured.", source: "https://opentelemetry.io/docs/collector/" },
    investigate: { label: "THE OBSERVABILITY PLANE", title: "Find the slowest boundary.", description: "Use metric changes to locate the time window, structured events to explain the transition, and a correlated trace to inspect the operating sequence. Ask what happened to the workload before acting on a component.", example: "Prometheus metrics → Mimir\nStructured events → Loki\nIncident spans → Tempo → Grafana", boundary: "Public research: Nscale names Mimir, Loki, and Tempo. Project design: the repository's optional integration profile. These backends do not run in this static page.", source: "https://www.nscale.com/blog/fleet-operations" },
    coordinate: { label: "THE ACTION PLANE", title: "A repair needs a memory.", description: "Persist incident stage and approved procedure, isolate the asset, deduplicate effects, and wait for the human boundary. Worker restarts must not create another ticket or return an unverified asset.", example: "isolate → diagnose → remediate\nwait_repair → validate → done\noperation_id guards repeated effects", boundary: "Executable lab: a transactional SQLite state machine. Optional Temporal integration demonstrates a durable workflow boundary. Nscale publicly names Temporal; its internal workflow implementation is unpublished.", source: "https://docs.temporal.io/workflow-execution" },
    verify: { label: "THE OUTCOME PLANE", title: "Recovery must be earned.", description: "A closed ticket records an action. Qualification and workload checks establish the result. Keep failed revalidation quarantined, retain history, and quantify the useful capacity at risk.", example: "repair completion ≠ healthy capacity\npolicy pass + fresh health → available\ncapacity exposure ≠ cash savings", boundary: "Lab results distinguish simulated checks from skipped physical GPU tests. Cost uses an explicit assumed hourly value; it is not Nscale pricing or a measured financial saving.", source: "https://www.nscale.com/blog/the-gpu-fleet-that-fixes-itself" }
  };

  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    window.clearTimeout(toast.timeout);
    toast.timeout = window.setTimeout(() => { $("toast").hidden = true; }, 6500);
  }

  function scenarios() { return state.evidence?.scenarios || []; }
  function replay() { return scenarios().find(s => s.id === state.scenario); }
  function frames() { return replay()?.frames || []; }
  function latestIncident(snapshot) {
    if (!snapshot) return null;
    const list = [...(snapshot.incidents || [])].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (state.api && state.apiTargetAssetId) return list.find(i => i.asset_id === state.apiTargetAssetId) || null;
    if (["burnin", "telemetry"].includes(state.scenario)) return null;
    return list.find(i => i.kind === (state.scenario === "worker-crash" ? "optic" : state.scenario)) || null;
  }
  function selectedAsset(snapshot) {
    if (!snapshot) return null;
    const incident = latestIncident(snapshot);
    const laterIncident = frames().map(f => latestIncident(f.snapshot)).find(Boolean);
    const eventAsset = [...(snapshot.events || [])].reverse().find(e => e.asset_id && ["telemetry.stale", "qualification.failed", "fault.injected", "qualification.started", "workload.allocated"].includes(e.kind))?.asset_id;
    const replayAsset = frames().flatMap(f => f.snapshot?.events || []).find(e => e.asset_id && ["qualification.started", "workload.allocated", "fault.injected", "telemetry.stale"].includes(e.kind))?.asset_id;
    const assetId = (state.api ? state.apiTargetAssetId : null) || incident?.asset_id || laterIncident?.asset_id || (!state.api && replayAsset) || eventAsset;
    return (snapshot.assets || []).find(a => a.id === assetId) || (snapshot.assets || []).find(a => a.fault || a.health === "unknown" || a.lifecycle === "quarantined") || snapshot.assets?.[0] || null;
  }
  function selectedWorkload(snapshot, asset) {
    return [...(snapshot?.workloads || [])].reverse().find(w => (w.assets || []).includes(asset?.id)) || null;
  }
  function human(value) { return value ? String(value).replaceAll("_", " ").replaceAll("-", " ") : "No active incident"; }
  function currentData() {
    if (state.api) return { label: "Local backend snapshot", caption: "A real API response from your running synthetic lab.", snapshot: state.apiSnapshot };
    return frames()[state.frame] || null;
  }

  function determineStage(snapshot, asset, incident) {
    if (!snapshot) return "observe";
    if (asset?.health === "unknown") return "unknown";
    if (incident?.stage === "done" || incident?.status === "resolved") return "complete";
    if (incident?.stage === "validate" || asset?.lifecycle === "validating") return "verify";
    if (["wait_repair", "remediate"].includes(incident?.stage) || asset?.lifecycle === "repairing") return "repair";
    if (asset?.lifecycle === "quarantined") return "quarantine";
    if (incident || asset?.health === "degraded") return "detect";
    if (!state.api && state.frame === frames().length - 1 && asset?.health === "healthy") return "complete";
    if (state.frame > 0 && asset?.lifecycle === "available") return "complete";
    return "observe";
  }

  function decisionFor(snapshot, asset, incident) {
    if (!snapshot) return "No execution record is available. The page does not invent a measured result.";
    if (asset?.health === "unknown") return "Telemetry is stale or missing. Withhold automated hardware diagnosis until fresh evidence returns.";
    if (incident?.status === "escalated") return "The bounded repair path did not establish recovery. Retain isolation and require further investigation.";
    if (incident?.stage === "wait_repair") return "Physical work is a human boundary. Keep the asset withheld, retain the procedure and location, and wait for repair completion.";
    if (incident?.stage === "validate") return "A completed action is not a passed quality gate. Run the policy again before returning this asset.";
    if (incident?.status === "resolved") return "The repair reached a verified outcome. Preserve its history and the measurements that justified return to service.";
    if (incident?.stage === "isolate") return "Persistent symptoms crossed the lab policy. Protect the affected workload before attempting remediation.";
    if (incident?.stage === "diagnose") return "Use component and workload evidence to select a versioned, allowlisted repair procedure.";
    if (incident?.stage === "remediate") return "Execute only the selected bounded action. Persist its outcome so retries can resume safely.";
    if (asset?.lifecycle === "quarantined") return "Qualification has not established acceptable capacity. Withhold allocation and retain the failed check.";
    if (state.scenario === "burnin") return "Record every qualification check and its provenance. Physical GPU burn-in is skipped in this hardware-free lab.";
    if (asset?.health === "degraded") return "One signal is insufficient for disruptive action. The lab requires persistent observations before opening an incident.";
    return "Fresh health, lifecycle eligibility, and workload context are evaluated together. Availability is an explicit policy decision.";
  }

  function metricValue(snapshot) {
    if (!snapshot) return null;
    const config = scenarioInfo[state.scenario];
    const a = selectedAsset(snapshot);
    if (!a) return null;
    if (config.metric === "qualification") return (snapshot.qualification || []).filter(q => q.asset_id === a.id && q.status === "pass").length;
    if (config.metric === "freshness") return Math.max(0, (snapshot.generated_at || a.last_seen) - a.last_seen);
    if (config.metric === "tickets") return (snapshot.tickets || []).filter(t => (snapshot.incidents || []).some(i => i.id === t.incident_id && i.asset_id === a.id)).length;
    const n = Number(a[config.metric]);
    return Number.isFinite(n) ? n : null;
  }

  function renderChart() {
    const config = scenarioInfo[state.scenario];
    $("chart-title").textContent = config.metricName;
    $("chart-unit").textContent = config.unit;
    const data = state.api ? [{ value: metricValue(state.apiSnapshot), label: "Current API snapshot" }] : frames().map((f, index) => ({ value: metricValue(f.snapshot), label: f.label, index }));
    const valid = data.filter(d => d.value !== null);
    const holder = $("chart-content");
    holder.replaceChildren();
    if (!valid.length) {
      const p = document.createElement("p"); p.className = "empty-chart"; p.textContent = "No measured replay values available for this signal."; holder.append(p);
      $("chart-note").textContent = "No production telemetry is displayed.";
      return;
    }
    const nowIndex = state.api ? 0 : state.frame;
    const visible = valid.filter(d => state.api || d.index <= nowIndex);
    const max = Math.max(...valid.map(d => d.value), 1) * 1.14;
    const W = 360, H = 90, P = 8;
    const x = i => data.length <= 1 ? W / 2 : P + i * (W - 2 * P) / (data.length - 1);
    const y = v => H - P - v / max * (H - 2 * P);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("role", "img");
    const current = state.api ? valid[0] : data[nowIndex];
    svg.setAttribute("aria-label", `${config.metricName}: ${current?.value == null ? "unavailable" : format(current.value)} ${config.unit} at phase ${nowIndex + 1}. Values are from synthetic lab execution.`);
    [0.2, 0.6, 1].forEach(f => { const line = document.createElementNS(ns, "line"); line.setAttribute("x1", "0"); line.setAttribute("x2", String(W)); line.setAttribute("y1", String(H - P - (H - P * 2) * f)); line.setAttribute("y2", line.getAttribute("y1")); line.setAttribute("class", "chart-grid"); svg.append(line); });
    if (visible.length) {
      const points = visible.map(d => `${x(d.index || 0)},${y(d.value)}`);
      if (points.length > 1) {
        const firstX = x(visible[0].index || 0), lastX = x(visible[visible.length - 1].index || 0);
        const area = document.createElementNS(ns, "path"); area.setAttribute("d", `M ${firstX},${H - P} L ${points.join(" L ")} L ${lastX},${H - P} Z`); area.setAttribute("class", "chart-area"); svg.append(area);
        const line = document.createElementNS(ns, "polyline"); line.setAttribute("points", points.join(" ")); line.setAttribute("class", "chart-line"); svg.append(line);
      }
      visible.forEach(d => { const circle = document.createElementNS(ns, "circle"); circle.setAttribute("cx", String(x(d.index || 0))); circle.setAttribute("cy", String(y(d.value))); circle.setAttribute("r", d.index === nowIndex || state.api ? "4.5" : "3"); circle.setAttribute("class", `chart-point${d.index === nowIndex || state.api ? " current" : ""}`); svg.append(circle); });
    }
    holder.append(svg);
    const labels = document.createElement("div"); labels.className = "chart-labels";
    const l = document.createElement("span"); l.textContent = state.api ? "CURRENT SNAPSHOT" : "START";
    const r = document.createElement("span"); r.textContent = current?.value == null ? "VALUE UNAVAILABLE" : `CURRENT ${format(current.value)}`;
    labels.append(l, r); holder.append(labels);
    $("chart-note").textContent = state.api ? "Real API record · synthetic hardware input" : "Recorded execution · synthetic hardware input · phase axis";
  }

  function format(value) { return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value); }

  function renderProgress() {
    const holder = $("play-progress"); holder.replaceChildren();
    if (state.api) return;
    frames().forEach((f, index) => {
      const button = document.createElement("button"); button.className = `progress-dot${index === state.frame ? " active" : index < state.frame ? " complete" : ""}`;
      button.type = "button"; button.setAttribute("aria-label", `Replay phase ${index + 1}: ${f.label}`); button.setAttribute("aria-current", index === state.frame ? "step" : "false");
      button.addEventListener("click", () => { pause(); state.frame = index; render(); }); holder.append(button);
    });
  }

  function render() {
    const config = scenarioInfo[state.scenario];
    const frame = currentData();
    const snapshot = frame?.snapshot;
    const asset = selectedAsset(snapshot);
    const incident = latestIncident(snapshot);
    const ticket = snapshot?.tickets?.find(t => t.incident_id === incident?.id);
    const workload = selectedWorkload(snapshot, asset);
    document.querySelectorAll("[data-scenario]").forEach(b => { const active = b.dataset.scenario === state.scenario; b.setAttribute("aria-selected", String(active)); b.tabIndex = active ? 0 : -1; });
    $("scenario-panel").setAttribute("aria-labelledby", `tab-${state.scenario}`);
    $("scenario-panel").dataset.phase = String(state.frame);
    $("phase-count").textContent = state.api ? "LOCAL API MODE" : frames().length ? `PHASE ${String(state.frame + 1).padStart(2, "0")} / ${String(frames().length).padStart(2, "0")}` : "EVIDENCE PENDING";
    $("phase-eyebrow").textContent = state.api ? "LOCAL EXECUTION RECORD" : "RECORDED EXECUTION EVIDENCE";
    $("phase-title").textContent = frame?.label || "Execution evidence pending";
    $("phase-description").textContent = frame?.caption || "The source includes the executable lab. This page needs its generated evidence.json before a recorded scenario can be replayed.";
    $("asset-state").textContent = asset ? `${human(asset.lifecycle)} · ${human(asset.health)}` : "Unverified";
    $("workflow-state").textContent = incident ? `${human(incident.stage)} · ${human(incident.status)}` : snapshot ? "No active incident" : "Unverified";
    $("workload-step").textContent = workload ? `${format(workload.step_ms)} ms` : snapshot ? "No active job" : "—";
    $("eligible-gpus").textContent = snapshot?.summary ? String(snapshot.summary.available_gpus) : "—";
    $("cost-exposure").textContent = snapshot?.cost ? `$${format(snapshot.cost.capacity_exposure_usd)}` : "—";
    $("decision-text").textContent = decisionFor(snapshot, asset, incident);
    $("takeaway-text").textContent = config.takeaway;
    $("scene-caption").textContent = frame?.caption || config.caption;
    $("scene-state-pill").replaceChildren();
    const dot = document.createElement("span"); dot.className = "status-dot";
    const stateLabel = document.createElement("span"); stateLabel.textContent = asset ? `${asset.id} · ${human(asset.lifecycle)}` : "Execution evidence pending";
    $("scene-state-pill").append(dot, stateLabel);
    const image = $("operation-image");
    if (!image.src.endsWith(config.image)) image.src = `assets/${config.image}`;
    image.alt = state.scenario === "burnin" ? "Illustrated GPU qualification lab with testing and isolation areas" : state.scenario === "thermal" || state.scenario === "telemetry" ? "Illustrated datacenter with robots observing GPU racks and cooling infrastructure" : "Illustrated investigation, component replacement, and recovery verification";
    $("operation-scene").dataset.stage = determineStage(snapshot, asset, incident);
    const latestEvents = snapshot?.events?.filter(e => !asset || e.asset_id === asset.id).slice(-4);
    $("raw-evidence").textContent = snapshot ? JSON.stringify({ mode: snapshot.mode, policy_version: snapshot.policy_version, asset, incident: incident || null, ticket: ticket || null, workload, qualification: (snapshot.qualification || []).filter(q => q.asset_id === asset?.id).map(q => ({ test: q.test, status: q.status, provenance: q.provenance })), cost: snapshot.cost, latest_events: latestEvents }, null, 2) : "No execution evidence loaded. No result has been fabricated.";
    const hasFrames = frames().length > 0;
    $("play-button").disabled = !hasFrames || Boolean(state.api);
    $("step-button").disabled = !hasFrames || Boolean(state.api);
    $("restart-button").disabled = !hasFrames || Boolean(state.api);
    $("play-label").textContent = state.playing ? "Pause story" : state.frame === frames().length - 1 && hasFrames ? "Replay story" : "Play story";
    $("play-icon").textContent = state.playing ? "Ⅱ" : "▶";
    $("mode-text").textContent = state.api ? "Connected local lab · synthetic hardware" : hasFrames ? "Recorded synthetic lab replay" : "Execution evidence pending";
    $("playback-note").textContent = state.api ? "API actions execute against your local lab. The artwork remains an explanation." : "Animated concept illustration above · recorded code execution beside it";
    $("api-actions").hidden = !state.api;
    $("api-advance").hidden = !incident || incident.status !== "open";
    $("api-advance").disabled = state.busy || !incident || incident.status !== "open" || incident.stage === "wait_repair" && ticket?.status !== "completed";
    $("api-ticket").hidden = !ticket || ticket.status !== "open";
    $("api-ticket").disabled = state.busy;
    $("api-check").hidden = !asset;
    $("api-check").textContent = state.scenario === "burnin" && !incident ? "Requalify idle asset" : "Record fresh lab observation";
    $("api-check").disabled = state.busy;
    $("api-release").hidden = !workload || workload.status === "released" || Boolean(incident && incident.status !== "resolved");
    $("api-release").disabled = state.busy;
    renderChart(); renderProgress();
  }

  function pause() { state.playing = false; clearInterval(state.timer); state.timer = null; }
  function play() {
    if (!frames().length || state.api) return;
    if (state.frame >= frames().length - 1) state.frame = 0;
    state.playing = true;
    state.timer = window.setInterval(() => {
      if (state.frame < frames().length - 1) state.frame++;
      else pause();
      render();
    }, 5200);
    render();
  }

  async function chooseScenario(id) {
    if (!scenarioInfo[id] || state.busy) return;
    pause(); const priorScenario = state.scenario; const priorTarget = state.apiTargetAssetId; state.scenario = id; state.frame = 0;
    if (state.api) {
      state.busy = true; render();
      try { state.apiSnapshot = await request("/api/scenarios", "POST", { scenario: id }); state.apiTargetAssetId = [...(state.apiSnapshot.events || [])].reverse().find(e => e.asset_id)?.asset_id || null; toast("Scenario executed in the local lab."); }
      catch (error) { state.scenario = priorScenario; state.apiTargetAssetId = priorTarget; toast(error.message); }
      finally { state.busy = false; }
    }
    render();
  }

  async function request(path, method = "GET", body) {
    const options = { method, headers: { "Authorization": `Bearer ${state.api.token}`, "Content-Type": "application/json" }, signal: AbortSignal.timeout(10000) };
    if (body !== undefined) options.body = JSON.stringify(body);
    const response = await fetch(`${state.api.base}${path}`, options);
    let result;
    try { result = await response.json(); } catch { throw new Error(`The lab returned HTTP ${response.status} without a JSON response.`); }
    if (!response.ok) throw new Error(result.error || result.message || `Lab request failed: HTTP ${response.status}`);
    return result.snapshot || result;
  }

  async function apiAction(type) {
    if (state.busy || !state.api) return;
    const snapshot = state.apiSnapshot, incident = latestIncident(snapshot), asset = selectedAsset(snapshot), workload = selectedWorkload(snapshot, asset);
    const ticket = snapshot?.tickets?.find(t => t.incident_id === incident?.id && t.status === "open");
    if (["advance", "ticket"].includes(type) && !incident || type === "ticket" && !ticket || type === "check" && !asset || type === "release" && !workload) return;
    state.busy = true; render();
    try {
      if (type === "ticket") state.apiSnapshot = await request(`/api/tickets/${encodeURIComponent(ticket.id)}/complete`, "POST", { success: true });
      else if (type === "advance") state.apiSnapshot = await request(`/api/incidents/${encodeURIComponent(incident.id)}/advance`, "POST", { expected_version: incident.version });
      else if (type === "release") state.apiSnapshot = await request(`/api/workloads/${encodeURIComponent(workload.id)}/release`, "POST", {});
      else state.apiSnapshot = state.scenario === "burnin" && !incident ? await request(`/api/assets/${encodeURIComponent(asset.id)}/commission`, "POST", {}) : await request(`/api/assets/${encodeURIComponent(asset.id)}/observe`, "POST", { fresh: true });
      toast(type === "ticket" ? "Mock technician completion recorded. Revalidation still determines recovery." : type === "check" ? "Fresh synthetic lab evidence recorded." : type === "release" ? "Lab workload released and idle baseline rechecked." : "One workflow transition committed.");
    } catch (error) { toast(error.message); try { state.apiSnapshot = await request("/api/fleet"); } catch { /* Preserve the last known snapshot on connection failure. */ } }
    finally { state.busy = false; render(); }
  }

  function selectTech(id) {
    const item = tech[id]; if (!item) return;
    document.querySelectorAll("[data-tech]").forEach(b => { const active = b.dataset.tech === id; b.setAttribute("aria-selected", String(active)); b.tabIndex = active ? 0 : -1; });
    $("tech-panel").setAttribute("aria-labelledby", `tech-${id}`);
    $("tech-label").textContent = item.label; $("tech-title").textContent = item.title; $("tech-description").textContent = item.description;
    $("tech-example").textContent = item.example; $("tech-example").style.whiteSpace = "pre-line";
    $("tech-boundary").textContent = item.boundary; $("tech-source").href = item.source;
  }

  function setMotion(enabled) {
    state.motion = enabled; document.body.classList.toggle("motion-paused", !enabled);
    $("motion-toggle").setAttribute("aria-pressed", String(enabled)); $("motion-toggle").textContent = enabled ? "Animation on" : "Animation off";
  }

  function renderVerification() {
    const v = state.evidence?.verification;
    const fields = ["tests_run", "failures", "errors", "skipped"];
    const complete = v && typeof v.test_command === "string" && fields.every(key => Number.isInteger(v[key]) && v[key] >= 0);
    $("verification-panel").hidden = !complete;
    $("verification-facts").replaceChildren();
    if (!complete) return;
    const facts = [
      ["Tests executed", String(v.tests_run)],
      ["Failures / errors / skipped", `${v.failures} / ${v.errors} / ${v.skipped}`],
      ["Command", v.test_command],
      ["Recorded at", v.recorded_at || "Not recorded"],
      ["Source revision", v.source_revision || "Not recorded"]
    ];
    facts.forEach(([name, value]) => { const dt = document.createElement("dt"); dt.textContent = name; const dd = document.createElement("dd"); dd.textContent = value; $("verification-facts").append(dt, dd); });
  }

  function keyboardTabs(event, selector, attribute) {
    if (!["ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    const list = [...document.querySelectorAll(selector)]; const current = list.indexOf(event.target); if (current < 0) return;
    event.preventDefault();
    const forward = ["ArrowRight", "ArrowDown"].includes(event.key);
    const next = event.key === "Home" ? 0 : event.key === "End" ? list.length - 1 : (current + (forward ? 1 : -1) + list.length) % list.length;
    list[next].focus(); if (attribute === "scenario") chooseScenario(list[next].dataset.scenario); else selectTech(list[next].dataset.tech);
  }

  document.querySelectorAll("[data-scenario]").forEach(button => { button.addEventListener("click", () => chooseScenario(button.dataset.scenario)); button.addEventListener("keydown", e => keyboardTabs(e, "[data-scenario]", "scenario")); });
  document.querySelectorAll("[data-tech]").forEach(button => { button.addEventListener("click", () => selectTech(button.dataset.tech)); button.addEventListener("keydown", e => keyboardTabs(e, "[data-tech]", "tech")); });
  document.querySelectorAll(".scenario-jump").forEach(button => button.addEventListener("click", () => { chooseScenario(button.dataset.target); $("experience").scrollIntoView({ behavior: reducedMotion.matches ? "instant" : "smooth" }); }));
  $("show-burnin").addEventListener("click", () => chooseScenario("burnin"));
  $("play-button").addEventListener("click", () => { if (state.playing) { pause(); render(); } else play(); });
  $("step-button").addEventListener("click", () => { pause(); state.frame = (state.frame + 1) % frames().length; render(); });
  $("restart-button").addEventListener("click", () => { pause(); state.frame = 0; render(); });
  $("motion-toggle").addEventListener("click", () => setMotion(!state.motion));
  reducedMotion.addEventListener("change", e => { if (e.matches) { pause(); setMotion(false); render(); } });
  document.addEventListener("visibilitychange", () => { if (document.hidden) { pause(); render(); } });
  $("connect-open").addEventListener("click", () => { pause(); render(); $("connect-status").textContent = ""; $("connect-dialog").showModal(); });
  $("connect-close").addEventListener("click", () => $("connect-dialog").close());
  $("connect-dialog").addEventListener("click", e => { if (e.target === $("connect-dialog")) { const r = e.target.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close(); } });
  $("connect-form").addEventListener("submit", async e => {
    e.preventDefault(); const prior = state.api;
    let url;
    try { url = new URL($("api-base").value); }
    catch { $("connect-status").textContent = "Enter a valid loopback backend URL."; return; }
    if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) { $("connect-status").textContent = "Connect only to a loopback address for this local lab."; return; }
    if (!["http:", "https:"].includes(url.protocol)) { $("connect-status").textContent = "Use an HTTP or HTTPS backend URL."; return; }
    state.api = { base: url.origin, token: $("api-token").value };
    $("connect-status").textContent = "Connecting…";
    try {
      const snapshot = await request("/api/fleet");
      if (!Array.isArray(snapshot.assets)) throw new Error("The endpoint did not return a fleet snapshot.");
      state.apiSnapshot = snapshot;
      const ordered = [...(snapshot.incidents || [])].sort((a,b) => b.created_at-a.created_at);
      const liveIncident = ordered.find(i => !["resolved","retired"].includes(i.status)) || ordered[0];
      state.apiTargetAssetId = liveIncident?.asset_id || null;
      if (liveIncident && scenarioInfo[liveIncident.kind]) state.scenario = liveIncident.kind;
      $("connect-dialog").close(); $("api-token").value = ""; render(); toast("Connected to the local synthetic lab. Choose a scenario to execute it.");
    } catch (error) { state.api = prior; $("connect-status").textContent = `${error.message} Start the backend and check its CORS origin configuration.`; }
  });
  $("disconnect-button").addEventListener("click", () => { state.api = null; state.apiSnapshot = null; state.apiTargetAssetId = null; $("api-token").value = ""; $("connect-dialog").close(); state.frame = 0; render(); toast("Using the checked-in recorded replay."); });
  $("api-advance").addEventListener("click", () => apiAction("advance"));
  $("api-ticket").addEventListener("click", () => apiAction("ticket"));
  $("api-check").addEventListener("click", () => apiAction("check"));
  $("api-release").addEventListener("click", () => apiAction("release"));

  async function initialize() {
    selectTech("collect"); setMotion(!reducedMotion.matches); render();
    try {
      const response = await fetch("evidence.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const evidence = await response.json();
      if (evidence.schema_version !== 1 || !Array.isArray(evidence.scenarios)) throw new Error("Unsupported evidence schema");
      const good = evidence.scenarios.filter(s => scenarioInfo[s.id] && Array.isArray(s.frames) && s.frames.every(f => f.snapshot && Array.isArray(f.snapshot.assets)));
      state.evidence = { ...evidence, scenarios: good };
      $("evidence-status").textContent = `${good.length} recorded scenarios loaded from evidence.json. Hardware readings and workloads are synthetic; lifecycle transitions, policy decisions, database records, and API code are executable. Physical GPU burn-in is not measured by this replay.`;
      renderVerification();
      render();
    } catch (error) {
      $("evidence-status").textContent = `Execution evidence is unavailable (${error.message}). Run the evidence-generation command in the repository and serve this directory over HTTP. The page does not fabricate replay results.`;
      render();
    }
  }
  initialize();
})();
