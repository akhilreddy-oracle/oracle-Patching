import { el, badge, classifyStatus } from "../dom.js";
import { apiFetch } from "../api.js";
import { runToCompletion, RunStartError } from "../runs.js";
import {
  field,
  helperText,
  formErrorBox,
  showFormError,
  clearFormError,
  requireToken,
  requireNonEmpty,
  isAbsolutePath,
  explainStatus,
  formatRunFailure,
  summarizeBlockedEvidence,
  mediaRemediation,
} from "../ux.js";
import { backupPolicyChooser, policyRecoveryBlock, getBackupPolicy, hydrateBackupPolicy, savedPolicyFromSteps, validateRecoveryPolicy } from "../backup_policy.js";
import { PROCEDURE_ADAPTERS, REQUIRED_PRECHECKS, REQUIRED_POSTCHECKS, buildProcedure, procedureMatchesArtifact } from "../procedure_adapters.js";

import { blockerCards } from "../readiness_blockers.js";

const STEP_LABELS = {
  reconcile: "Topology reconciliation",
  "artifact-inspect": "Artifact inspection",
  "procedure-validate": "Procedure validation",
  "compatibility-collect": "OPatch compatibility",
  "compatibility-reconcile": "Compatibility reconciliation",
  "readiness-evaluate": "Readiness evaluation",
};

const STEP_PREREQS = {
  reconcile: ["discovery"],
  "artifact-inspect": ["discovery"],
  "procedure-validate": ["artifact-inspect"],
  "compatibility-collect": ["discovery", "artifact-inspect", "procedure-validate"],
  "compatibility-reconcile": ["reconcile", "procedure-validate", "compatibility-collect"],
  "readiness-evaluate": ["discovery", "reconcile", "artifact-inspect", "procedure-validate", "compatibility-reconcile"],
};

const READINESS_STEPS = [
  "reconcile",
  "artifact-inspect",
  "procedure-validate",
  "compatibility-collect",
  "compatibility-reconcile",
  "readiness-evaluate",
];

const ARTIFACT_DIR_KEY = "opu-webapp-artifact-dir";

function artifactDirKey(hostId) {
  return `${ARTIFACT_DIR_KEY}:${hostId}`;
}

function rememberedArtifactDir(hostId) {
  return (
    localStorage.getItem(artifactDirKey(hostId)) ||
    localStorage.getItem(ARTIFACT_DIR_KEY) ||
    ""
  );
}

function rememberArtifactDir(hostId, path) {
  localStorage.setItem(artifactDirKey(hostId), path);
  localStorage.setItem(ARTIFACT_DIR_KEY, path);
}

export async function renderReadinessStage(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "stage-head" }, [
      el("h2", { class: "stage-title", text: "Readiness" }),
      el("p", {
        class: "stage-lead",
        text: "Choose the discovered database and staged patch, review README requirements, then resolve readiness findings. Prepare and validate backup in Recovery before sealing a plan.",
      }),
    ])
  );

  const policyControls = el("div");
  mount.appendChild(policyControls);
  let policyLoaded = false;
  let selectedPolicyDraft = null;
  let selectedPolicyBinding = null;

  const list = el("div", { class: "step-list" });
  mount.appendChild(list);

  async function refresh() {
    list.innerHTML = "";
    let steps = [];
    let loadFailed = false;
    try {
      const res = await apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/pipeline`);
      const data = await res.json();
      if (!res.ok) {
        loadFailed = true;
        list.appendChild(
          el("div", { class: "panel panel-warn" }, [
            helperText(
              data.message ||
                `Failed to load pipeline (${res.status}). Paste the API token from webapp/var/api-token into the session panel, then reload.`,
              "error"
            ),
          ])
        );
      } else {
        steps = Array.isArray(data.steps) ? data.steps : [];
      }
    } catch (err) {
      loadFailed = true;
      list.appendChild(
        el("div", { class: "panel panel-warn" }, [
          helperText(`Could not load pipeline state: ${err}`, "error"),
        ])
      );
    }
    // Never paint fake "not run" cards when we do not have pipeline evidence —
    // that is exactly how a missing token looks like an empty inspect step.
    if (loadFailed) return;
    if (!policyLoaded) {
      hydrateBackupPolicy(hostId, savedPolicyFromSteps(steps));
      policyControls.appendChild(backupPolicyChooser(hostId));
      policyLoaded = true;
    }

    const recoverySelection = steps.find((s) => s.step === "readiness-evaluate")?.recovery_selection;
    const currentSelectionBinding = JSON.stringify([recoverySelection?.host_id, recoverySelection?.request_id, recoverySelection?.policy]);
    if (selectedPolicyDraft && currentSelectionBinding !== selectedPolicyBinding) {
      selectedPolicyDraft = null;
      selectedPolicyBinding = null;
      hydrateBackupPolicy(hostId, savedPolicyFromSteps(steps));
      policyControls.innerHTML = "";
      policyControls.appendChild(backupPolicyChooser(hostId));
    }
    if (recoverySelection?.host_id === hostId && recoverySelection.policy && typeof recoverySelection.policy === "object") {
      const usePolicy = el("button", { type: "button", text: "Use selected backup policy" });
      usePolicy.addEventListener("click", async () => {
        selectedPolicyDraft = recoverySelection.policy;
        selectedPolicyBinding = currentSelectionBinding;
        hydrateBackupPolicy(hostId, selectedPolicyDraft);
        policyControls.innerHTML = "";
        policyControls.appendChild(backupPolicyChooser(hostId));
        await refresh();
      });
      list.appendChild(el("section", { class: "panel" }, [
        el("h3", { class: "panel-title", text: "Selected backup policy" }),
        helperText(`Recovery request: ${recoverySelection.request_id}. ${selectedPolicyDraft ? "Its policy is loaded into the draft controls. Evaluate readiness to verify and save it." : "Review and explicitly load its policy for the readiness evaluation. Existing saved readiness remains unchanged until evaluation."}`),
        el("details", {}, [el("summary", { text: "Review preparation policy" }), el("pre", { class: "mono", text: JSON.stringify(recoverySelection.policy, null, 2) })]),
        usePolicy,
      ]));
    }
    const disc = steps.find((s) => s.step === "discovery");
    if (!disc?.done) {
      list.appendChild(
        el("div", { class: "panel panel-warn" }, [
          helperText("Discovery evidence missing.", "warn"),
          el("p", { class: "stage-next" }, [
            el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/discover`, text: "← Run Discover" }),
          ]),
        ])
      );
    }
    for (const stepId of READINESS_STEPS) {
      const stepState = steps.find((s) => s.step === stepId) || { step: stepId, done: false };
      list.appendChild(stepCard(hostId, stepState, steps, refresh, selectedPolicyDraft));
    }
  }

  await refresh();
}

/** Prefer API step.status; fall back to nested evidence (artifact.status). */
function effectiveStatus(stepState) {
  if (stepState?.status != null && stepState.status !== "") return stepState.status;
  const evidence = stepState?.evidence;
  if (!evidence || typeof evidence !== "object") return stepState?.status ?? null;
  if (stepState.step === "artifact-inspect") {
    const nested = evidence.artifact?.status;
    if (nested != null && nested !== "") return nested;
  }
  if (evidence.status != null && evidence.status !== "") return evidence.status;
  return stepState?.status ?? null;
}

function missingPrereqs(step, allSteps) {
  const need = STEP_PREREQS[step] || [];
  const byId = Object.fromEntries(allSteps.map((s) => [s.step, s]));
  return need.filter((id) => !byId[id]?.done);
}

function stepCard(hostId, stepState, allSteps, refresh, selectedPolicyDraft) {
  const { step, evidence } = stepState;
  // Evidence presence means the step ran — don't rely only on a top-level status
  // (artifact-inspect nests status under evidence.artifact.status).
  const done = Boolean(stepState.done || evidence);
  const status = effectiveStatus(stepState);
  const card = el("section", { class: "panel step-card" });
  const displayStatus = done ? status || "done" : "not run";
  const statusBadge = badge(displayStatus, done ? classifyStatus(displayStatus) : "neutral");

  card.appendChild(
    el("div", { class: "step-card-head" }, [
      el("h3", { text: STEP_LABELS[step] || step }),
      statusBadge,
    ])
  );

  const explained = explainStatus(status, { done });
  const statusExplanation = helperText(
    explained.text,
    explained.kind === "ok" ? null : explained.kind === "error" ? "error" : explained.kind === "warn" ? "warn" : null
  );
  card.appendChild(statusExplanation);

  if (step === "artifact-inspect" && done) {
    const path = evidence?.artifact?.path;
    if (path) {
      card.appendChild(helperText(`Cached artifact path: ${path}`, null));
    }
  }

  const missing = missingPrereqs(step, allSteps);
  if (missing.length) {
    card.appendChild(helperText(`Prereq: ${missing.join(", ")}`, "warn"));
  }

  const blocked = done && /blocked|incomplete|failed/i.test(String(status));
  if (blocked) {
    const findings = step === "readiness-evaluate" ? blockerCards(evidence, allSteps, hostId) : null;
    if (findings) card.appendChild(findings);
    const summary = summarizeBlockedEvidence(evidence);
    if (summary?.length && !findings) {
      card.appendChild(el("ul", { class: "blocked-summary" }, summary.map((line) => el("li", { text: line }))));
    }
    if (step === "readiness-evaluate" && staleSnapshotGate(evidence)) {
      const limit = evidence?.policy?.maximum_snapshot_age_seconds ?? evidence?.inputs?.policy?.maximum_snapshot_age_seconds;
      card.appendChild(
        helperText(
          `The topology snapshot was older than the policy allows${limit ? ` (${limit} s)` : ""} by the time readiness ran — ` +
            "running the steps one by one usually takes longer than that. Use “Refresh all evidence & evaluate” below to re-run " +
            "discovery through readiness in one go (typically 3–5 min), or raise Max snapshot age.",
          "warn"
        )
      );
    }
  }

  const controls = el("div", { class: "pipeline-controls" });
  card.appendChild(controls);
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  card.appendChild(logBox);

  if (blocked && (step === "artifact-inspect" || step === "compatibility-collect")) {
    const remedy = mediaRemediation(evidence);
    if (remedy) {
      const artifactDir =
        evidence?.artifact?.path || evidence?.checks?.[0]?.applicability_check?.patch_source || rememberedArtifactDir(hostId);
      card.appendChild(stageMediaPanel(hostId, remedy, artifactDir, refresh));
    }
  }
  if (evidence) {
    card.appendChild(
      el("details", { class: "pipeline-result" }, [
        el("summary", { text: "Evidence JSON" }),
        el("pre", { class: "mono", text: JSON.stringify(evidence, null, 2) }),
      ])
    );
  }

  buildControls(hostId, step, controls, logBox, refresh, allSteps, evidence, { statusBadge, statusExplanation, status, selectedPolicyDraft });

  if (step === "readiness-evaluate" && status === "ready_for_approval") {
    card.appendChild(
      el("p", { class: "stage-next" }, [
        el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/plan`, text: "Continue to Plan →" }),
      ])
    );
  }

  return card;
}

function runButton(label, onClick) {
  const btn = el("button", { type: "button", text: label });
  btn.addEventListener("click", async () => {
    try { await onClick(); }
    catch (error) {
      if (error.name === "AbortError") return;
      let box = btn.parentElement?.querySelector(".form-error");
      if (!box) { box = formErrorBox(); btn.parentElement?.appendChild(box); }
      showFormError(box, error.message || String(error));
    }
  });
  return btn;
}

async function executeRun(logBox, btn, runFn, { restoreDisabled } = {}) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.classList.remove("run-log-error");
  logBox.textContent = "starting…";
  try {
    const record = await runFn((rec) => {
      logBox.textContent = `status: ${rec.status}\n` + (rec.log_tail || []).join("\n");
    });
    if (record.status === "failed") {
      logBox.classList.add("run-log-error");
      logBox.textContent = formatRunFailure(record);
      return record;
    }
    logBox.textContent = "succeeded";
    return record;
  } catch (err) {
    logBox.classList.add("run-log-error");
    logBox.textContent = err instanceof RunStartError ? `Could not start: ${err.message}` : `Error: ${err}`;
    return null;
  } finally {
    btn.disabled = typeof restoreDisabled === "function" ? !!restoreDisabled() : false;
  }
}

/** Refresh only after success so a failed Run's error log is not destroyed. */
async function executeThenRefresh(logBox, btn, runFn, refresh, opts) {
  const record = await executeRun(logBox, btn, runFn, opts);
  if (record?.status === "succeeded") await refresh();
  return record;
}

function buildControls(hostId, step, controls, logBox, refresh, allSteps, evidence, presentation) {
  const base = `/api/hosts/${encodeURIComponent(hostId)}`;
  const errBox = formErrorBox();
  controls.appendChild(errBox);

  if (step === "reconcile" || step === "compatibility-reconcile") {
    const btn = runButton("Run", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const missing = missingPrereqs(step, allSteps);
      if (missing.length) {
        showFormError(errBox, `Complete: ${missing.join(", ")}`);
        return;
      }
      await executeThenRefresh(logBox, btn, (onTick) =>
        runToCompletion(`${base}/pipeline/${step}`, {}, { onTick }), refresh
      );
    });
    controls.appendChild(btn);
    return;
  }

  if (step === "artifact-inspect" || step === "compatibility-collect") {
    const cachedPath = evidence?.artifact?.path || rememberedArtifactDir(hostId);
    const input = el("input", {
      type: "text",
      placeholder: "/absolute/path/to/staged/patch",
      value: cachedPath,
    });
    const btn = runButton("Run", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const path = requireNonEmpty(input, errBox, "Artifact path");
      if (path == null) return;
      if (!isAbsolutePath(path)) {
        showFormError(errBox, "Path must be absolute (start with /).");
        return;
      }
      const missing = missingPrereqs(step, allSteps);
      if (missing.length) {
        showFormError(errBox, `Complete: ${missing.join(", ")}`);
        return;
      }
      rememberArtifactDir(hostId, path);
      const record = await executeThenRefresh(
        logBox,
        btn,
        (onTick) => runToCompletion(`${base}/pipeline/${step}`, { artifact_dir: path }, { onTick }),
        refresh,
        { restoreDisabled: () => !isAbsolutePath(input.value) }
      );
      if (record?.status === "failed") {
        showFormError(errBox, record.error?.message || "Artifact inspect failed — see log below.");
      } else if (!record) {
        showFormError(errBox, "Could not start artifact inspect — see log below.");
      }
    });
    function sync() {
      btn.disabled = !isAbsolutePath(input.value);
    }
    input.addEventListener("input", sync);
    sync();
    controls.appendChild(helperText("Absolute path on the target host (this host’s filesystem)."));
    controls.appendChild(input);
    controls.appendChild(btn);
    return;
  }

  if (step === "procedure-validate") {
    controls.appendChild(procedureForm(base, logBox, refresh, errBox, allSteps, presentation));
    return;
  }

  if (step === "readiness-evaluate") {
    controls.appendChild(policyForm(base, logBox, refresh, errBox, allSteps, presentation.selectedPolicyDraft));
  }
}

function staleSnapshotGate(evidence) {
  const gates = Array.isArray(evidence?.gates) ? evidence.gates : [];
  return gates.some((g) => g?.name === "snapshot_freshness" && /blocker|blocked|fail/i.test(String(g.status)));
}

function formatBytes(n) {
  if (n == null || Number.isNaN(Number(n))) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

/**
 * Remediation panel: stage complete patch media on this host (every RAC node)
 * straight from the browser — copy from a managed host that has it, or unpack
 * a zip already present on the host. Clears artifact-bound evidence on success.
 */
function stageMediaPanel(hostId, remedy, artifactDir, refresh) {
  const base = `/api/hosts/${encodeURIComponent(hostId)}`;
  const panel = el("section", { class: "panel panel-warn remediation" });
  panel.appendChild(el("h4", { text: `Fix from here: ${remedy.title}` }));
  panel.appendChild(helperText(remedy.text, "warn"));

  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const dirInput = el("input", { type: "text", value: artifactDir || "", placeholder: "/u01/stage/<patch_id>" });
  const ownerInput = el("input", { type: "text", placeholder: "oracle[:oinstall] (from discovery)" });
  const mode = el("select", {}, [
    el("option", { value: "host", text: "Copy from another managed host" }),
    el("option", { value: "zip", text: "Unpack a zip already on this host" }),
  ]);
  const sourceSel = el("select", {}, [el("option", { value: "", text: "Probing hosts…" })]);
  const zipInput = el("input", { type: "text", placeholder: "/u01/stage/p39034528_190000_Linux-x86-64.zip" });
  const replace = el("input", { type: "checkbox" });
  const status = helperText("", null);
  const probeBtn = runButton("Probe hosts", () => probe());
  const btn = runButton("Stage media", () => run());

  const hostRow = field("Source host", sourceSel, "Only hosts holding complete media at the same path are selectable.");
  const zipRow = field("Zip path on this host", zipInput, "Absolute path; every node of this host must have it.");
  function syncMode() {
    hostRow.style.display = mode.value === "host" ? "" : "none";
    zipRow.style.display = mode.value === "zip" ? "" : "none";
  }
  mode.addEventListener("change", syncMode);
  syncMode();

  async function probe() {
    clearFormError(errBox);
    const dir = dirInput.value.trim();
    if (!isAbsolutePath(dir)) {
      showFormError(errBox, "Artifact path must be absolute.");
      return;
    }
    sourceSel.innerHTML = "";
    sourceSel.appendChild(el("option", { value: "", text: "Probing hosts…" }));
    status.textContent = "";
    probeBtn.disabled = true;
    try {
      const res = await apiFetch(`${base}/artifact-sources?artifact_dir=${encodeURIComponent(dir)}`);
      const data = await res.json();
      if (!res.ok) {
        showFormError(errBox, data.message || "Probe failed");
        return;
      }
      if (data.owner && !ownerInput.value) ownerInput.value = data.owner;
      const targets = (data.targets || [])
        .map((t) => `${t.node}: ${t.state}${t.bytes != null ? ` (${formatBytes(t.bytes)})` : ""}`)
        .join("; ");
      status.textContent = `This host — ${targets || "no nodes probed"}.`;
      sourceSel.innerHTML = "";
      const complete = (data.sources || []).filter((s) => s.state === "complete");
      if (!complete.length) {
        sourceSel.appendChild(el("option", { value: "", text: "No managed host has complete media at this path" }));
        if (mode.value === "host") mode.value = "zip";
        syncMode();
      }
      for (const s of data.sources || []) {
        const opt = el("option", {
          value: s.host_id,
          text: `${s.label} — ${s.state}${s.bytes != null ? ` (${formatBytes(s.bytes)})` : ""}${s.error ? ` — ${s.error}` : ""}`,
        });
        if (s.state !== "complete") opt.disabled = true;
        sourceSel.appendChild(opt);
      }
      if (complete.length) sourceSel.value = complete[0].host_id;
    } catch (err) {
      showFormError(errBox, `Probe failed: ${err}`);
    } finally {
      probeBtn.disabled = false;
    }
  }

  async function run() {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    const dir = dirInput.value.trim();
    if (!isAbsolutePath(dir)) {
      showFormError(errBox, "Artifact path must be absolute.");
      return;
    }
    const body = { artifact_dir: dir, owner: ownerInput.value.trim() || undefined, replace: replace.checked };
    if (mode.value === "host") {
      if (!sourceSel.value) {
        showFormError(errBox, "Pick a source host with complete media (Probe hosts first).");
        return;
      }
      body.source = { host_id: sourceSel.value };
    } else {
      const zip = zipInput.value.trim();
      if (!isAbsolutePath(zip)) {
        showFormError(errBox, "Zip path must be absolute.");
        return;
      }
      body.source = { zip_path: zip };
    }
    rememberArtifactDir(hostId, dir);
    const record = await executeRun(logBox, btn, (onTick) =>
      runToCompletion(`${base}/pipeline/stage-artifact`, body, { onTick })
    );
    if (record?.status === "succeeded") {
      const nodes = (record.result?.nodes || []).map((n) => `${n.node}: ${n.status}${n.bytes ? ` (${formatBytes(n.bytes)})` : ""}`).join("; ");
      logBox.textContent = `staged — ${nodes}\n${record.result?.next || ""}`;
      await refresh();
    } else if (record?.status === "failed") {
      showFormError(errBox, record.error?.message || "Staging failed — see log below.");
    }
  }

  panel.appendChild(
    el("div", { class: "form-grid" }, [
      field("Artifact path on this host", dirInput),
      field("Owner (user[:group])", ownerInput, "Defaults to the Oracle Home owner from discovery."),
      field("Source", mode),
      hostRow,
      zipRow,
      field("Replace complete media", replace, "Only needed if this host already has a complete stage you want overwritten."),
    ])
  );
  panel.appendChild(status);
  panel.appendChild(errBox);
  panel.appendChild(el("div", { class: "pipeline-controls" }, [probeBtn, btn]));
  panel.appendChild(logBox);
  // Kick off the probe so the source list is ready when the operator looks.
  probe();
  return panel;
}

function procedureForm(base, logBox, refresh, errBox, allSteps, presentation) {
  const initialArtifact = allSteps.find((entry) => entry.step === "artifact-inspect")?.evidence?.artifact;
  const savedStep = allSteps.find((entry) => entry.step === "procedure-validate");
  const savedProcedure = savedStep?.evidence?.procedure;
  const restored = procedureMatchesArtifact(savedProcedure, initialArtifact);
  const savedInput = savedStep?.input;
  const restoredInput = !restored && procedureMatchesArtifact(savedInput, initialArtifact);
  const initialProcedure = restored ? savedProcedure : restoredInput ? savedInput : null;
  // A refresh must never silently attach a draft to newly inspected media.
  const artifactBinding = (artifact) => JSON.stringify([artifact?.sha256, artifact?.patch_ids, artifact?.platforms, artifact?.readme_files]);
  const initialBinding = artifactBinding(initialArtifact);
  let artifactChanged = false;
  let validationAttempted = false;
  let autofilledOpatch = null;
  const form = el("div", { class: "pipeline-form" });
  const patchId = el("select", {}, [el("option", { value: "", text: "Select an inspected patch" }), ...(initialArtifact?.patch_ids || []).map((id) => el("option", { value: id, text: id }))]);
  const family = el("input", { type: "text", readonly: "readonly" });
  const topology = el("input", { type: "text", readonly: "readonly" });
  const method = el("input", { type: "text", readonly: "readonly" });
  const adapter = el("select", {}, Object.entries(PROCEDURE_ADAPTERS).map(([value, config]) =>
    el("option", { value, text: config.label })
  ));
  const discoveredDatabases = [...new Set((allSteps.find((entry) => entry.step === "discovery")?.evidence?.databases || []).map((entry) => entry.db_unique_name).filter(Boolean))];
  const dbName = el("select", {}, [el("option", { value: "", text: "Select a discovered database" }), ...discoveredDatabases.map((name) => el("option", { value: name, text: name }))]);
  const platformId = el("input", { type: "text", placeholder: "e.g. 226" });
  const operations = el("input", {
    type: "text",
    readonly: "readonly",
  });
  const requiredOpatch = el("input", { type: "text", placeholder: "e.g. 12.2.0.1.49" });
  const readmeIdentifier = el("select", {}, [
    el("option", { value: "", text: "Choose an inspected README" }),
    ...(initialArtifact?.readme_files || []).map((entry) => el("option", { value: entry.path, text: entry.path })),
  ]);
  const mandatoryPre = el("input", {
    type: "text",
    value: REQUIRED_PRECHECKS.join(","),
  });
  const mandatoryPost = el("input", { type: "text", value: REQUIRED_POSTCHECKS.join(",") });
  const rollbackPrecondition = el("textarea", {
    rows: "2",
    placeholder: "README rollback condition",
    value: "",
  });

  const inputs = {
    patch_id: patchId, platform_id: platformId, database_unique_name: dbName,
    required_opatch_version: requiredOpatch, readme_identifier: readmeIdentifier,
    rollback_precondition: rollbackPrecondition, prechecks: mandatoryPre, postchecks: mandatoryPost,
  };
  const formValues = () => Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.value.trim()]));
  const draftIdentity = () => JSON.stringify([adapter.value, formValues()]);
  if (initialProcedure) {
    adapter.value = initialProcedure.execution.adapter;
    const values = {
      patch_id: initialProcedure.patch_id, platform_id: initialProcedure.target.platform_id,
      database_unique_name: initialProcedure.target.database_unique_name || "",
      required_opatch_version: initialProcedure.required_opatch_version || "",
      readme_identifier: initialProcedure.oracle_references.find((entry) => entry.kind === "patch_readme").identifier,
      rollback_precondition: initialProcedure.rollback?.precondition || "",
      prechecks: (initialProcedure.mandatory_prechecks || []).join(","),
      postchecks: (initialProcedure.mandatory_postchecks || []).join(","),
    };
    for (const [name, value] of Object.entries(values)) inputs[name].value = value;
  }
  const savedIdentity = restored ? draftIdentity() : null;
  function updateDraftStatus() {
    const unchanged = restored && draftIdentity() === savedIdentity && !artifactChanged && !validationAttempted;
    const status = unchanged ? presentation.status || "done" : "Draft · not validated";
    presentation.statusBadge.textContent = unchanged ? `Saved: ${status}` : status;
    presentation.statusBadge.className = badge("", unchanged ? classifyStatus(status) : "warn").className;
    presentation.statusExplanation.textContent = unchanged
      ? "Loaded the saved procedure for this inspected artifact. The validation result applies to these saved values."
      : artifactChanged || ((savedProcedure || savedInput) && !initialProcedure)
        ? "The saved procedure belongs to different artifact evidence. Review the current artifact and validate a new procedure."
        : restoredInput
          ? "Loaded the last submitted draft. It has no successful validation for these values; review it and validate again."
          : savedProcedure
            ? "These draft values have not been validated. The previous validation applies only to the saved procedure in Evidence JSON."
            : "Complete the draft and validate it before planning.";
    presentation.statusExplanation.className = unchanged ? "helper-text" : "helper-text helper-warn";
  }
  const draftTarget = el("p", { class: "wizard-draft-target", "aria-live": "polite" });
  const updateTarget = () => {
    const target = (allSteps.find((entry) => entry.step === "discovery")?.evidence?.databases || []).find((db) => db.db_unique_name === dbName.value);
    draftTarget.textContent = `Draft selection — Database: ${dbName.value || "not selected"} · Oracle home: ${target?.oracle_home || "unknown"} · Patch: ${patchId.value || "not selected"}`;
  };
  function onEdit() {
    updateDraftStatus();
    updateTarget();
  }
  for (const input of Object.values(inputs)) {
    input.addEventListener("input", onEdit);
    input.addEventListener("change", onEdit);
  }
  function requiredFieldsMissing() {
    const labels = {
      patch_id: "Patch ID", platform_id: "Platform ID", readme_identifier: "README identifier",
      required_opatch_version: "Required OPatch version from the selected README",
      rollback_precondition: "Rollback precondition from the README",
      ...(PROCEDURE_ADAPTERS[adapter.value].family === "database" ? { database_unique_name: "Database unique name" } : {}),
    };
    return Object.entries(labels).filter(([name]) => !inputs[name].value.trim()).map(([name, label]) => ({ input: inputs[name], label }));
  }
  function checkArtifact(artifact) {
    if (!artifact) throw new Error("Run artifact-inspect first, then reload Readiness.");
    if (artifactBinding(artifact) !== initialBinding) {
      artifactChanged = true;
      updateDraftStatus();
      throw new Error("Artifact evidence changed while this form was open. Reload Readiness to review the current artifact before validating.");
    }
  }

  const databaseField = field("Database unique name", dbName);
  function syncAdapter() {
    const config = PROCEDURE_ADAPTERS[adapter.value];
    family.value = config.family;
    topology.value = config.topology || "Grid cluster";
    method.value = config.method;
    operations.value = config.operations.join(",");
    databaseField.hidden = config.family !== "database";
    databaseField.style.display = config.family === "database" ? "" : "none";
    dbName.disabled = config.family !== "database";
    dbName.required = config.family === "database";
  }
  adapter.addEventListener("change", () => { syncAdapter(); onEdit(); });
  syncAdapter();
  updateDraftStatus();
  updateTarget();

  const autofillBtn = runButton("Autofill from artifact", async () => {
    clearFormError(errBox);
    readmeSource.textContent = "";
    autofillBtn.disabled = true;
    const notices = [];
    try {
      const res = await apiFetch(`${base}/pipeline`);
      const data = await res.json();
      const artifact = data.steps?.find((s) => s.step === "artifact-inspect")?.evidence?.artifact;
      checkArtifact(artifact);
      const discovery = data.steps?.find((s) => s.step === "discovery")?.evidence;
      function fillUnique(input, candidates, label) {
        if (input.value.trim()) return;
        const values = [...new Set(candidates.filter(Boolean).map(String))];
        if (values.length === 1) input.value = values[0];
        else if (values.length > 1) notices.push(`Choose ${label}: ${values.join(", ")}`);
      }
      fillUnique(patchId, artifact.patch_ids || [], "a patch ID");
      fillUnique(platformId, (artifact.platforms || []).map((entry) => entry.id), "a platform ID");
      fillUnique(readmeIdentifier, (artifact.readme_files || []).map((entry) => entry.path), "a README");
      if (PROCEDURE_ADAPTERS[adapter.value].family === "database") {
        fillUnique(dbName, (discovery?.databases || []).map((entry) => entry.db_unique_name), "a database");
      }
      const selectedReadme = readmeIdentifier.value;
      const reference = artifact.readme_files?.find((entry) => entry.path === selectedReadme);
      if (reference) {
        try {
          const hintRes = await apiFetch(`${base}/procedure-hints?readme_identifier=${encodeURIComponent(selectedReadme)}`);
          const hint = await hintRes.json();
          if (!hintRes.ok) throw new Error(hint.message || "Could not verify the selected README.");
          if (hint.artifact_sha256 !== artifact.sha256 || hint.readme_identifier !== reference.path || hint.readme_sha256 !== reference.sha256) {
            throw new Error("README hints do not match the inspected artifact. Reinspect the artifact before using them.");
          }
          // The operator can edit the form while SSH verifies the README.
          if (readmeIdentifier.value !== selectedReadme) {
            notices.push("README selection changed during verification; run Autofill again for the selected README.");
          } else {
            if (!requiredOpatch.value.trim() && hint.required_opatch_version) {
              requiredOpatch.value = hint.required_opatch_version;
              autofilledOpatch = { value: hint.required_opatch_version, readme: selectedReadme };
            }
            readmeSource.textContent = hint.required_opatch_version
              ? `Verified ${reference.path}: minimum OPatch ${hint.required_opatch_version}.${hint.evidence ? ` ${hint.evidence}` : ""}`
              : `Verified ${reference.path}; no unambiguous minimum OPatch version was found. Enter it from the README.`;
            notices.push(...(Array.isArray(hint.warnings) ? hint.warnings : []));
          }
        } catch (error) {
          if (error.name === "AbortError") throw error;
          notices.push(error.message || String(error));
        }
      }
      const missing = requiredFieldsMissing();
      if (missing.length) notices.push(`Still required: ${missing.map((entry) => entry.label).join("; ")}.`);
      if (notices.length) showFormError(errBox, notices.join(" "));
    } catch (error) {
      if (error.name === "AbortError") throw error;
      showFormError(errBox, error.message || String(error));
    } finally {
      autofillBtn.disabled = false;
      updateDraftStatus();
      updateTarget();
    }
  });

  const readmeSource = helperText("");
  readmeSource.setAttribute("aria-live", "polite");
  readmeIdentifier.addEventListener("change", () => {
    readmeSource.textContent = "";
    if (autofilledOpatch && readmeIdentifier.value !== autofilledOpatch.readme) {
      if (requiredOpatch.value === autofilledOpatch.value) requiredOpatch.value = "";
      autofilledOpatch = null;
      updateDraftStatus();
    }
  });
  requiredOpatch.addEventListener("input", () => { autofilledOpatch = null; });

  form.appendChild(
    helperText(
      "Select the adapter specified by the patch README. Autofill fills empty fields from unambiguous artifact and discovery evidence, and verifies the selected README for its minimum OPatch version. Existing values are preserved. Enter the exact rollback condition from the README. Required prechecks are always retained."
    )
  );
  form.appendChild(draftTarget);
  form.appendChild(readmeSource);
  form.appendChild(el("div", { class: "form-grid" }, [
    field("Patch ID", patchId), databaseField, field("Adapter", adapter),
    field("README identifier", readmeIdentifier), field("Required OPatch", requiredOpatch),
  ]));
  form.appendChild(el("details", { class: "advanced-settings" }, [
    el("summary", { text: "Advanced settings — procedure contract" }),
    el("div", { class: "form-grid" }, [
      field("Family", family), field("Topology", topology), field("Method", method),
      field("Platform ID", platformId), field("Operations", operations),
      field("Mandatory prechecks", mandatoryPre), field("Mandatory postchecks", mandatoryPost),
    ]),
  ]));
  form.appendChild(field("Rollback precondition", rollbackPrecondition));
  form.appendChild(autofillBtn);

  const submitBtn = runButton("Validate procedure", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    if (missingPrereqs("procedure-validate", allSteps).length) {
      showFormError(errBox, "Complete prerequisite steps.");
      return;
    }
    const missing = requiredFieldsMissing();
    if (missing.length) {
      showFormError(errBox, `Required: ${missing.map((entry) => entry.label).join("; ")}.`);
      missing[0].input.focus();
      return;
    }
    const res = await apiFetch(`${base}/pipeline`);
    const data = await res.json();
    const artifact = data.steps?.find((s) => s.step === "artifact-inspect")?.evidence?.artifact;
    let procedure;
    try {
      checkArtifact(artifact);
      procedure = buildProcedure(adapter.value, formValues(), artifact, initialProcedure);
    } catch (error) {
      showFormError(errBox, error.message);
      return;
    }
    validationAttempted = true;
    updateDraftStatus();
    const record = await executeThenRefresh(
      logBox,
      submitBtn,
      (onTick) => runToCompletion(`${base}/pipeline/procedure-validate`, { procedure }, { onTick }),
      refresh
    );
    if (record?.status === "failed") {
      showFormError(errBox, formatRunFailure(record) || record.error?.message || "Procedure validate failed — see log below.");
    } else if (!record) {
      showFormError(errBox, "Could not start procedure validate — see log below.");
    }
  });
  form.appendChild(submitBtn);
  return form;
}

function policyForm(base, logBox, refresh, errBox, allSteps, selectedPolicyDraft) {
  const form = el("div", { class: "pipeline-form" });
  const hostId = decodeURIComponent(base.split("/hosts/")[1]?.split("/")[0] || "");
  const savedPolicy = selectedPolicyDraft || savedPolicyFromSteps(allSteps) || {};
  const maxSnapshotAge = el("input", { type: "number", value: String(savedPolicy.maximum_snapshot_age_seconds ?? 1800) });
  const requireXmlInventory = el("input", { type: "checkbox", checked: "checked" });
  const requirePrimaryRW = el("input", { type: "checkbox", checked: "checked" });
  const maxInvalidObjects = el("input", { type: "number", value: String(savedPolicy.database?.maximum_invalid_objects ?? 0) });
  requireXmlInventory.checked = savedPolicy.require_xml_inventory ?? true;
  requirePrimaryRW.checked = savedPolicy.database?.require_primary_read_write ?? true;
  const waiverNote = el("p", {
    class: "helper-text",
    text:
      getBackupPolicy(hostId) === "waive"
        ? "Backup waived for this host — recovery gates will be skipped (require_backup=false)."
        : "Backup is required. Filesystem backups need a completed recovery request validated for this host’s current evidence.",
  });
  if (getBackupPolicy(hostId) === "waive") waiverNote.classList.add("helper-warn");

  form.appendChild(waiverNote);
  form.appendChild(el("p", { class: "stage-next" }, [
    el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/recovery`, text: "Prepare or validate recovery evidence →" }),
  ]));
  form.appendChild(el("details", { class: "advanced-settings" }, [
    el("summary", { text: "Advanced settings — readiness policy" }),
    el("div", { class: "form-grid" }, [
      field("Max snapshot age (s)", maxSnapshotAge),
      field("Require XML inventory", requireXmlInventory),
      field("Require primary R/W", requirePrimaryRW),
      field("Max invalid objects", maxInvalidObjects),
    ]),
  ]));

  const buildPolicy = () => ({
    ...savedPolicy,
    schema_version: "1.0",
    maximum_snapshot_age_seconds: Number(maxSnapshotAge.value),
    require_xml_inventory: requireXmlInventory.checked,
    recovery: policyRecoveryBlock(hostId),
    database: {
      ...savedPolicy.database,
      require_primary_read_write: requirePrimaryRW.checked,
      maximum_invalid_objects: Number(maxInvalidObjects.value),
    },
  });

  // Age of the cached topology snapshot; readiness will reject anything older
  // than the policy limit, so we can know the outcome before asking the tool.
  const byId = Object.fromEntries(allSteps.map((s) => [s.step, s]));
  const snapshotAgeSeconds = () => {
    const at = byId.discovery?.evidence?.collected_at;
    const t = at ? Date.parse(at) : NaN;
    return Number.isFinite(t) ? Math.max(0, (Date.now() - t) / 1000) : null;
  };
  const chainReady = Boolean(byId["artifact-inspect"]?.done && byId["procedure-validate"]?.done);

  const submitBtn = runButton("Evaluate readiness", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    if (missingPrereqs("readiness-evaluate", allSteps).length) {
      showFormError(errBox, "Complete prerequisite steps.");
      return;
    }
    const policy = buildPolicy();
    const policyError = validateRecoveryPolicy(policy.recovery);
    if (policyError) { showFormError(errBox, policyError); return; }
    const age = snapshotAgeSeconds();
    if (age != null && age > policy.maximum_snapshot_age_seconds) {
      const mins = Math.round(age / 60);
      if (chainReady) {
        // Evaluating now can only produce a snapshot_freshness blocker; do the useful thing instead.
        logBox.style.display = "block";
        logBox.classList.remove("run-log-error");
        logBox.textContent = `snapshot is ${mins} min old (limit ${Math.round(policy.maximum_snapshot_age_seconds / 60)} min) — refreshing all evidence first…`;
        await runChain(policy);
      } else {
        showFormError(errBox, `Snapshot is ${mins} min old, above the ${Math.round(policy.maximum_snapshot_age_seconds / 60)} min limit — re-run Discover first, or raise Max snapshot age.`);
      }
      return;
    }
    const record = await executeThenRefresh(
      logBox,
      submitBtn,
      (onTick) => runToCompletion(`${base}/pipeline/readiness-evaluate`, { policy }, { onTick }),
      refresh
    );
    if (record?.status === "failed") {
      showFormError(errBox, record.error?.message || "Readiness evaluate failed — see log below.");
    } else if (!record) {
      showFormError(errBox, "Could not start readiness evaluate — see log below.");
    }
  });

  // Re-derives discovery → readiness in one sitting so snapshot_freshness is
  // measured against a snapshot taken minutes, not hours, earlier.
  async function runChain(policy) {
    submitBtn.disabled = true;
    const record = await executeRun(logBox, chainBtn, (onTick) =>
      runToCompletion(`${base}/pipeline/readiness-chain`, { policy }, { onTick })
    );
    submitBtn.disabled = false;
    if (record?.status === "succeeded") {
      const r = record.result || {};
      const lines = (r.steps || []).map((s) => `${s.ok ? "ok " : "STOP"} ${s.step}: ${s.status ?? "done"} (${s.seconds}s)`);
      logBox.textContent =
        `${r.status === "ready_for_approval" ? "ready_for_approval" : `blocked at ${r.stopped_at}`} after ${r.elapsed_seconds}s\n` +
        lines.join("\n") +
        (r.findings?.length ? `\n\nfindings:\n- ${r.findings.map((f) => (typeof f === "string" ? f : f.message || JSON.stringify(f))).join("\n- ")}` : "");
      if (r.status !== "ready_for_approval") logBox.classList.add("run-log-error");
      await refresh();
    } else if (record?.status === "failed") {
      showFormError(errBox, record.error?.message || "Chain failed — see log below.");
    } else if (!record) {
      showFormError(errBox, "Could not start the chain — see log below.");
    }
  }
  const chainBtn = runButton("Refresh all evidence & evaluate", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    if (!chainReady) {
      showFormError(errBox, "Run Artifact inspection and Procedure validation once first — the chain reuses their saved inputs.");
      return;
    }
    const policy = buildPolicy();
    const policyError = validateRecoveryPolicy(policy.recovery);
    if (policyError) { showFormError(errBox, policyError); return; }
    await runChain(policy);
  });
  chainBtn.title = chainReady
    ? "Runs discovery, reconcile, artifact inspect, procedure validate, compatibility, reconciliation and readiness back to back."
    : "Run Artifact inspection and Procedure validation once first.";
  form.appendChild(el("div", { class: "pipeline-controls" }, [submitBtn, chainBtn]));
  return form;
}
