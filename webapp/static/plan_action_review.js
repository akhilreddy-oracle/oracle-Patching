import { el } from "./dom.js";
import { helperText } from "./ux.js";

const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const routeText = value => value === null || (typeof value === "string" && value.length <= 4096);

/** The displayed plan, tasks and routes come from one controller review. */
export function actionReviewBinding(review, planId) {
  const confirmation = review?.confirmation;
  if (!object(review?.plan) || review.plan.plan_id !== planId || !Array.isArray(review.tasks)
      || !review.tasks.every(object) || !object(confirmation) || confirmation.source !== "controller"
      || confirmation.plan_id !== planId || typeof confirmation.expected_action_binding_sha256 !== "string"
      || !/^[a-f0-9]{64}$/.test(confirmation.expected_action_binding_sha256)
      || !Array.isArray(confirmation.targets)) return null;
  const nodes = review.plan.nodes || [];
  if (!Array.isArray(nodes) || nodes.some(node => typeof node !== "string")) return null;
  const targets = confirmation.targets;
  if (targets.length !== new Set(nodes).size || targets.some(target => !object(target)
      || typeof target.node !== "string" || !nodes.includes(target.node)
      || typeof target.available !== "boolean" || typeof target.sudo !== "boolean"
      || ![target.host_id, target.ssh_alias, target.remote_root].every(routeText))
      || new Set(targets.map(target => target.node)).size !== targets.length) return null;
  return confirmation.expected_action_binding_sha256;
}

export function actionReviewPanel(review, planId) {
  const panel = el("section", { class: "panel execution-target-review", "aria-label": "Reviewed execution targets" });
  panel.appendChild(el("h3", { class: "panel-title", text: "Reviewed execution targets" }));
  if (!actionReviewBinding(review, planId)) {
    panel.appendChild(helperText("Execution review is unavailable or incomplete. Reload this plan to review its current tasks and targets before dispatching or executing.", "warn"));
    return panel;
  }
  const targets = review.confirmation.targets;
  if (!targets.length) panel.appendChild(helperText("No execution nodes are recorded in this plan. Native plan checks still determine whether an action is eligible.", "warn"));
  for (const target of targets) {
    panel.appendChild(el("p", { class: "helper-text", text: target.available
      ? `${target.node} → Host: ${target.host_id || "not recorded"}; SSH: ${target.ssh_alias || "not configured"}; Tools: ${target.remote_root || "not configured"}; sudo: ${target.sudo ? "yes" : "no"}`
      : `${target.node} → No configured SSH route. Fixture and pull-agent work remains subject to native execution checks.` }));
  }
  panel.appendChild(helperText("Dispatch and execution use the plan, tasks and configured routes reviewed here. If they change, reload and review the updated targets before trying again."));
  return panel;
}
