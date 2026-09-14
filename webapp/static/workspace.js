import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch } from "./api.js";
import { planBelongsToHost, belongsToHost, newestFirst } from "./host_scope.js";
import { renderDiscoverStage } from "./stages/discover.js";
import { renderReadinessStage } from "./stages/readiness.js";
import { renderPlanStage } from "./stages/plan.js";
import { renderExecuteStage } from "./stages/execute.js";
import { renderRecoveryStage } from "./stages/recovery.js";

const STAGES = [
  { id: "discover", label: "Discover", hint: "Live SSH · phases A–E" },
  { id: "readiness", label: "Readiness", hint: "Reconcile → evaluate" },
  { id: "plan", label: "Plan", hint: "Seal · approve · authorize" },
  { id: "execute", label: "Execute", hint: "Dispatch · tasks" },
  { id: "recovery", label: "Recovery", hint: "RMAN evidence" },
];

export async function renderWorkspace(mount, hostId, stage) {
  mount.innerHTML = "";

  const stageStatuses = await loadStageStatuses(hostId);

  const header = el("header", { class: "ws-header" }, [
    el("div", { class: "ws-header-text" }, [
      el("p", { class: "ws-kicker", text: "Patching workspace" }),
      el("h1", { class: "ws-title", text: hostId }),
      el("p", { class: "ws-sub", text: "Live discovery, readiness gates, sealed plan, then execute — one host at a time." }),
    ]),
  ]);
  mount.appendChild(header);

  const rail = el("nav", { class: "stage-rail", "aria-label": "Lifecycle" });
  for (const s of STAGES) {
    const st = stageStatuses[s.id] || { kind: "neutral", text: "—" };
    const link = el("a", {
      class: `stage-rail-item${s.id === stage ? " is-active" : ""}`,
      ...(s.id === stage ? { "aria-current": "step" } : {}),
      href: `#/hosts/${encodeURIComponent(hostId)}/${s.id}`,
    }, [
      el("span", { class: "stage-rail-label", text: s.label }),
      el("span", { class: "stage-rail-hint", text: s.hint }),
      badge(st.text, st.kind),
    ]);
    rail.appendChild(link);
  }
  mount.appendChild(rail);

  const stageMount = el("div", { class: "ws-stage" });
  mount.appendChild(stageMount);

  if (stage === "discover") await renderDiscoverStage(stageMount, hostId);
  else if (stage === "readiness") await renderReadinessStage(stageMount, hostId);
  else if (stage === "plan") await renderPlanStage(stageMount, hostId);
  else if (stage === "execute") await renderExecuteStage(stageMount, hostId);
  else if (stage === "recovery") await renderRecoveryStage(stageMount, hostId);
  else await renderDiscoverStage(stageMount, hostId);
}

async function loadStageStatuses(hostId) {
  const out = {
    discover: { kind: "neutral", text: "idle" },
    readiness: { kind: "neutral", text: "idle" },
    plan: { kind: "neutral", text: "idle" },
    execute: { kind: "neutral", text: "idle" },
    recovery: { kind: "neutral", text: "idle" },
  };

  try {
    const res = await apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/pipeline`);
    const data = await res.json();
    const steps = data.steps || [];
    const byId = Object.fromEntries(steps.map((s) => [s.step, s]));

    const disc = byId.discovery;
    if (disc?.done) {
      const status = disc.phases_status || disc.status || "done";
      out.discover = { kind: classifyStatus(status), text: String(status) };
    }

    const ready = byId["readiness-evaluate"];
    const midSteps = ["reconcile", "artifact-inspect", "procedure-validate", "compatibility-collect", "compatibility-reconcile"];
    const midDone = midSteps.filter((id) => byId[id]?.done).length;
    if (ready?.done) {
      out.readiness = { kind: classifyStatus(ready.status), text: String(ready.status || "done") };
    } else if (midDone > 0) {
      out.readiness = { kind: "warn", text: `${midDone}/6` };
    }
  } catch (error) {
    // Authentication and cancellation must reach the page boundary.
    throw error;
  }

  try {
    const res = await apiFetch("/api/plans");
    const data = await res.json();
    const hostPlans = newestFirst(data.plans || []).filter((p) => planBelongsToHost(p, hostId));
    if (hostPlans.length) {
      const latest = hostPlans[0];
      const state = latest.state || "unknown";
      out.plan = { kind: classifyStatus(state), text: state };
      if (["running", "paused", "execution_authorized", "succeeded", "failed"].includes(state)) {
        out.execute = { kind: classifyStatus(state), text: state };
      }
    }
  } catch (error) {
    // Authentication and cancellation must reach the page boundary.
    throw error;
  }

  try {
    const res = await apiFetch("/api/recovery");
    const data = await res.json();
    const requests = newestFirst(data.requests || []).filter((request) => belongsToHost(request, hostId));
    if (requests.length) {
      const latest = requests[0];
      out.recovery = { kind: classifyStatus(latest.state), text: latest.state || "present" };
    }
  } catch (error) {
    // Authentication and cancellation must reach the page boundary.
    throw error;
  }

  return out;
}
