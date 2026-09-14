// Fixed procedure choices mirror the controller's supported apply adapters.
// The server validates this binding and the sealed Oracle README independently.
export const PROCEDURE_ADAPTERS = Object.freeze({
  database_single_instance_opatch: {
    label: "Standalone database · OPatch", family: "database", method: "opatch", topology: "single_instance",
    operations: ["database_shutdown", "database_opatch_apply", "database_startup", "database_datapatch"],
  },
  database_rolling_opatch: {
    label: "RAC database · rolling OPatch", family: "database", method: "opatch", topology: "rac",
    operations: ["database_stop_instance", "database_opatch_apply", "database_start_instance", "database_datapatch"],
  },
  grid_rolling_opatch: {
    label: "Grid Infrastructure · manual rolling OPatch", family: "grid", method: "opatch",
    operations: ["grid_rootcrs_prepatch", "grid_opatch_apply", "grid_rootadd_rdbms", "grid_rootcrs_postpatch"],
  },
  grid_rolling_opatchauto: {
    label: "Grid Infrastructure · rolling OPatchAuto", family: "grid", method: "opatchauto",
    operations: ["grid_opatchauto_apply"],
  },
  database_ojvm_opatch: {
    label: "Standalone OJVM · shutdown and upgrade mode", family: "database", method: "opatch", topology: "single_instance",
    operations: ["database_shutdown", "database_opatch_apply", "database_startup_upgrade", "database_datapatch_upgrade"],
  },
  database_out_of_place_switch: {
    label: "Standalone database · out-of-place home switch", family: "database", method: "switch_home", topology: "single_instance",
    operations: ["database_home_clone", "database_opatch_apply_clone", "database_home_switch", "database_datapatch"],
    rollback: "home_switch_back",
  },
});

export const REQUIRED_PRECHECKS = ["artifact_integrity", "platform_applicability", "opatch_version", "conflict_check", "backup_or_restore"];
export const REQUIRED_POSTCHECKS = ["binary_inventory", "service_health"];
const csv = (value) => String(value || "").split(",").map((part) => part.trim()).filter(Boolean);

/** Only restore saved inputs when every artifact and README binding still matches. */
export function procedureMatchesArtifact(procedure, artifact) {
  if (!procedure || !artifact?.sha256 || procedure.artifact_sha256 !== artifact.sha256) return false;
  if (!PROCEDURE_ADAPTERS[procedure.execution?.adapter]) return false;
  if (artifact.patch_ids?.length !== 1 || artifact.patch_ids[0] !== procedure.patch_id) return false;
  if (artifact.platforms?.length !== 1 || artifact.platforms[0].id !== procedure.target?.platform_id) return false;
  const references = Array.isArray(procedure.oracle_references)
    ? procedure.oracle_references.filter((entry) => entry?.kind === "patch_readme") : [];
  return references.length === 1 && Boolean(artifact.readme_files?.some((entry) =>
    entry.path === references[0].identifier && entry.sha256 && entry.sha256 === references[0].sha256
  ));
}

export function buildProcedure(adapter, fields, artifact, savedProcedure = null) {
  const config = PROCEDURE_ADAPTERS[adapter];
  if (!config) throw new Error("Choose a supported procedure adapter.");
  const required = ["patch_id", "platform_id", "required_opatch_version", "readme_identifier", "rollback_precondition"];
  if (config.family === "database") required.push("database_unique_name");
  for (const name of required) {
    if (!String(fields[name] || "").trim()) throw new Error(`${name.replaceAll("_", " ")} is required.`);
  }
  const reference = artifact?.readme_files?.find((entry) => entry.path === fields.readme_identifier);
  if (!reference?.sha256) throw new Error("README identifier must match a hashed file in the inspected artifact.");
  const procedure = {
    schema_version: "1.0", patch_id: fields.patch_id.trim(), artifact_sha256: artifact.sha256,
    target: {
      family: config.family, method: config.method, platform_id: fields.platform_id.trim(),
      ...(config.family === "database" ? { topology: config.topology, database_unique_name: fields.database_unique_name.trim() } : {}),
    },
    execution: { adapter, operations: [...config.operations] },
    required_opatch_version: fields.required_opatch_version.trim(),
    oracle_references: [{ kind: "patch_readme", identifier: reference.path, sha256: reference.sha256 }],
    mandatory_prechecks: [...new Set([...REQUIRED_PRECHECKS, ...csv(fields.prechecks)])],
    mandatory_postchecks: [...new Set([...REQUIRED_POSTCHECKS, ...csv(fields.postchecks)])],
    rollback: { mode: config.rollback || "opatch_rollback", precondition: fields.rollback_precondition.trim() },
  };
  if (savedProcedure?.execution?.adapter === adapter && procedureMatchesArtifact(savedProcedure, artifact)) {
    // The form does not edit supporting references or the recovery mode. Retain
    // them when editing this same workflow; a different adapter gets its own
    // canonical operations and rollback mode.
    procedure.oracle_references.push(...savedProcedure.oracle_references
      .filter((entry) => entry.kind === "mos_note" || entry.kind === "oracle_doc")
      .map((entry) => ({ ...entry })));
    if (savedProcedure.rollback?.mode) procedure.rollback.mode = savedProcedure.rollback.mode;
    if (config.family === "grid" && savedProcedure.target.topology) procedure.target.topology = savedProcedure.target.topology;
  }
  return procedure;
}
