import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch, getReadSignal } from "./api.js";
import { runToCompletion } from "./runs.js";

export function duration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "Unknown";
  const value = Math.floor(seconds);
  return value >= 3600 ? `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m` : value >= 60 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${value}s`;
}

export function heartbeatSummary(run, now = Date.now() / 1000) {
  const observed = run?.observation;
  const heartbeat = observed?.worker_heartbeat;
  const poll = run?.controller_poll;
  const contact = poll?.observed_at ? `${poll.state} · ${duration(Math.max(0, now - poll.observed_at))} ago` : "No controller poll recorded";
  if (!heartbeat?.available) return { controller: contact, worker: "Native worker heartbeat unknown" };
  const renewed = heartbeat.last_renewed_at;
  const age = renewed && observed.remote_clock && observed.received_at
    ? Math.max(0, observed.remote_clock - renewed + now - observed.received_at) : null;
  return { controller: contact, worker: age === null ? "No native lease renewal observed"
    : `Native lease last renewed ${duration(age)} ago · observed task ${heartbeat.task_status || "unknown"}` };
}

export function executionConsole(planId) {
  const section = el("section", { class: "panel", "aria-label": "Execution dashboard" });
  const title = el("h2", { text: "Execution dashboard" });
  const controls = el("div", { class: "pipeline-controls" });
  const refresh = el("button", { type: "button", text: "Refresh timeline" });
  const observe = el("button", { type: "button", text: "Refresh native logs" });
  const follow = el("input", { type: "checkbox" });
  const status = el("p", { class: "helper-text", role: "status", "aria-live": "polite", text: "Loading saved execution history…" });
  const content = el("div");
  const logSelect = el("select", { "aria-label": "Native log" });
  const log = el("pre", { class: "run-log", text: "No native log observation yet." });
  let selectedRun = null;
  let lastNative = 0;
  let pending = false;
  let timer;
  const signal = getReadSignal();
  controls.appendChild(refresh); controls.appendChild(observe);
  controls.appendChild(el("label", {}, [follow, document.createTextNode(" Follow native logs while running")]));
  section.appendChild(title); section.appendChild(controls); section.appendChild(status); section.appendChild(content);
  section.appendChild(el("details", {}, [el("summary", { text: "Native logs · bounded and redacted" }), logSelect, log]));
  function showLog() {
    const entry = selectedRun?.observation?.logs?.[logSelect.value];
    log.textContent = entry?.text || entry?.reason || "No native log observation yet.";
  }
  function draw(data) {
    content.innerHTML = "";
    status.textContent = data.guidance;
    const runs = (data.runs || []).filter(run => run.can_observe);
    selectedRun = runs.at(-1) || null;
    observe.disabled = !selectedRun || pending;
    if (selectedRun) {
      const heartbeat = heartbeatSummary(selectedRun);
      content.appendChild(el("p", { text: `Run ${selectedRun.run_id} · ${selectedRun.status} · elapsed ${duration(selectedRun.elapsed_seconds)}` }));
      content.appendChild(el("p", { class: "helper-text", text: `Controller contact: ${heartbeat.controller}. ${heartbeat.worker}.` }));
      content.appendChild(el("p", { class: "helper-text", text: "Logs and heartbeat observations do not verify task completion. Use Inspect and reconcile after a disconnect or unknown outcome." }));
    }
    const tasks = el("ol", { "aria-label": "Persistent task timeline" });
    for (const task of data.tasks || []) {
      const started = task.claimed_at_epoch;
      const elapsed = started ? duration(Math.max(0, (task.completed_at_epoch || Date.now() / 1000) - started)) : "Not started";
      tasks.appendChild(el("li", {}, [el("strong", { text: `${task.stage || task.task_id} · ${task.node || "unknown node"} ` }),
        badge(task.status || "unknown", classifyStatus(task.status)), document.createTextNode(` · ${elapsed}${task.evidence_verified ? " · evidence verified" : ""}`)]));
    }
    content.appendChild(tasks);
    const events = el("ol");
    for (const event of (data.timeline || []).slice(-40)) {
      events.appendChild(el("li", { text: `${event.at ? new Date(event.at * 1000).toLocaleString() : "Unknown time"} · ${event.message || event.event}${event.task_id ? ` · ${event.task_id}` : ""}` }));
    }
    content.appendChild(el("details", {}, [el("summary", { text: "Saved controller events" }), events]));
    const selected = logSelect.value;
    logSelect.innerHTML = "";
    for (const name of Object.keys(selectedRun?.observation?.logs || {})) logSelect.appendChild(el("option", { value: name, text: name }));
    if (selectedRun?.observation?.logs?.[selected]) logSelect.value = selected;
    showLog();
  }
  async function native() {
    if (!selectedRun || pending) return;
    pending = true; observe.disabled = true;
    try {
      const result = await runToCompletion(`/api/plans/${encodeURIComponent(planId)}/execution-observe`, { run_id: selectedRun.run_id });
      if (result.status !== "succeeded") throw new Error(result.error?.message || "Native observation could not complete");
      lastNative = Date.now();
      await load(false);
    } catch (error) {
      status.textContent = `Native observation unavailable: ${error.message}. Existing execution continues independently.`;
      follow.checked = false;
    } finally { pending = false; observe.disabled = !selectedRun; }
  }
  async function load(schedule = true) {
    if (signal?.aborted) return;
    clearTimeout(timer);
    try {
      const response = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/execution`, { signal });
      const data = await response.json(); draw(data);
      const active = selectedRun && ["queued", "running"].includes(selectedRun.status);
      if (active && follow.checked && !pending && Date.now() - lastNative > 15000) await native();
      if (schedule && section.isConnected) {
        timer = setTimeout(() => { if (section.isConnected) void load(); }, active ? 5000 : 15000);
        timer?.unref?.();
      }
    } catch (error) {
      if (error.name !== "AbortError") status.textContent = `Saved execution view unavailable: ${error.message}`;
    }
  }
  refresh.addEventListener("click", () => load());
  observe.addEventListener("click", native);
  logSelect.addEventListener("change", showLog);
  follow.addEventListener("change", () => { if (follow.checked) void load(); });
  void load();
  return section;
}
