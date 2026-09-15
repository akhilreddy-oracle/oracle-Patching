import { mountSession, refreshSession, refreshHostNav, syncSecondaryNav, clearHostCache } from "./shell.js";
import { TOKEN_EVENT } from "./api.js";
import { setSessionIdentity } from "./actor.js";
import { createPageRenderer } from "./navigation.js";
import { renderEstate } from "./estate.js";
import { renderWorkspace } from "./workspace.js";
import {
  renderPlanList,
  renderPlanNew,
  renderPlanDetail,
  renderPlanDemoNew,
  renderRollbackNew,
} from "./plans.js";
import { renderRecoveryList, renderRecoveryNew, renderRecoveryDetail } from "./recovery_pages.js";
import { renderApprovals } from "./approvals.js";
import { renderValidation } from "./validation.js";
import { renderAssistant } from "./assistant.js";

const STAGES = new Set(["discover", "readiness", "plan", "execute", "recovery"]);
const app = document.getElementById("app");

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const parts = hash.split("/").filter(Boolean);

  if (parts[0] === "hosts" && parts[1] && parts[2] === "pipeline") {
    return { name: "workspace", id: decodeURIComponent(parts[1]), stage: "readiness" };
  }
  if (parts[0] === "hosts" && parts[1]) {
    const stage = STAGES.has(parts[2]) ? parts[2] : "discover";
    return { name: "workspace", id: decodeURIComponent(parts[1]), stage };
  }
  if (parts[0] === "plans" && parts[1] === "new" && parts[2]) {
    return { name: "plan-new", hostId: decodeURIComponent(parts[2]) };
  }
  if (parts[0] === "plans" && parts[1] === "demo-new") return { name: "plan-demo-new" };
  if (parts[0] === "plans" && parts[1] === "rollback-new" && parts[2]) {
    return { name: "rollback-new", sourceId: decodeURIComponent(parts[2]) };
  }
  if (parts[0] === "plans" && parts[1]) {
    return { name: "plan-detail", id: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "plans") return { name: "plan-list" };
  if (parts[0] === "approvals") return { name: "approvals" };
  if (parts[0] === "validation") return { name: "validation" };
  if (parts[0] === "assistant") return { name: "assistant", id: parts[1] ? decodeURIComponent(parts[1]) : null };
  if (parts[0] === "recovery" && parts[1] === "new") return { name: "recovery-new" };
  if (parts[0] === "recovery" && parts[1]) {
    return { name: "recovery-detail", id: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "recovery") return { name: "recovery-list" };
  return { name: "estate" };
}

async function loadPage(view, signal) {
  const route = parseRoute();

  if (route.name === "workspace") {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    if (parts[2] === "pipeline" || !STAGES.has(parts[2])) {
      location.replace(`#/hosts/${encodeURIComponent(route.id)}/${route.stage}`);
      return;
    }
  }

  const activeHost = route.name === "workspace" ? route.id : null;
  await Promise.all([refreshSession(), refreshHostNav(activeHost)]);
  signal.throwIfAborted();
  syncSecondaryNav(route.name === "plan-list" || route.name === "plan-detail" ? "plans" : route.name === "recovery-list" || route.name === "recovery-detail" ? "recovery" : "");

  if (route.name === "assistant") {
    syncSecondaryNav("assistant");
    await renderAssistant(view, route.id);
  } else if (route.name === "validation") {
    syncSecondaryNav("validation");
    await renderValidation(view);
  } else if (route.name === "approvals") {
    syncSecondaryNav("approvals");
    await renderApprovals(view);
  } else if (route.name === "workspace") {
    await renderWorkspace(view, route.id, route.stage);
  } else if (route.name === "plan-new") {
    await renderPlanNew(view, route.hostId);
  } else if (route.name === "plan-demo-new") {
    await renderPlanDemoNew(view);
  } else if (route.name === "rollback-new") {
    await renderRollbackNew(view, route.sourceId);
  } else if (route.name === "plan-detail") {
    await renderPlanDetail(view, route.id);
  } else if (route.name === "plan-list") {
    await renderPlanList(view);
  } else if (route.name === "recovery-new") {
    await renderRecoveryNew(view);
  } else if (route.name === "recovery-detail") {
    await renderRecoveryDetail(view, route.id);
  } else if (route.name === "recovery-list") {
    await renderRecoveryList(view);
  } else {
    await renderEstate(view);
  }
}

const render = createPageRenderer(app, loadPage);
mountSession(document.getElementById("rail-session"));
window.addEventListener("hashchange", () => render());
window.addEventListener(TOKEN_EVENT, () => {
  clearHostCache();
  setSessionIdentity(null);
  render({ focus: false });
});
window.addEventListener("storage", (event) => {
  if (event.key === "opu-webapp-token") window.dispatchEvent(new Event(TOKEN_EVENT));
});
render();
