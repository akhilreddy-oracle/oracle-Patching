import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch, getReadSignal } from "./api.js";
import { planBelongsToHost, belongsToHost, newestFirst } from "./host_scope.js";
import { renderDiscoverStage } from "./stages/discover.js";
import { renderReadinessStage } from "./stages/readiness.js";
import { renderPlanStage } from "./stages/plan.js";
import { renderExecuteStage } from "./stages/execute.js";
import { renderRecoveryStage } from "./stages/recovery.js";

import { WIZARD_STAGES as STAGES, wizardContext, targetSummary } from "./patch_wizard.js";

export async function renderWorkspace(mount, hostId, stage) {
  const signal = getReadSignal();
  mount.innerHTML = "";

  const stageStatuses = await loadStageStatuses(hostId);

  const header = el("header", { class: "ws-header" }, [
    el("div", { class: "ws-header-text" }, [
      el("p", { class: "ws-kicker", text: "Patching workspace" }),
      el("h1", { class: "ws-title", text: hostId }),
      el("p", { class: "ws-sub", text: "Select the target and patch, validate recovery, review the sealed plan, then apply and verify." }),
    ]),
  ]);
  mount.appendChild(header);
  const target = el("div", { class: "workspace-target" });
  target.appendChild(targetSummary(wizardContext(stageStatuses.steps, hostId)));
  mount.appendChild(target);

  const rail = el("nav", { class: "stage-rail", "aria-label": "Lifecycle" });
  const stageBadges = new Map();
  for (const s of STAGES) {
    const st = stageStatuses[s.id] || { kind: "neutral", text: "—" };
    const statusBadge = badge(st.text, st.kind);
    if (st.detail) statusBadge.title = st.detail;
    stageBadges.set(s.id, statusBadge);
    const link = el("a", {
      class: `stage-rail-item${s.id === stage ? " is-active" : ""}`,
      ...(s.id === stage ? { "aria-current": "step" } : {}),
      href: `#/hosts/${encodeURIComponent(hostId)}/${s.id}`,
    }, [
      el("span", { class: "stage-rail-label", text: s.label }),
      el("span", { class: "stage-rail-hint", text: s.hint }),
      statusBadge,
    ]);
    rail.appendChild(link);
  }
  mount.appendChild(rail);

  const stageMount = el("div", { class: "ws-stage" });
  mount.appendChild(stageMount);

  // A stage already read these saved observations. Update only its workspace
  // summary, leaving the stage's controls and drafts in their existing mount.
  const onEvidenceChanged = (steps) => {
    if (signal?.aborted || !mount.isConnected) return;
    updatePipelineStatuses(stageStatuses, steps);
    target.replaceChildren(targetSummary(wizardContext(steps, hostId)));
    for (const id of ["discover", "readiness"]) {
      const state = stageStatuses[id], node = stageBadges.get(id);
      node.textContent = state.text;
      node.className = badge("", state.kind).className;
    }
  };
  if (stage === "discover") await renderDiscoverStage(stageMount, hostId, { onEvidenceChanged });
  else if (stage === "readiness") await renderReadinessStage(stageMount, hostId, { onEvidenceChanged });
  else if (stage === "plan") await renderPlanStage(stageMount, hostId);
  else if (stage === "execute") await renderExecuteStage(stageMount, hostId);
  else if (stage === "recovery") await renderRecoveryStage(stageMount, hostId);
  else await renderDiscoverStage(stageMount, hostId, { onEvidenceChanged });
}

function updatePipelineStatuses(out, steps) {
  out.steps = steps;
  out.discover = { kind: "neutral", text: "idle" };
  out.readiness = { kind: "neutral", text: "idle" };
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
    if (!res.ok) throw new Error(data.message || "Could not load host evidence");
    updatePipelineStatuses(out, Array.isArray(data.steps) ? data.steps : []);
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
    const res = await apiFetch(`/api/recovery?host_id=${encodeURIComponent(hostId)}&view=saved`);
    const data = await res.json();
    const requests = newestFirst(data.requests || []).filter((request) => belongsToHost(request, hostId));
    if (requests.length) {
      const latest = requests[0];
      out.recovery = { kind: classifyStatus(latest.state), text: `Saved: ${latest.state || "unknown"}`,
        detail: latest.observed_at ? `Last observed ${latest.observed_at}; open Recovery to refresh.` : "Saved request state; observation time unknown. Open Recovery to refresh." };
    }
  } catch (error) {
    // Authentication and cancellation must reach the page boundary.
    throw error;
  }

  return out;
}
