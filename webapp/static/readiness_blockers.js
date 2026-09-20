import { el, badge } from "./dom.js";

const unknown = "Unknown — evidence not supplied";
const array = (value) => Array.isArray(value) ? value : [];
const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const display = (value) => value == null || value === "" ? unknown : typeof value === "object" ? JSON.stringify(value) : String(value);
const hostName = (value) => String(value || "").toLowerCase().split(".")[0];
const containsName = (text, value) => Boolean(value) && new RegExp(`(?:^|[\\s:])${String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?=$|[\\s.,:])`).test(text);
const ACTIONS = {
  recovery_backup: ["Prepare backup", "recovery"], recovery_filesystem: ["Select validated backup", "recovery"],
  recovery_fra: ["Review backup capacity", "recovery"], recovery_restore_point: ["Review recovery requirements", "recovery"],
  patch_not_installed: ["Review installed patch and selection", "readiness", "procedure-validate"],
  artifact: ["Review patch media", "readiness", "artifact-inspect"], compatibility_contract: ["Review compatibility checks", "readiness", "compatibility-collect"],
  compatibility_home: ["Review compatibility evidence", "readiness", "compatibility-collect"],
};

/** Values retain their evidence source. Never attribute another node's cached metrics to a blocker. */
export function readinessBlockers(evidence, steps, hostId) {
  const snapshot = array(steps).find((s) => s.step === "discovery")?.evidence;
  const policy = array(steps).find((s) => s.step === "readiness-evaluate")?.input || evidence?.policy || {};
  return array(evidence?.gates).filter((gate) => ["blocker", "blocked", "fail", "failed"].includes(gate?.status)).map((gate) => {
    const detail = String(gate.detail || gate.message || "No detailed finding was supplied.");
    const node = gate.node || gate.host || (detail.match(/^Snapshot for ([^ ]+)/)?.[1]) || detail.split(" ")[0];
    const matchingNode = snapshot && hostName(node) === hostName(snapshot.host?.name);
    const databaseName = gate.database || gate.database_unique_name || detail.match(/\bdatabase ([^ :]+)/)?.[1];
    const database = matchingNode ? array(snapshot.databases).find((db) => databaseName === db.db_unique_name) : null;
    const home = matchingNode ? array(snapshot.oracle_homes).find((entry) => gate.oracle_home === entry.path || containsName(detail, entry.path) || database?.oracle_home === entry.path) : null;
    const runtime = database?.runtime || {};
    let actual = gate.actual ?? null, required = gate.required ?? null;
    if (actual == null) {
      switch (gate.name) {
        case "recovery_backup": { const age = number(runtime.backup_age_minutes ?? runtime.latest_backup_age_minutes); actual = age == null ? null : `${age} minutes old`; break; }
        case "recovery_fra": { const limit = number(runtime.fra_space_limit_bytes), used = number(runtime.fra_space_used_bytes); actual = limit != null && used != null && used <= limit ? `${limit - used} bytes free` : null; break; }
        case "recovery_restore_point": actual = number(runtime.guaranteed_restore_points); break;
        case "database_invalid_objects": actual = number(runtime.invalid_objects); break;
        case "database_open": actual = database ? [runtime.database_role, runtime.open_mode, runtime.instance_state].map(display).join(" / ") : null; break;
        case "database_runtime": actual = runtime.status; break;
        case "database_sql_state": actual = database ? `SQL patches not successful: ${display(number(runtime.sqlpatch_non_success))}; PDBs not read/write: ${display(number(runtime.pdb_not_read_write))}` : null; break;
        case "patch_not_installed": actual = home ? `Installed patch IDs: ${array(home.patches).join(", ") || unknown}` : null; break;
        case "snapshot_freshness": actual = matchingNode ? snapshot.collected_at : array(evidence.snapshot_evidence).find((s) => hostName(s.host) === hostName(node))?.collected_at; break;
        case "platform_inventory": actual = home?.platform?.id; break;
        case "authoritative_inventory": actual = home?.patch_inventory_source; break;
      }
    }
    if (required == null) {
      switch (gate.name) {
        case "recovery_backup": required = number(policy.recovery?.max_backup_age_minutes) == null ? null : `Backup age ≤ ${policy.recovery.max_backup_age_minutes} minutes`; break;
        case "recovery_fra": required = number(policy.recovery?.minimum_fra_free_bytes) == null ? null : `At least ${policy.recovery.minimum_fra_free_bytes} bytes free`; break;
        case "recovery_filesystem": required = "Selected recovery set validated for this database, home, owner and current policy"; break;
        case "recovery_restore_point": required = policy.recovery?.require_guaranteed_restore_point === true ? "At least one guaranteed restore point" : null; break;
        case "database_invalid_objects": required = number(policy.database?.maximum_invalid_objects) == null ? null : `Invalid objects ≤ ${policy.database.maximum_invalid_objects}`; break;
        case "database_open": required = policy.database?.require_primary_read_write === true ? "PRIMARY / READ WRITE / OPEN" : null; break;
        case "database_runtime": required = "Complete runtime evidence"; break;
        case "database_sql_state": required = "0 failed SQL patches and 0 PDBs not read/write"; break;
        case "patch_not_installed": required = evidence.patch_id ? `Patch ${evidence.patch_id} absent before apply` : null; break;
        case "snapshot_freshness": required = number(policy.maximum_snapshot_age_seconds) == null ? null : `Snapshot age ≤ ${policy.maximum_snapshot_age_seconds} seconds`; break;
        case "platform_inventory": required = evidence.target?.platform_id; break;
        case "authoritative_inventory": required = policy.require_xml_inventory === true ? "Hashed OPatch XML inventory" : null; break;
      }
    }
    const action = ACTIONS[gate.name] || (/snapshot|inventory|topology/.test(gate.name)
      ? ["Review discovery evidence", "discover"] : ["Review readiness controls", "readiness", "readiness-evaluate"]);
    const affectedDatabases = databaseName || (home ? array(snapshot.databases).filter((db) => db.oracle_home === home.path).map((db) => db.db_unique_name).filter(Boolean).join(", ") : null);
    return { name: gate.name || "Readiness finding", detail, database: display(affectedDatabases), home: display(gate.oracle_home || home?.path || database?.oracle_home), host: gate.node || gate.host || (matchingNode ? snapshot.host.name : hostId), actual: display(actual), required: display(required), observedAt: actual != null && matchingNode ? snapshot.collected_at : null, action: { label: action[0], href: `#/hosts/${encodeURIComponent(hostId)}/${action[1]}`, step: action[2] } };
  });
}

export function blockerCards(evidence, steps, hostId, { onReviewStep } = {}) {
  const cards = readinessBlockers(evidence, steps, hostId);
  if (!cards.length) return null;
  return el("div", { class: "readiness-findings" }, cards.map((finding) => {
    const action = el("a", { class: "back-link", href: finding.action.href, text: finding.action.label });
    if (finding.action.step && onReviewStep) action.addEventListener("click", (event) => {
      event.preventDefault();
      onReviewStep(finding.action.step);
    });
    return el("article", { class: "readiness-finding" }, [
    el("div", { class: "step-card-head" }, [el("h4", { text: finding.name.replaceAll("_", " ") }), badge("Needs action", "bad")]),
    el("p", { text: `Host: ${finding.host} · Database: ${finding.database}` }),
    el("p", { class: "mono", text: `Oracle home: ${finding.home}` }),
    el("dl", { class: "finding-values" }, [el("dt", { text: "Actual" }), el("dd", { text: finding.actual }), el("dt", { text: "Required" }), el("dd", { text: finding.required })]),
    ...(finding.observedAt ? [el("p", { class: "helper-text", text: `Actual values from cached discovery: ${finding.observedAt}. Refresh and evaluate to update this decision.` })] : []),
    el("p", { class: "helper-text", text: finding.detail }),
    action,
  ]);
  }));
}
