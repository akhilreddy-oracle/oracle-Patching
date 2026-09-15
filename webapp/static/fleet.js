import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch } from "./api.js";

const KNOWN = {
  baseline_status: new Set(["compliant", "behind", "unknown"]),
  backup_status: new Set(["fresh", "stale", "missing", "unknown"]),
  readiness: new Set(["ready_for_approval", "blocked", "unknown"]),
  evidence_status: new Set(["fresh", "stale", "unknown"]),
};
const value = (data) => typeof data === "string" && data.trim() ? data : "unknown";

/** Do not promote an absent, stale or unfamiliar result to compliance. */
export function normalizeFleet(rows) {
  return (Array.isArray(rows) ? rows : []).filter((row) => row && typeof row.host_id === "string" && row.host_id).map((row) => {
    const item = { ...row };
    for (const key of ["environment", "oracle_version", "patch_baseline", "desired_patch_baseline", "database", "oracle_home"]) item[key] = value(row[key]);
    for (const [key, allowed] of Object.entries(KNOWN)) item[key] = allowed.has(row[key]) ? row[key] : "unknown";
    if (item.evidence_status !== "fresh") {
      if (item.readiness === "ready_for_approval") item.readiness = "unknown";
      if (item.baseline_status === "compliant") item.baseline_status = "unknown";
    }
    item.blockers = Number.isSafeInteger(row.blockers) && row.blockers >= 0 ? row.blockers : null;
    return item;
  });
}
export function fleetPriority(row) {
  if (row.readiness === "blocked" || row.backup_status === "missing" || row.baseline_status === "behind") return 0;
  if (row.evidence_status === "stale" || row.backup_status === "stale") return 1;
  if ([row.readiness, row.backup_status, row.baseline_status, row.evidence_status].includes("unknown")) return 2;
  return 3;
}
export function filterFleet(rows, filters = {}) {
  return normalizeFleet(rows).filter((row) => Object.entries(filters).every(([key, expected]) => !expected || expected === "all" || row[key] === expected))
    .sort((a, b) => fleetPriority(a) - fleetPriority(b) || a.host_id.localeCompare(b.host_id) || a.database.localeCompare(b.database));
}
const statusKind = (status) => ["fresh", "compliant", "ready_for_approval"].includes(status) ? "ok" : ["blocked", "behind", "missing"].includes(status) ? "bad" : ["unknown", "stale"].includes(status) ? "warn" : classifyStatus(status);

export function validateFleetMetadata(environment, baseline) {
  const env = String(environment ?? "").trim(), patch = String(baseline ?? "").trim();
  if (env && !/^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}$/.test(env)) return "Environment must start with a letter or number and use at most 64 letters, numbers, spaces, dots, underscores, slashes or hyphens.";
  if (patch && !/^[1-9][0-9]{0,19}$/.test(patch)) return "Desired baseline must be a positive patch ID of at most 20 digits, without leading zeros.";
  return null;
}

export function fleetNextActions(row) {
  const root = `#/hosts/${encodeURIComponent(row.host_id)}`;
  if (row.evidence_status !== "fresh") return [{ label: row.evidence_status === "stale" ? "Refresh expired evidence" : "Discover database", href: `${root}/discover` }];
  if (["missing", "stale"].includes(row.backup_status)) return [{ label: "Review backup", href: `${root}/recovery` }, { label: "Review readiness", href: `${root}/readiness` }];
  return [{ label: "Review readiness", href: `${root}/readiness` }];
}

function metadataEditor(row, canManage, reload) {
  const missing = Array.isArray(row.configuration_missing) ? row.configuration_missing : [];
  const labels = { environment: "environment", desired_patch_baseline: "desired patch baseline" };
  const block = el("div");
  if (missing.length) block.appendChild(el("p", { class: "helper-text helper-warn", text: `Not configured: ${missing.map((field) => labels[field] || field).join(", ")}.` }));
  if (!canManage || !row.metadata_version) {
    if (missing.length) block.appendChild(el("p", { class: "helper-text", text: "An administrator can configure these fleet settings." }));
    return block;
  }
  const environment = el("input", { type: "text", maxlength: "64", value: missing.includes("environment") ? "" : row.environment, "aria-label": `Environment for ${row.host_id}` });
  const baseline = el("input", { type: "text", inputmode: "numeric", maxlength: "20", value: missing.includes("desired_patch_baseline") ? "" : row.desired_patch_baseline, "aria-label": `Desired patch baseline for ${row.host_id}` });
  const save = el("button", { type: "submit", class: "btn btn-primary", text: "Save fleet settings" });
  const message = el("p", { class: "helper-text", role: "status", "aria-live": "polite" });
  const refresh = el("button", { type: "button", class: "btn btn-secondary", text: "Reload fleet" });
  refresh.hidden = true;
  refresh.addEventListener("click", reload);
  const form = el("form", { class: "fleet-metadata-form" }, [
    el("label", { class: "form-field" }, [el("span", { text: "Environment" }), environment]),
    el("label", { class: "form-field" }, [el("span", { text: "Desired patch baseline" }), baseline]),
    el("p", { class: "helper-text", text: "These settings apply to every database on this host. Enter the patch ID your team requires. Leave a field blank to clear it. Saving does not refresh evidence or approve patching." }),
    save, message, refresh,
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const invalid = validateFleetMetadata(environment.value, baseline.value);
    message.textContent = invalid || "Saving fleet settings…";
    if (invalid) return;
    save.disabled = true; refresh.hidden = true;
    try {
      await apiFetch(`/api/fleet/hosts/${encodeURIComponent(row.host_id)}/metadata`, { method: "POST", body: JSON.stringify({
        expected_version: row.metadata_version, environment: environment.value.trim() || null, desired_patch_baseline: baseline.value.trim() || null,
      }) });
      await reload(`Fleet settings saved for ${row.host_label || row.host_id}.`);
    } catch (error) {
      if (error.name === "AbortError") throw error;
      message.textContent = error.message || String(error);
      refresh.hidden = error.status !== 409;
    } finally { save.disabled = false; }
  });
  block.appendChild(el("details", {}, [el("summary", { text: "Edit fleet settings" }), form]));
  return block;
}

export async function renderFleet(mount, savedMessage = "") {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { class: "panel-title", text: "Fleet compliance" }));
  mount.appendChild(el("p", { class: "helper-text", text: "Saved evidence, ordered by required attention. Unknown values remain unknown. Refresh host evidence and readiness to update decisions." }));
  let data;
  try {
    const res = await apiFetch("/api/fleet"); data = await res.json();
    if (!res.ok) throw new Error(data.message || `Fleet evidence unavailable (${res.status})`);
    if (!Array.isArray(data.databases)) throw new Error("Fleet evidence did not include a database list.");
  } catch (error) {
    if (error.name === "AbortError") throw error;
    mount.appendChild(el("p", { class: "helper-text helper-warn", role: "status", text: `Fleet compliance unavailable: ${error.message || error}. Host discovery remains available below.` }));
    return;
  }
  const rows = normalizeFleet(data.databases);
  if (savedMessage) mount.appendChild(el("p", { class: "helper-text", role: "status", text: savedMessage }));
  if (data.metadata_error) mount.appendChild(el("p", { class: "helper-text helper-warn", role: "status", text: data.metadata_error }));
  const filters = {};
  const controls = el("div", { class: "form-grid fleet-filters" });
  const count = el("p", { class: "helper-text", role: "status", "aria-live": "polite" });
  const content = el("div", { class: "table-scroll fleet-table-scroll", role: "region", "aria-label": "Fleet databases", tabindex: "0" });
  const labels = { environment: "Environment", oracle_version: "Oracle version", patch_baseline: "Patch baseline", baseline_status: "Baseline compliance", backup_status: "Backup freshness", readiness: "Readiness", evidence_status: "Evidence freshness" };
  for (const [key, label] of Object.entries(labels)) {
    const select = el("select", { "aria-label": label }, [el("option", { value: "all", text: "All" }), ...[...new Set(rows.map((row) => row[key]))].sort().map((v) => el("option", { value: v, text: v }))]);
    select.addEventListener("change", () => { filters[key] = select.value; paint(); });
    controls.appendChild(el("label", { class: "form-field" }, [el("span", { text: label }), select]));
  }
  mount.appendChild(controls); mount.appendChild(count);
  mount.appendChild(el("p", { class: "helper-text fleet-scroll-hint", text: "Scroll the table to review all columns. The database column stays visible." }));
  mount.appendChild(content);
  if (data.generated_at) mount.appendChild(el("p", { class: "helper-text", text: `View generated: ${data.generated_at}. Each row retains its own observation time.` }));
  function paint() {
    const selected = filterFleet(rows, filters);
    count.textContent = `${selected.length} of ${rows.length} databases · ${selected.filter((row) => fleetPriority(row) < 3).length} need review`;
    content.innerHTML = "";
    if (!selected.length) { content.appendChild(el("p", { class: "empty-state", text: rows.length ? "No databases match these filters." : "No database evidence has been collected." })); return; }
    content.appendChild(el("table", { class: "fleet-table", role: "table", "aria-label": "Fleet compliance" }, [
      el("colgroup", {}, [20, 10, 8, 14, 10, 10, 12, 16].map((width) => el("col", { style: `width: ${width}%` }))),
      el("thead", {}, [el("tr", {}, ["Database / host", "Environment", "Oracle version", "Patch baseline", "Backup", "Readiness", "Evidence", "Next action"].map((text) => el("th", { text, scope: "col" })))]),
      el("tbody", {}, selected.map((row) => el("tr", {}, [
        el("td", { class: "fleet-identity", "data-label": "Database / host" }, [el("strong", { text: row.database }), el("p", { text: row.host_label || row.host_id }), el("small", { class: "mono", text: row.oracle_home })]),
        el("td", { "data-label": "Environment", text: row.environment }), el("td", { "data-label": "Oracle version", text: row.oracle_version }),
        el("td", { class: "fleet-patches", "data-label": "Patch baseline" }, [el("p", { text: row.patch_baseline }), badge(row.baseline_status, statusKind(row.baseline_status)), ...(row.desired_patch_baseline !== "unknown" ? [el("p", { class: "helper-text", text: `Required: ${row.desired_patch_baseline}` })] : [])]),
        el("td", { "data-label": "Backup" }, [badge(row.backup_status, statusKind(row.backup_status)), el("p", { class: "helper-text", text: row.backup_completed_at || "Completion time unknown" })]),
        el("td", { "data-label": "Readiness" }, [badge(row.readiness, statusKind(row.readiness)), ...(row.blockers != null ? [el("p", { class: "helper-text", text: `${row.blockers} blockers` })] : [])]),
        el("td", { class: "fleet-evidence", "data-label": "Evidence" }, [badge(row.evidence_status, statusKind(row.evidence_status)), el("p", { class: "helper-text", text: row.evidence_at || "Observation time unknown" })]),
        el("td", { class: "fleet-actions", "data-label": "Next action" }, [
          ...fleetNextActions(row).map((action) => el("p", {}, [el("a", { class: "back-link", href: action.href, text: action.label })])),
          metadataEditor(row, data.can_manage_metadata === true, (message = "") => renderFleet(mount, typeof message === "string" ? message : "")),
        ]),
      ]))),
    ]));
  }
  paint();
}
