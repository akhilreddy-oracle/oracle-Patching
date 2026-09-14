import { el } from "./dom.js";
import { helperText } from "./ux.js";

const KEY_PREFIX = "opu-webapp-backup-policy:";

/** @returns {"require"|"waive"} */
export function getBackupPolicy(hostId) {
  const raw = localStorage.getItem(KEY_PREFIX + hostId);
  return raw === "waive" ? "waive" : "require";
}

export function setBackupPolicy(hostId, value) {
  localStorage.setItem(KEY_PREFIX + hostId, value === "waive" ? "waive" : "require");
}

/**
 * Customer-facing backup choice control.
 * @param {string} hostId
 * @param {(value: "require"|"waive") => void} [onChange]
 */
export function backupPolicyChooser(hostId, onChange) {
  const current = getBackupPolicy(hostId);
  const wrap = el("fieldset", { class: "backup-policy" });
  wrap.appendChild(el("legend", { text: "Backup requirement" }));
  wrap.appendChild(
    helperText(
      "Customer choice for this host. Waiving backup allows readiness and plan create without recovery evidence — accept the operational risk."
    )
  );

  const requireId = `backup-require-${hostId}`;
  const waiveId = `backup-waive-${hostId}`;
  const requireRadio = el("input", {
    type: "radio",
    name: `backup-policy-${hostId}`,
    id: requireId,
    value: "require",
  });
  const waiveRadio = el("input", {
    type: "radio",
    name: `backup-policy-${hostId}`,
    id: waiveId,
    value: "waive",
  });
  if (current === "waive") waiveRadio.checked = true;
  else requireRadio.checked = true;

  function commit() {
    const value = waiveRadio.checked ? "waive" : "require";
    setBackupPolicy(hostId, value);
    if (onChange) onChange(value);
  }
  requireRadio.addEventListener("change", commit);
  waiveRadio.addEventListener("change", commit);

  wrap.appendChild(
    el("label", { class: "backup-policy-option", for: requireId }, [
      requireRadio,
      el("span", {}, [
        el("strong", { text: "Require backup evidence" }),
        el("span", {
          class: "helper-text",
          text: "Recommended. Readiness and plans need valid backup / recovery evidence.",
        }),
      ]),
    ])
  );
  wrap.appendChild(
    el("label", { class: "backup-policy-option backup-policy-waive", for: waiveId }, [
      waiveRadio,
      el("span", {}, [
        el("strong", { text: "Allow patching without backup" }),
        el("span", {
          class: "helper-text helper-warn",
          text: "Customer waiver — skip backup gates. Policy seals require_backup=false; plan has no recovery-evidence.",
        }),
      ]),
    ])
  );
  return wrap;
}

export function policyRecoveryBlock(hostId) {
  if (getBackupPolicy(hostId) === "waive") {
    return {
      require_backup: false,
      max_backup_age_minutes: 0,
      minimum_fra_free_bytes: 0,
      require_guaranteed_restore_point: false,
    };
  }
  return {
    require_backup: true,
    max_backup_age_minutes: 1440,
    minimum_fra_free_bytes: 107374182400,
    require_guaranteed_restore_point: false,
  };
}

/**
 * Parse mandatory prechecks CSV for the procedure contract.
 * Always keep backup_or_restore — opu-procedure-validate requires it.
 * Backup waiver is expressed only via readiness policy.require_backup=false.
 */
export function filterPrechecks(_hostId, prechecksCsv) {
  const list = String(prechecksCsv || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  if (!list.includes("backup_or_restore")) list.push("backup_or_restore");
  return list;
}
