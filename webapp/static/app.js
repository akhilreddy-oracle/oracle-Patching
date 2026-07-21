import { renderEstate } from "./estate.js";
import { renderHost } from "./host.js";
import { renderPipeline } from "./pipeline.js";
import { renderPlanList, renderPlanNew, renderPlanDetail, renderPlanDemoNew } from "./plan.js";
import { mountActorWidget } from "./actor.js";

const app = document.getElementById("app");

function parseRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] === "hosts" && parts[1] && parts[2] === "pipeline") {
    return { name: "pipeline", id: decodeURIComponent(parts[1]) };
  }
  if (parts[0] === "hosts" && parts[1]) return { name: "host", id: decodeURIComponent(parts[1]) };
  if (parts[0] === "plans" && parts[1] === "new" && parts[2]) return { name: "plan-new", hostId: decodeURIComponent(parts[2]) };
  if (parts[0] === "plans" && parts[1] === "demo-new") return { name: "plan-demo-new" };
  if (parts[0] === "plans" && parts[1]) return { name: "plan-detail", id: decodeURIComponent(parts[1]) };
  if (parts[0] === "plans") return { name: "plan-list" };
  return { name: "estate" };
}

async function render() {
  const route = parseRoute();
  if (route.name === "pipeline") {
    await renderPipeline(app, route.id);
  } else if (route.name === "host") {
    await renderHost(app, route.id);
  } else if (route.name === "plan-new") {
    await renderPlanNew(app, route.hostId);
  } else if (route.name === "plan-demo-new") {
    await renderPlanDemoNew(app);
  } else if (route.name === "plan-detail") {
    await renderPlanDetail(app, route.id);
  } else if (route.name === "plan-list") {
    await renderPlanList(app);
  } else {
    await renderEstate(app);
  }
}

mountActorWidget(document.getElementById("topbar-actor"));
window.addEventListener("hashchange", render);
render();
