import { el, badge, classifyStatus } from "../dom.js";
import { apiFetch } from "../api.js";
import { belongsToHost, newestFirst } from "../host_scope.js";
import { getActor } from "../actor.js";
import { runToCompletion } from "../runs.js";
import { maintenanceWindowError } from "../patch_wizard.js";
import {
  helperText, field, bindActorField, formErrorBox, showFormError, clearFormError,
  requireActor, requireToken, requireNonEmpty, isAbsolutePath, formatRunFailure,
} from "../ux.js";
import {
  backupPolicyChooser, policyRecoveryBlock, hydrateBackupPolicy, savedPolicyFromSteps, validateRecoveryPolicy,
} from "../backup_policy.js";

export async function renderRecoveryStage(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(el("div", { class: "stage-head" }, [
    el("h2", { class: "stage-title", text: "Recovery" }),
    el("p", { class: "stage-lead", text: "Prepare a backup, check its readability with RMAN, and bind verified recovery evidence to patch planning." }),
  ]));
  const [pipelineResponse, recoveryResponse] = await Promise.all([
    apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/pipeline`), apiFetch(`/api/recovery?host_id=${encodeURIComponent(hostId)}`),
  ]);
  const [pipeline, data] = await Promise.all([pipelineResponse.json(), recoveryResponse.json()]);
  if (!pipelineResponse.ok || !recoveryResponse.ok) throw new Error(pipeline.message || data.message || "Could not load recovery workflow");
  const steps = pipeline.steps || [];
  const savedPolicy = savedPolicyFromSteps(steps) || {
    schema_version: "1.0", maximum_snapshot_age_seconds: 1800, require_xml_inventory: true,
    database: { require_primary_read_write: true, maximum_invalid_objects: 0 },
  };
  hydrateBackupPolicy(hostId, savedPolicy);
  mount.appendChild(backupPolicyChooser(hostId));
  const snapshot = steps.find((step) => step.step === "discovery")?.evidence;
  const liveAvailable = data.live_available === true;
  mount.appendChild(liveRecoveryForm(hostId, snapshot, savedPolicy, data));
  if (!liveAvailable && data.live_reason) mount.appendChild(helperText(data.live_reason, "warn"));
  const selection = steps.find((step) => step.step === "readiness-evaluate")?.recovery_selection;
  const selectedSummary = el("section", { class: "panel recovery-selection" });
  const showSelection = (selected) => {
    selectedSummary.innerHTML = "";
    selectedSummary.appendChild(el("h3", { class: "panel-title", text: "Backup selected for patch readiness" }));
    selectedSummary.appendChild(helperText(selected?.request_id ? `Selected request: ${selected.request_id}. Readiness will verify freshness, target and policy again.` : "No validated backup is selected. Complete a live recovery request, then validate it for patch planning.", selected?.request_id ? null : "warn"));
    if (selected?.request_id) selectedSummary.appendChild(el("a", { class: "back-link", href: `#/hosts/${encodeURIComponent(hostId)}/readiness`, text: "Continue to readiness evaluation →" }));
  };
  showSelection(selection?.host_id === hostId ? selection : null);
  mount.appendChild(selectedSummary);
  const requests = newestFirst(data.requests || []).filter((request) => belongsToHost(request, hostId));
  const panel = el("section", { class: "panel" }, [el("h3", { class: "panel-title", text: "Recovery requests" })]);
  if (requests.length) {
    panel.appendChild(el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Request" }), el("th", { text: "Mode" }), el("th", { text: "State" }), el("th", { text: "Requester" }), el("th", { text: "Planning evidence" })])]),
      el("tbody", {}, requests.map((request) => {
        const action = el("td");
        if (request.mode === "live" && request.state === "completed") {
          const logBox = el("pre", { class: "run-log", style: "display:none" });
          const btn = el("button", { type: "button", text: "Validate for patch planning" });
          btn.disabled = !liveAvailable;
          if (!liveAvailable) btn.title = "Live recovery has not been enabled on this server";
          btn.addEventListener("click", async () => {
            if (!liveAvailable) return;
            btn.disabled = true;
            logBox.style.display = "block";
            logBox.classList.remove("run-log-error");
            logBox.textContent = "Validating the completed backup against current discovery…";
            try {
              const record = await runToCompletion(`/api/hosts/${encodeURIComponent(hostId)}/pipeline/recovery-collect`, { request_id: request.request_id }, {
                onTick: (run) => { logBox.textContent = `status: ${run.status}\n${(run.log_tail || []).join("\n")}`; },
              });
              const status = record.result?.status;
              if (record.status === "failed") {
                logBox.classList.add("run-log-error");
                logBox.textContent = formatRunFailure(record);
              } else {
                logBox.textContent = `Recovery validation: ${status || "completed"}. Re-evaluate readiness to check the complete patch policy.`;
                if (status === "passed") {
                  const selectedResponse = await apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/pipeline`);
                  const selectedData = await selectedResponse.json();
                  const selected = selectedData.steps?.find((step) => step.step === "readiness-evaluate")?.recovery_selection;
                  if (selectedResponse.ok && selected?.host_id === hostId && selected.request_id === request.request_id) showSelection(selected);
                  else logBox.textContent += " Selection is not confirmed by saved pipeline state; refresh this page before continuing.";
                }
                if (status && !["passed", "ready_for_approval", "ready_for_planning", "validated", "complete", "completed"].includes(status)) {
                  logBox.classList.add("run-log-error");
                  logBox.textContent += `\n${JSON.stringify(record.result, null, 2)}`;
                }
              }
            } catch (error) {
              if (error.name !== "AbortError") { logBox.classList.add("run-log-error"); logBox.textContent = error.message || String(error); }
            } finally { btn.disabled = false; }
          });
          action.appendChild(btn);
          action.appendChild(logBox);
        } else action.textContent = "—";
        return el("tr", {}, [
          el("td", {}, [el("a", { class: "back-link", href: `#/recovery/${encodeURIComponent(request.request_id)}`, text: request.request_id })]),
          el("td", { text: request.mode === "test_mode" ? "TEST_MODE fixture" : request.mode === "live" ? "Live host" : "Unknown" }),
          el("td", {}, [badge(request.state, classifyStatus(request.state))]),
          el("td", { text: request.requester || "—" }), action,
        ]);
      })),
    ]));
  } else panel.appendChild(helperText("No recovery preparations yet."));
  panel.appendChild(el("p", { class: "stage-next" }, [el("a", { href: "#/recovery", text: "Open All recovery →" })]));
  mount.appendChild(panel);
  mount.appendChild(el("details", { class: "panel lab-only" }, [
    el("summary", { text: "Lab only: TEST_MODE recovery demo" }),
    helperText("Fixture path — does not touch this host’s live estate."),
    el("p", { class: "stage-next" }, [el("a", { href: "#/recovery/new", text: "Open demo route →" })]),
  ]));
}

function liveRecoveryForm(hostId, snapshot, savedPolicy, capabilities) {
  const liveAvailable = capabilities.live_available === true;
  const panel = el("section", { class: "panel" }, [
    el("h3", { class: "panel-title", text: "Prepare backup on this host" }),
    helperText("Available adapter: standalone PRIMARY READ WRITE NOARCHIVELOG database using an SPFILE. Other database configurations are not supported by this preparation workflow."),
    helperText(`Adapter validation: ${capabilities.validation_level === "fixture_tested" ? "tested with fixtures; a full live workflow is not yet verified" : "not reported"}.`),
    helperText(capabilities.restore_validation || "RMAN RESTORE DATABASE VALIDATE checks backup readability. It does not restore a separate database."),
    helperText("This creates a live recovery request. Execution stops this database and its listener, makes and validates the backup, and restores services. Review analysis, approval and authorization before execution.", "warn"),
  ]);
  const requestId = el("input", { type: "text", value: `${hostId}-recovery-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  bindActorField(requester);
  const databases = [...new Set((snapshot?.databases || []).map((database) => database.db_unique_name).filter(Boolean))];
  const database = el("select", {}, [
    ...(databases.length === 1 ? [] : [el("option", { value: "", text: "Select a discovered database" })]),
    ...databases.map((name) => el("option", { value: name, text: name })),
  ]);
  const backupParent = el("input", { type: "text", placeholder: "/absolute/path/to/backup-directory" });
  const now = Date.now();
  const windowStart = el("input", { type: "text", value: new Date(now).toISOString().replace(/\.\d+Z$/, "Z") });
  const windowEnd = el("input", { type: "text", value: new Date(now + 4 * 3600000).toISOString().replace(/\.\d+Z$/, "Z") });
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const btn = el("button", { type: "button", text: "Create live recovery request" });
  const capabilityPanel = el("section", { class: "recovery-capability", "aria-live": "polite" });
  const targetCapability = () => {
    if (capabilities.supported_adapter !== "standalone_primary_noarchivelog_spfile" || capabilities.target_capability_context?.host_id !== hostId) return null;
    const matches = (Array.isArray(capabilities.target_capabilities) ? capabilities.target_capabilities : []).filter((target) => target.database === (database.value || null));
    return matches.length === 1 ? matches[0] : null;
  };
  const canCreate = () => {
    const target = targetCapability();
    return liveAvailable && databases.includes(database.value) && target?.can_create === true && target.status === "needs_native_analysis";
  };
  const renderCapability = () => {
    capabilityPanel.innerHTML = "";
    const target = targetCapability();
    capabilityPanel.appendChild(el("h4", { text: `Recovery requirements — ${database.value || "select a database"}` }));
    capabilityPanel.appendChild(badge(target?.status === "needs_native_analysis" ? "Native analysis required" : target?.status === "blocked" ? "Preparation blocked" : "Capability unknown", target?.status === "blocked" ? "bad" : "warn"));
    if (target && Array.isArray(target.requirements)) {
      capabilityPanel.appendChild(el("table", {}, [
        el("thead", {}, [el("tr", {}, ["Requirement", "Observed", "Required", "Next action"].map((title) => el("th", { text: title })))]),
        el("tbody", {}, target.requirements.map((requirement) => el("tr", {}, [
          el("th", { text: requirement.label }),
          el("td", {}, [badge(requirement.status, classifyStatus(requirement.status)), document.createTextNode(` ${requirement.observed ?? "Unknown"}`)]),
          el("td", { text: requirement.required }),
          el("td", { text: requirement.status === "passed" ? "Checked from saved discovery; checked again before execution" : requirement.next_action }),
        ]))),
      ]));
    }
    capabilityPanel.appendChild(helperText(target?.next_action || "Saved discovery or capability evidence is missing. Run Discover for this host, then return to Recovery.", "warn"));
    if (target?.can_create === true) capabilityPanel.appendChild(helperText("Creating a request does not stop services. Unknown SPFILE use, live identity, capacity and backup location must pass native analysis before approval."));
    capabilityPanel.appendChild(el("p", { class: "stage-next" }, [el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/discover`, text: "Open Discover →" })]));
    btn.disabled = !canCreate();
  };
  database.addEventListener("change", renderCapability);
  if (!liveAvailable) {
    btn.disabled = true;
    panel.appendChild(helperText("Live recovery has not been enabled on this server. The proposed request fields are shown for review.", "warn"));
    for (const input of [requestId, requester, database, backupParent, windowStart, windowEnd]) input.disabled = true;
  }
  if (!databases.length) {
    btn.disabled = true;
    panel.appendChild(helperText("Run Discover to identify the database first.", "warn"));
    panel.appendChild(el("p", { class: "stage-next" }, [el("a", {
      href: `#/hosts/${encodeURIComponent(hostId)}/discover`, text: "Open Discover →",
    })]));
  }
  btn.addEventListener("click", async () => {
    clearFormError(errBox);
    if (!liveAvailable) { showFormError(errBox, "Live recovery has not been enabled on this server."); return; }
    if (!canCreate()) { showFormError(errBox, "This database cannot create a recovery request from the current evidence. Resolve the recovery requirements shown above and refresh Discover."); return; }
    if (!requireToken(errBox)) return;
    const id = requireNonEmpty(requestId, errBox, "Request ID");
    if (!id) return;
    const actor = requireActor(requester, errBox, "Requester");
    if (!actor) return;
    if (!databases.includes(database.value)) { showFormError(errBox, "Select a database from this host’s discovery."); return; }
    const parent = requireNonEmpty(backupParent, errBox, "Backup parent directory");
    if (!parent) return;
    if (!isAbsolutePath(parent)) { showFormError(errBox, "Backup parent directory must be an absolute path on this host."); return; }
    const start = windowStart.value.trim(), end = windowEnd.value.trim();
    if (maintenanceWindowError(start, end)) {
      showFormError(errBox, "Use UTC timestamps such as 2026-09-14T15:00:00Z, with an end after the start and in the future."); return;
    }
    const recovery = policyRecoveryBlock(hostId);
    if (!recovery.require_backup) { showFormError(errBox, "Select “Require backup evidence” to create a recovery preparation."); return; }
    const policyError = validateRecoveryPolicy(recovery);
    if (policyError) { showFormError(errBox, policyError); return; }
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "Creating live request…";
    try {
      const record = await runToCompletion("/api/recovery", {
        request_id: id, requester: actor, host_id: hostId, database: database.value,
        backup_parent: parent, window_start: start, window_end: end,
        policy: { ...savedPolicy, recovery },
      }, { onTick: (run) => { logBox.textContent = `status: ${run.status}\n${(run.log_tail || []).join("\n")}`; } });
      if (record.status === "failed") { logBox.classList.add("run-log-error"); logBox.textContent = formatRunFailure(record); }
      else location.hash = `#/recovery/${encodeURIComponent(id)}`;
    } catch (error) {
      if (error.name !== "AbortError") { logBox.classList.add("run-log-error"); logBox.textContent = error.message || String(error); }
    } finally { btn.disabled = !canCreate(); }
  });
  panel.appendChild(el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Requester", requester, "Synced with the session identity"),
      field("Database", database, "From this host’s saved discovery"), field("Backup parent directory", backupParent, "Existing directory on this host; the tool creates a request subdirectory"),
      field("Window start (UTC)", windowStart), field("Window end (UTC)", windowEnd),
    ]),
    el("details", { class: "advanced-settings" }, [
      el("summary", { text: "Advanced settings — request identity and policy" }), field("Request ID", requestId),
      helperText(`Snapshot limit: ${savedPolicy.maximum_snapshot_age_seconds} seconds · XML inventory: ${savedPolicy.require_xml_inventory ? "required" : "not required"} · Primary read/write: ${savedPolicy.database.require_primary_read_write ? "required" : "not required"} · Invalid object limit: ${savedPolicy.database.maximum_invalid_objects}. Backup requirements come from the controls above.`),
    ]), capabilityPanel, errBox, btn, logBox,
  ]));
  renderCapability();
  return panel;
}
