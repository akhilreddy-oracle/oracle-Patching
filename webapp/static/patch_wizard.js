import { el, badge } from "./dom.js";
import { procedureMatchesArtifact } from "./procedure_adapters.js";

export const WIZARD_STAGES = [
  { id: "discover", label: "Discover", hint: "1 · Select target" },
  { id: "readiness", label: "Readiness", hint: "2 · Patch and README" },
  { id: "recovery", label: "Recovery", hint: "3 · Prepare and validate backup" },
  { id: "plan", label: "Plan", hint: "4 · Window, review and approve" },
  { id: "execute", label: "Execute", hint: "5 · Apply and verify" },
];

export function wizardContext(steps, hostId) {
  const byId = Object.fromEntries((steps || []).map((s) => [s.step, s]));
  const discovery = byId.discovery?.evidence;
  const artifact = byId["artifact-inspect"]?.evidence?.artifact;
  const validated = byId["procedure-validate"];
  const procedure = validated?.evidence?.procedure;
  const bound = validated?.status === "ready_for_planning" && procedureMatchesArtifact(procedure, artifact);
  const databases = discovery?.databases || [];
  const selected = databases.find((db) => db.db_unique_name === procedure?.target?.database_unique_name);
  const database = selected || (databases.length === 1 ? databases[0] : null);
  const readiness = byId["readiness-evaluate"]?.evidence;
  return { hostId, database: database?.db_unique_name || "Select a discovered database", home: database?.oracle_home || "Unknown until target selection", patch: bound ? procedure.patch_id : artifact?.patch_ids?.join(", ") || "Select staged patch media", procedure, bound, readiness, recoverySelection: byId["readiness-evaluate"]?.recovery_selection, readme: bound ? procedure.oracle_references.find((ref) => ref.kind === "patch_readme") : null };
}

export function targetSummary(context) {
  return el("section", { class: "panel wizard-target", "aria-label": "Selected patch target" }, [
    el("div", { class: "step-card-head" }, [el("h2", { class: "panel-title", text: "Saved patch target" }), badge("LIVE · managed host over SSH", "warn")]),
    el("div", { class: "meta-row" }, [el("strong", { text: `Host: ${context.hostId}` }), el("strong", { text: `Database: ${context.database}` }), el("strong", { text: `Patch: ${context.patch}` })]),
    el("p", { class: "mono", text: `Oracle home: ${context.home}` }),
    el("p", { class: "helper-text", text: "This host workflow uses live operations. Fixture demos are separate routes. Target fields become authoritative when the plan is sealed." }),
  ]);
}

export function planReview(context) {
  const p = context.procedure;
  return el("section", { class: "panel wizard-review" }, [
    el("h3", { class: "panel-title", text: "Review patch requirements before sealing" }),
    badge(context.bound ? "README bound to validated procedure" : "README review required", context.bound ? "ok" : "warn"),
    el("p", { text: `README: ${context.readme?.identifier || "No currently validated README"}` }),
    el("p", { text: `Required OPatch: ${context.bound ? p.required_opatch_version : "Unknown until procedure validation"}` }),
    el("p", { text: `Rollback precondition: ${context.bound ? p.rollback?.precondition || "Not supplied" : "Review the selected README"}` }),
    el("p", { text: `Planned operations: ${context.bound ? (p.execution?.operations || []).join(" → ") : "Validate the procedure first"}` }),
    el("p", { class: "helper-text", text: "After creation, inspect the sealed task plan before a different approver grants approval. Server checks retain artifact, README, evidence, window and role bindings." }),
    el("a", { class: "back-link", href: `#/hosts/${encodeURIComponent(context.hostId)}/readiness`, text: "Review or change database, patch and README" }),
  ]);
}

export function maintenanceWindowError(start, end, now = Date.now()) {
  const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/;
  if (![start, end].every((value) => utc.test(value) && Number.isFinite(Date.parse(value))
      && new Date(Date.parse(value)).toISOString().slice(0, 19) === value.slice(0, 19))) return "Use valid UTC calendar timestamps, for example 2026-09-15T15:00:00Z.";
  if (Date.parse(end) <= Date.parse(start)) return "The maintenance window must end after it starts.";
  if (Date.parse(end) <= now) return "The maintenance window has expired. Choose a future end time.";
  return null;
}
