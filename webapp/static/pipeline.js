import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch } from "./api.js";
import { runToCompletion, RunStartError } from "./runs.js";

const STEP_LABELS = {
  discovery: "1. Discovery (opu-topology-discover)",
  reconcile: "2. Topology reconciliation (opu-snapshot-reconcile)",
  "artifact-inspect": "3. Artifact inspection (opu-artifact-inspect)",
  "procedure-validate": "4. Procedure validation (opu-procedure-validate)",
  "compatibility-collect": "5. OPatch compatibility (opu-opatch-compatibility-collect)",
  "compatibility-reconcile": "6. Compatibility reconciliation (opu-compatibility-reconcile)",
  "readiness-evaluate": "7. Readiness evaluation (opu-readiness-evaluate)",
};

const ARTIFACT_DIR_KEY = "opu-webapp-artifact-dir";

export async function renderPipeline(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "host-header" }, [
      el("a", { class: "back-link", href: `#/hosts/${encodeURIComponent(hostId)}`, text: "← Host detail" }),
      el("h2", { text: `Readiness pipeline — ${hostId}` }),
    ])
  );

  const list = el("div", { class: "pipeline-list" });
  mount.appendChild(list);

  async function refresh() {
    const res = await apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/pipeline`);
    const data = await res.json();
    list.innerHTML = "";
    for (const stepState of data.steps) {
      list.appendChild(stepCard(hostId, stepState, refresh));
    }
  }

  await refresh();
}

function stepCard(hostId, stepState, refresh) {
  const { step, done, status, evidence } = stepState;
  const card = el("section", { class: "card pipeline-card" });

  const head = el("div", { class: "pipeline-card-head" }, [
    el("h3", { text: STEP_LABELS[step] || step }),
    badge(done ? status || "done" : "not run", done ? classifyStatus(status || "ok") : "neutral"),
  ]);
  card.appendChild(head);

  const controls = el("div", { class: "pipeline-controls" });
  card.appendChild(controls);

  const logBox = el("pre", { class: "run-log", style: "display:none" });
  card.appendChild(logBox);

  const resultBox = evidence
    ? el("details", { class: "pipeline-result" }, [
        el("summary", { text: "Evidence JSON" }),
        el("pre", { class: "mono", text: JSON.stringify(evidence, null, 2) }),
      ])
    : null;
  if (resultBox) card.appendChild(resultBox);

  buildControls(hostId, step, controls, logBox, refresh);

  if (step === "readiness-evaluate" && status === "ready_for_approval") {
    card.appendChild(
      el("p", { style: "margin-top:12px" }, [
        el("a", { href: `#/plans/new/${encodeURIComponent(hostId)}`, class: "back-link", text: "→ Create a plan from this readiness result" }),
      ])
    );
  }

  return card;
}

function runButton(label, onClick) {
  const btn = el("button", { type: "button", text: label });
  btn.addEventListener("click", onClick);
  return btn;
}

async function executeRun(logBox, btn, runFn) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.textContent = "starting…";
  try {
    const record = await runFn((rec) => {
      logBox.textContent = `status: ${rec.status}\n` + (rec.log_tail || []).join("\n");
    });
    if (record.status === "failed") {
      logBox.textContent = `FAILED: ${record.error?.message || "unknown error"}\n${record.error?.stderr || ""}`;
    } else {
      logBox.textContent = `succeeded`;
    }
  } catch (err) {
    if (err instanceof RunStartError) {
      logBox.textContent = `Could not start: ${err.message}`;
    } else {
      logBox.textContent = `Error: ${err}`;
    }
  } finally {
    btn.disabled = false;
  }
}

function buildControls(hostId, step, controls, logBox, refresh) {
  const base = `/api/hosts/${encodeURIComponent(hostId)}`;

  if (step === "discovery") {
    const btn = runButton("Run discovery", async () => {
      btn.disabled = true;
      logBox.style.display = "block";
      logBox.textContent = "discovering…";
      try {
        const res = await apiFetch(`${base}/discovery`);
        const data = await res.json();
        logBox.textContent = res.ok ? "succeeded" : `FAILED: ${data.message}\n${data.stderr || ""}`;
      } catch (err) {
        logBox.textContent = `Error: ${err}`;
      } finally {
        btn.disabled = false;
        await refresh();
      }
    });
    controls.appendChild(btn);
    return;
  }

  if (step === "reconcile" || step === "compatibility-reconcile") {
    const btn = runButton("Run", async () => {
      await executeRun(logBox, btn, (onTick) => runToCompletion(`${base}/pipeline/${step}`, {}, { onTick }));
      await refresh();
    });
    controls.appendChild(btn);
    return;
  }

  if (step === "artifact-inspect" || step === "compatibility-collect") {
    const input = el("input", {
      type: "text",
      placeholder: "/absolute/path/to/staged/patch on the target host",
      value: localStorage.getItem(ARTIFACT_DIR_KEY) || "",
    });
    const btn = runButton("Run", async () => {
      localStorage.setItem(ARTIFACT_DIR_KEY, input.value);
      await executeRun(logBox, btn, (onTick) =>
        runToCompletion(`${base}/pipeline/${step}`, { artifact_dir: input.value }, { onTick })
      );
      await refresh();
    });
    controls.appendChild(input);
    controls.appendChild(btn);
    return;
  }

  if (step === "procedure-validate") {
    controls.appendChild(procedureForm(base, logBox, refresh));
    return;
  }

  if (step === "readiness-evaluate") {
    controls.appendChild(policyForm(base, logBox, refresh));
    return;
  }
}

function field(labelText, inputEl) {
  return el("label", { class: "form-field" }, [el("span", { text: labelText }), inputEl]);
}

function procedureForm(base, logBox, refresh) {
  const form = el("div", { class: "pipeline-form" });

  const patchId = el("input", { type: "text", placeholder: "e.g. 39034528" });
  const family = el("select", {}, [el("option", { value: "database", text: "database" }), el("option", { value: "grid", text: "grid" })]);
  const topology = el("select", {}, [
    el("option", { value: "single_instance", text: "single_instance" }),
    el("option", { value: "rac", text: "rac" }),
  ]);
  const adapter = el("select", {}, [
    el("option", { value: "database_single_instance_opatch", text: "database_single_instance_opatch" }),
    el("option", { value: "database_rolling_opatch", text: "database_rolling_opatch" }),
    el("option", { value: "grid_rolling_opatch", text: "grid_rolling_opatch" }),
  ]);
  const dbName = el("input", { type: "text", placeholder: "database_unique_name" });
  const platformId = el("input", { type: "text", placeholder: "e.g. 226" });
  const operations = el("input", { type: "text", value: "database_stop_instance,database_opatch_apply,database_start_instance,database_datapatch" });
  const requiredOpatch = el("input", { type: "text", placeholder: "e.g. 12.2.0.1.49" });
  const readmeIdentifier = el("input", { type: "text", placeholder: "README relative path" });
  const mandatoryPre = el("input", { type: "text", value: "artifact_integrity,platform_applicability,opatch_version,conflict_check,backup_or_restore" });
  const mandatoryPost = el("input", { type: "text", value: "binary_inventory,service_health" });
  const rollbackPrecondition = el("textarea", { rows: "2", placeholder: "exact README rollback condition" });

  const autofillBtn = runButton("Autofill from artifact evidence", async () => {
    const res = await apiFetch(`${base}/pipeline`);
    const data = await res.json();
    const artifactStep = data.steps.find((s) => s.step === "artifact-inspect");
    const artifact = artifactStep?.evidence?.artifact;
    if (!artifact) {
      logBox.style.display = "block";
      logBox.textContent = "Run artifact-inspect first — nothing to autofill from.";
      return;
    }
    patchId.value = artifact.patch_ids?.[0] || "";
    platformId.value = artifact.platforms?.[0]?.id || "";
    if (artifact.readme_files?.[0]) readmeIdentifier.value = artifact.readme_files[0].path;
  });

  form.appendChild(el("div", { class: "form-grid" }, [
    field("Patch ID", patchId),
    field("Family", family),
    field("Topology", topology),
    field("Adapter", adapter),
    field("Database unique name", dbName),
    field("Platform ID", platformId),
    field("Required OPatch version", requiredOpatch),
    field("Operations (comma-separated)", operations),
    field("README identifier", readmeIdentifier),
    field("Mandatory prechecks", mandatoryPre),
    field("Mandatory postchecks", mandatoryPost),
  ]));
  form.appendChild(field("Rollback precondition", rollbackPrecondition));
  form.appendChild(autofillBtn);

  const submitBtn = runButton("Validate procedure", async () => {
    const res = await apiFetch(`${base}/pipeline`);
    const data = await res.json();
    const artifact = data.steps.find((s) => s.step === "artifact-inspect")?.evidence?.artifact;
    if (!artifact) {
      logBox.style.display = "block";
      logBox.textContent = "Run artifact-inspect first.";
      return;
    }
    const procedure = {
      schema_version: "1.0",
      patch_id: patchId.value,
      artifact_sha256: artifact.sha256,
      target: { family: family.value, method: "opatch", topology: topology.value, database_unique_name: dbName.value, platform_id: platformId.value },
      execution: { adapter: adapter.value, operations: operations.value.split(",").map((s) => s.trim()).filter(Boolean) },
      required_opatch_version: requiredOpatch.value,
      oracle_references: [{ kind: "patch_readme", identifier: readmeIdentifier.value, sha256: artifact.readme_files?.[0]?.sha256 || "" }],
      mandatory_prechecks: mandatoryPre.value.split(",").map((s) => s.trim()).filter(Boolean),
      mandatory_postchecks: mandatoryPost.value.split(",").map((s) => s.trim()).filter(Boolean),
      rollback: { mode: "opatch_rollback", precondition: rollbackPrecondition.value },
    };
    await executeRun(logBox, submitBtn, (onTick) =>
      runToCompletion(`${base}/pipeline/procedure-validate`, { procedure }, { onTick })
    );
    await refresh();
  });
  form.appendChild(submitBtn);

  return form;
}

function policyForm(base, logBox, refresh) {
  const form = el("div", { class: "pipeline-form" });

  const maxSnapshotAge = el("input", { type: "number", value: "1800" });
  const requireXmlInventory = el("input", { type: "checkbox", checked: "checked" });
  const requireBackup = el("input", { type: "checkbox", checked: "checked" });
  const maxBackupAge = el("input", { type: "number", value: "1440" });
  const minFraFreeBytes = el("input", { type: "number", value: "107374182400" });
  const requireRestorePoint = el("input", { type: "checkbox" });
  const requirePrimaryRW = el("input", { type: "checkbox", checked: "checked" });
  const maxInvalidObjects = el("input", { type: "number", value: "0" });

  form.appendChild(el("div", { class: "form-grid" }, [
    field("Maximum snapshot age (s)", maxSnapshotAge),
    field("Require XML inventory", requireXmlInventory),
    field("Require backup", requireBackup),
    field("Max backup age (min)", maxBackupAge),
    field("Minimum FRA free bytes", minFraFreeBytes),
    field("Require guaranteed restore point", requireRestorePoint),
    field("Require primary read/write", requirePrimaryRW),
    field("Maximum invalid objects", maxInvalidObjects),
  ]));

  const submitBtn = runButton("Evaluate readiness", async () => {
    const policy = {
      schema_version: "1.0",
      maximum_snapshot_age_seconds: Number(maxSnapshotAge.value),
      require_xml_inventory: requireXmlInventory.checked,
      recovery: {
        require_backup: requireBackup.checked,
        max_backup_age_minutes: Number(maxBackupAge.value),
        minimum_fra_free_bytes: Number(minFraFreeBytes.value),
        require_guaranteed_restore_point: requireRestorePoint.checked,
      },
      database: {
        require_primary_read_write: requirePrimaryRW.checked,
        maximum_invalid_objects: Number(maxInvalidObjects.value),
      },
    };
    await executeRun(logBox, submitBtn, (onTick) =>
      runToCompletion(`${base}/pipeline/readiness-evaluate`, { policy }, { onTick })
    );
    await refresh();
  });
  form.appendChild(submitBtn);

  return form;
}
