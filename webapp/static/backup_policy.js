import { el } from "./dom.js";
import { helperText, field } from "./ux.js";

const KEY_PREFIX = "opu-webapp-backup-policy:";
const RECOVERY_PREFIX = "opu-webapp-recovery-policy:";
const GIB = 1024 ** 3;
const DEFAULT_RECOVERY = {
  require_backup: true,
  max_backup_age_minutes: 1440,
  minimum_fra_free_bytes: 100 * GIB,
  require_guaranteed_restore_point: false,
  storage_mode: "fra",
  capacity_basis: "allocated",
  minimum_filesystem_free_bytes: 10 * GIB,
};

/** Saved server policy is authoritative when entering a host page. */
export function hydrateBackupPolicy(hostId, policy) {
  const recovery = policy?.recovery;
  const block = { ...DEFAULT_RECOVERY, ...(recovery && typeof recovery === "object" ? recovery : {}) };
  localStorage.setItem(RECOVERY_PREFIX + hostId, JSON.stringify(block));
  setBackupPolicy(hostId, block.require_backup === false ? "waive" : "require");
}

export function savedPolicyFromSteps(steps) {
  const step = steps.find((entry) => entry.step === "readiness-evaluate");
  const policy = step?.input;
  return policy && typeof policy === "object" && policy.recovery ? policy : null;
}

/** @returns {"require"|"waive"} */
export function getBackupPolicy(hostId) {
  return localStorage.getItem(KEY_PREFIX + hostId) === "waive" ? "waive" : "require";
}

export function setBackupPolicy(hostId, value) {
  localStorage.setItem(KEY_PREFIX + hostId, value === "waive" ? "waive" : "require");
}

export function policyRecoveryBlock(hostId) {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(RECOVERY_PREFIX + hostId) || "{}"); } catch { /* Safe defaults for an unreadable browser draft. */ }
  return { ...DEFAULT_RECOVERY, ...saved, require_backup: getBackupPolicy(hostId) !== "waive" };
}

/** Explicit draft controls; readiness or recovery creation seals the policy. */
export function backupPolicyChooser(hostId, onChange) {
  const current = policyRecoveryBlock(hostId);
  const wrap = el("fieldset", { class: "backup-policy" });
  wrap.appendChild(el("legend", { text: "Backup requirement" }));
  wrap.appendChild(helperText("Choose the recovery requirement for this host. Changes are sealed when you evaluate readiness or create a recovery request."));

  const requireId = `backup-require-${hostId}`;
  const waiveId = `backup-waive-${hostId}`;
  const requireRadio = el("input", { type: "radio", name: `backup-policy-${hostId}`, id: requireId, value: "require" });
  const waiveRadio = el("input", { type: "radio", name: `backup-policy-${hostId}`, id: waiveId, value: "waive" });
  requireRadio.checked = current.require_backup;
  waiveRadio.checked = !current.require_backup;
  const storage = el("select", { "aria-label": "Backup storage" }, [
    el("option", { value: "fra", text: "Fast Recovery Area (FRA)" }),
    el("option", { value: "filesystem", text: "Filesystem backup" }),
  ]);
  storage.value = current.storage_mode;
  const basis = el("select", { "aria-label": "Capacity estimate" }, [
    el("option", { value: "allocated", text: "Allocated datafile size" }),
    el("option", { value: "rman_unused_blocks", text: "RMAN unused-block estimate" }),
  ]);
  basis.value = current.capacity_basis;
  const reserve = el("input", { type: "number", min: "0", step: "1", value: String(current.minimum_filesystem_free_bytes / GIB), "aria-label": "Filesystem free space reserve (GiB)" });
  const filesystem = el("div", { class: "form-grid" }, [
    field("Capacity estimate", basis, "Allocated size is conservative. The RMAN estimate uses fresh database metadata and retains restore validation."),
    field("Filesystem free space reserve (GiB)", reserve, "Space that must remain after the backup capacity estimate."),
  ]);
  const note = helperText("");

  function sync() {
    waiveRadio.disabled = storage.value === "filesystem";
    storage.disabled = waiveRadio.checked;
    filesystem.hidden = storage.value !== "filesystem" || waiveRadio.checked;
    note.textContent = waiveRadio.checked
      ? "Backup is waived. This explicitly allows patching without recovery evidence."
      : storage.value === "filesystem"
        ? "Backup remains required. A completed, validated recovery request is needed; the filesystem capacity check replaces the FRA capacity check."
        : "Backup remains required, including the saved FRA capacity requirement.";
    note.classList.toggle("helper-warn", waiveRadio.checked);
  }
  function commit() {
    if (storage.value === "filesystem") {
      requireRadio.checked = true;
      waiveRadio.checked = false;
    }
    setBackupPolicy(hostId, waiveRadio.checked ? "waive" : "require");
    const block = policyRecoveryBlock(hostId);
    block.storage_mode = storage.value;
    block.capacity_basis = basis.value;
    const reserveBytes = Number(reserve.value) * GIB;
    // Keep invalid input invalid so the form cannot silently submit a previous value.
    block.minimum_filesystem_free_bytes = reserve.value.trim() && Number.isSafeInteger(reserveBytes) && reserveBytes >= 0 ? reserveBytes : null;
    localStorage.setItem(RECOVERY_PREFIX + hostId, JSON.stringify(block));
    sync();
    if (onChange) onChange(getBackupPolicy(hostId));
  }
  requireRadio.addEventListener("change", commit);
  waiveRadio.addEventListener("change", commit);
  storage.addEventListener("change", commit);
  basis.addEventListener("change", commit);
  reserve.addEventListener("input", commit);

  wrap.appendChild(el("label", { class: "backup-policy-option", for: requireId }, [requireRadio, el("strong", { text: "Require backup evidence" })]));
  wrap.appendChild(el("label", { class: "backup-policy-option backup-policy-waive", for: waiveId }, [waiveRadio, el("strong", { text: "Allow patching without backup" })]));
  wrap.appendChild(field("Backup storage", storage));
  wrap.appendChild(filesystem);
  wrap.appendChild(note);
  sync();
  return wrap;
}

export function validateRecoveryPolicy(block) {
  if (block.require_backup && block.storage_mode === "filesystem" && (!Number.isSafeInteger(block.minimum_filesystem_free_bytes) || block.minimum_filesystem_free_bytes < 0)) {
    return "Filesystem free space reserve must be a nonnegative amount in GiB.";
  }
  return null;
}
