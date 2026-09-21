import { el, badge, classifyStatus, renderErrorBox } from "./dom.js";
import { apiFetch } from "./api.js";
import { newestFirst } from "./host_scope.js";
import { runToCompletion, RunStartError } from "./runs.js";
import { getActor } from "./actor.js";
import { executionFailure, reconciliationCard } from "./run_reconciliation.js";
import {
  field,
  bindActorField,
  formErrorBox,
  showFormError,
  clearFormError,
  requireActor,
  requireToken,
  requireNonEmpty,
  helperText,
  nextStepBanner,
  pageIntro,
  formatRunFailure,
  failureCard,
} from "./ux.js";

export async function renderRecoveryList(mount) {
  mount.innerHTML = "";
  mount.appendChild(
    el("header", { class: "ws-header" }, [
      el("div", { class: "ws-header-text" }, [
        el("p", { class: "ws-kicker", text: "Indexes" }),
        el("h1", { class: "ws-title", text: "All recovery" }),
        el("p", { class: "ws-sub", text: "Recovery preparations bindable as --recovery-evidence on sealed plans." }),
      ]),
    ])
  );

  const res = await apiFetch("/api/recovery");
  const data = await res.json();
  if (!res.ok) { renderErrorBox(mount, data); return; }
  mount.appendChild(el("section", { class: "card" }, [
    el("h2", { text: "Live recovery support" }),
    helperText("Backup preparation currently supports a standalone PRIMARY READ WRITE NOARCHIVELOG database using an SPFILE. Check the database’s Recovery stage for observed requirements before creating a request."),
    helperText(data.restore_validation || "RMAN RESTORE DATABASE VALIDATE checks backup readability. It does not restore a separate database."),
    helperText(data.validation_level === "fixture_tested" ? "Adapter tested with fixtures. A complete live workflow is not yet verified." : "Adapter validation has not been reported.", "warn"),
    ...(data.live_available === false ? [helperText(data.live_reason || "Live recovery is unavailable on this server.", "warn")] : []),
  ]));
  if (!data.requests?.length) {
    mount.appendChild(
      el("p", {
        class: "empty-state",
        text: "No recovery preparations yet. Open a host’s Recovery stage to prepare a live backup. The demo route below uses fixtures only.",
      })
    );
  } else {
    const table = el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Request ID" }), el("th", { text: "Mode" }), el("th", { text: "State" }), el("th", { text: "Requester" })])]),
      el(
        "tbody",
        {},
        newestFirst(data.requests).map((r) =>
          el("tr", {}, [
            el("td", {}, [el("a", { href: `#/recovery/${encodeURIComponent(r.request_id)}`, text: r.request_id })]),
            el("td", { text: r.mode === "test_mode" ? "TEST_MODE fixture" : r.mode === "live" ? "Live host" : "Unknown" }),
            el("td", {}, [badge(r.state, classifyStatus(r.state))]),
            el("td", { text: r.requester || "—" }),
          ])
        )
      ),
    ]);
    mount.appendChild(el("section", { class: "card" }, [table]));
  }

  mount.appendChild(
    el("details", { class: "card", style: "margin-top:16px" }, [
      el("summary", { text: "Lab only: TEST_MODE recovery demo" }),
      el("p", { class: "helper-text" }, [
        document.createTextNode(
          "Optional fixture path. Builds a fake standalone Oracle home and runs create → analyze → approve → authorize → execute through opu-database-recovery-prepare."
        ),
      ]),
      el("p", { class: "empty-cta-row" }, [
        el("a", { class: "empty-cta", href: "#/recovery/new", text: "Open TEST_MODE recovery demo route →" }),
      ]),
    ])
  );
}

export async function renderRecoveryNew(mount) {
  mount.innerHTML = "";
  mount.appendChild(
    pageIntro(
      "Lab demo",
      "TEST_MODE recovery",
      "Requester syncs with Acting as in Session. Runs create → analyze → approve → authorize → execute through the real recovery binary."
    )
  );

  const requestId = el("input", { type: "text", value: `recovery-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  bindActorField(requester);
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });

  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Request ID", requestId),
      field("Requester (actor)", requester, "Synced with Acting as — required before create"),
    ]),
  ]);

  const btn = el("button", { type: "button", text: "Build fixture and create request" });
  btn.addEventListener("click", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    const submittedId = requireNonEmpty(requestId, errBox, "Request ID");
    if (!submittedId) return;
    const actor = requireActor(requester, errBox, "Requester");
    if (!actor) return;
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "building fixture…";
    try {
      const record = await runToCompletion("/api/recovery/testmode-demo", {
        request_id: submittedId,
        requester: actor,
      });
      if (record.status === "failed") {
        logBox.classList.add("run-log-error");
        logBox.textContent = formatRunFailure(record);
      } else {
        location.hash = `#/recovery/${encodeURIComponent(submittedId)}`;
      }
    } catch (err) {
      logBox.classList.add("run-log-error");
      logBox.textContent = err instanceof RunStartError ? err.message : String(err);
    } finally {
      btn.disabled = false;
    }
  });
  form.appendChild(errBox);
  form.appendChild(btn);
  form.appendChild(logBox);
  mount.appendChild(el("section", { class: "card" }, [form]));
}

export async function renderRecoveryDetail(mount, requestId) {
  mount.innerHTML = "";
  const header = el("div", { class: "host-header" }, [
    el("a", { class: "back-link", href: "#/recovery", text: "← Recovery" }),
    el("h2", { text: requestId }),
  ]);
  mount.appendChild(header);

  const body = el("div");
  mount.appendChild(body);
  async function refresh(failure) {
    body.innerHTML = "";
    const card = failureCard(failure);
    if (card) body.appendChild(card);
    const res = await apiFetch(`/api/recovery/${encodeURIComponent(requestId)}`);
    const data = await res.json();
    if (!res.ok) {
      renderErrorBox(body, data);
      return;
    }
    const latestRun = data.active_run || data.latest_run;
    const unresolved = failure?.run_id ? failure : latestRun?.status === "unknown" ? { run_id: latestRun.run_id, record: latestRun } : null;
    const reconcile = reconciliationCard(unresolved, refresh);
    if (reconcile) body.appendChild(reconcile);
    if (latestRun && ["pending", "queued", "running"].includes(latestRun.status)) {
      const update = el("button", { type: "button", text: "Refresh recovery progress" });
      update.addEventListener("click", async () => {
        update.disabled = true;
        try { await refresh(); }
        catch (error) { if (error.name !== "AbortError") body.appendChild(helperText(error.message || String(error), "error")); }
        finally { update.disabled = false; }
      });
      body.appendChild(el("section", { class: "panel" }, [
        el("h3", { text: "Recovery operation in progress" }),
        helperText(`Run ${latestRun.run_id} · ${latestRun.status}. Reconnect to this request to inspect the same operation.`),
        helperText(`Started: ${latestRun.started_at || "unknown"} · Last update: ${latestRun.updated_at || "unknown"}`),
        el("pre", { class: "run-log", text: (latestRun.log_tail || []).join("\n") }), update,
      ]));
    }
    renderRequest(body, requestId, { ...data, operation_blocked: Boolean(unresolved || latestRun && ["pending", "queued", "running"].includes(latestRun.status)) }, refresh);
  }

  await refresh();
}

function stateGuidance(req) {
  const map = {
    awaiting_approval: "Analyze the recovery request, then approve with a different actor than the requester.",
    approved: "Next: authorize while the declared window is open.",
    authorized: "Next: execute. Execute’s actor must equal the authorizer.",
    completed: "Backup completed. For a live host, validate it against current discovery from the host’s Recovery stage before patch planning.",
    blocked: "Preparation is blocked. Inspect the analysis or failure reason before creating a corrected request.",
    running: `Recovery is running: ${req.execution?.phase || "phase pending"}.`,
    failed_services_restored: "Failed after services restore attempt — reconcile, then inspect failure phase.",
    validation_failed: "Validation failed — reconcile and inspect failure details.",
    recovery_required: "Recovery required — reconcile after reviewing failure phase.",
  };
  return map[req.state] || `State: ${req.state}.`;
}

function renderRequest(body, requestId, req, refresh) {
  body.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("Mode: "), badge(req.mode === "test_mode" ? "TEST_MODE fixture" : req.mode === "live" ? "Live host" : "Unknown", req.mode === "test_mode" ? "warn" : "neutral")]),
      el("span", {}, [document.createTextNode("State: "), badge(req.state, classifyStatus(req.state))]),
      el("span", {}, [document.createTextNode("Requester: "), el("strong", { text: req.requester || "—" })]),
      el("span", {}, [document.createTextNode("Window: "), el("strong", { text: `${req.maintenance_window?.start || "?"} → ${req.maintenance_window?.end || "?"}` })]),
      el("span", {}, [document.createTextNode("Backup root: "), el("code", { class: "mono", text: req.backup?.root || "—" })]),
    ])
  );
  body.appendChild(nextStepBanner(stateGuidance(req)));
  if (req.target?.database_unique_name) body.appendChild(helperText(`Database: ${req.target.database_unique_name} · Host: ${req.host_id || "unknown"} · Oracle home: ${req.target.oracle_home || "unknown"}`));
  body.appendChild(helperText("Workflow: analyze capacity → approve preparation → authorize → prepare backup and run RMAN restore validation → verify database and listener → select backup for patch readiness."));
  body.appendChild(helperText("Restore validation checks that RMAN can read the backup for restoration. It does not perform a test restore to another database."));
  if (req.state === "awaiting_approval") {
    body.appendChild(analysisPanel(requestId, req.analysis, refresh, req.operation_blocked));
  } else if (req.approval?.analysis) {
    body.appendChild(analysisSummary(req.approval.analysis, "Analysis recorded with approval"));
    body.appendChild(helperText("This analysis records the approval checks. Execution repeats live checks before stopping services."));
  } else if (req.analysis) body.appendChild(analysisSummary(req.analysis));

  const controls = el("section", { class: "card" });
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  controls.appendChild(errBox);

  if (req.operation_blocked) {
    controls.appendChild(el("h2", { text: "Existing operation must finish or be reconciled" }));
    controls.appendChild(helperText("Inspect the existing run above. Starting another backup or a service recovery is blocked until its outcome is verified.", "warn"));
  } else if (req.state === "awaiting_approval") {
    controls.appendChild(el("h2", { text: "Approve" }));
    controls.appendChild(helperText("Actor must differ from the requester (SoD). Synced with Acting as."));
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const ticket = el("input", { type: "text", placeholder: "approval ticket / change #" });
    const btn = el("button", { type: "button", text: "Approve" });
    btn.disabled = req.analysis?.status !== "passed";
    if (btn.disabled) controls.appendChild(helperText("Run analysis and resolve its findings before approval.", "warn"));
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      if (req.analysis?.status !== "passed") { showFormError(errBox, "A passed recovery analysis is required before approval."); return; }
      const who = requireActor(actor, errBox, "Actor");
      if (!who) return;
      if (who === req.requester) { showFormError(errBox, "The requester cannot approve their own recovery preparation."); return; }
      if (!requireNonEmpty(ticket, errBox, "Approval ticket")) return;
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/approve`, { actor: who, approval_ticket: ticket.value }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), field("Ticket", ticket), btn]));
  } else if (req.state === "approved") {
    controls.appendChild(el("h2", { text: "Authorize" }));
    controls.appendChild(helperText("Authorize only succeeds while the declared window is open.", "warn"));
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const btn = el("button", { type: "button", text: "Authorize" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who) return;
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/authorize`, { actor: who }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), btn]));
  } else if (req.state === "authorized") {
    controls.appendChild(el("h2", { text: "Execute" }));
    controls.appendChild(
      helperText(
        req.mode === "test_mode"
          ? "TEST_MODE executes against the fixture database and listener only. The execution actor must equal the authorizer."
          : "Execution stops the listener and shuts down this database for a cold RMAN backup, validates the backup, and restores services. Database downtime is required. The execution actor must equal the authorizer.",
        "warn"
      )
    );
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const btn = el("button", { type: "button", text: "Execute" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who) return;
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/execute`, { actor: who }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), btn]));
  } else if (req.state === "completed") {
    const evidencePath = req.result?.recovery_evidence?.path;
    controls.appendChild(el("h2", { text: "Completed" }));
    controls.appendChild(
      el("p", { class: "helper-text" }, [
        document.createTextNode("recovery-evidence.json: "),
        el("code", { class: "mono", text: evidencePath || "—" }),
      ])
    );
    controls.appendChild(
      helperText(req.mode === "test_mode" ? "This evidence belongs to the TEST_MODE fixture." : "Validate this backup against current host discovery before using it for patch planning.")
    );
    if (req.mode === "live" && req.host_id) controls.appendChild(el("p", { class: "stage-next" }, [
      el("a", { href: `#/hosts/${encodeURIComponent(req.host_id)}/recovery`, text: "Validate for patch planning on this host →" }),
    ]));
  } else if (["failed_services_restored", "validation_failed", "recovery_required"].includes(req.state)) {
    controls.appendChild(el("h2", { text: "Failed — services restoration attempted automatically" }));
    controls.appendChild(
      helperText(
        `Failure phase: ${req.failure?.phase || "unknown"}. Services restored: ${req.failure?.services_restored}`,
        "error"
      )
    );
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const btn = el("button", { type: "button", text: "Reconcile" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who) return;
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/reconcile`, { actor: who }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), btn]));
  } else {
    controls.appendChild(el("h2", { text: "No actions available for this state" }));
    controls.appendChild(helperText(stateGuidance(req)));
  }

  controls.appendChild(logBox);
  body.appendChild(controls);

  body.appendChild(
    el("details", { class: "pipeline-result" }, [el("summary", { text: "Request JSON" }), el("pre", { class: "mono", text: JSON.stringify(req, null, 2) })])
  );
}

function formatCapacity(value) {
  return Number.isFinite(value) ? `${(value / 1024 ** 3).toFixed(2)} GiB` : "Not available";
}

function analysisSummary(analysis, title = "Recovery analysis") {
  const capacity = analysis.capacity || {};
  const panel = el("section", { class: "card recovery-analysis" }, [
    el("h2", { text: title }),
    badge(analysis.status || "Not analyzed", classifyStatus(analysis.status)),
  ]);
  if (analysis.reason) panel.appendChild(helperText(analysis.reason, analysis.status === "blocked" ? "error" : null));
  if (analysis.status) panel.appendChild(el("div", { class: "meta-row" }, [
    el("span", { text: `Required capacity: ${formatCapacity(capacity.required_bytes)}` }),
    el("span", { text: `Available capacity: ${formatCapacity(capacity.available_bytes)}` }),
    el("span", { text: `Estimate: ${capacity.capacity_basis || capacity.basis || "Not available"}` }),
  ]));
  if (analysis.checked_at || analysis.analyzed_at) panel.appendChild(helperText(`Analyzed: ${analysis.checked_at || analysis.analyzed_at}`));
  return panel;
}

function analysisPanel(requestId, analysis, refresh, operationBlocked = false) {
  const panel = analysisSummary(analysis || {});
  panel.appendChild(helperText("Analysis probes SPFILE use, live database identity, role, open and log modes, capacity and the backup location before approval. Execution checks them again before stopping services."));
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const btn = el("button", { type: "button", text: analysis ? "Refresh analysis" : "Analyze recovery" });
  btn.disabled = operationBlocked;
  btn.addEventListener("click", async () => {
    if (operationBlocked || btn.disabled) return;
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/analyze`, {}, refresh, errBox);
  });
  panel.appendChild(errBox);
  panel.appendChild(btn);
  panel.appendChild(logBox);
  return panel;
}

async function runAction(logBox, btn, url, payload, refresh, errBox) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.classList.remove("run-log-error");
  logBox.textContent = "working…";
  try {
    const record = await runToCompletion(url, payload, {
      onTick: (run) => { logBox.textContent = `status: ${run.status}\n${(run.log_tail || []).join("\n")}`; },
    });
    if (record.status === "failed") {
      logBox.classList.add("run-log-error");
      logBox.textContent = formatRunFailure(record);
      if (errBox) showFormError(errBox, record.error?.message || "Action failed");
      await refresh({ message: record.error?.message || "Action failed", detail: formatRunFailure(record) });
    } else {
      logBox.textContent = "succeeded";
      if (errBox) clearFormError(errBox);
      await refresh(undefined, record.result);
    }
  } catch (err) {
    logBox.classList.add("run-log-error");
    const msg = err instanceof RunStartError ? err.message : String(err);
    logBox.textContent = msg;
    if (errBox) showFormError(errBox, msg);
    try {
      await refresh(executionFailure(err));
    } catch (_refreshErr) {
      // Keep the inline error if the refresh itself fails.
    }
  } finally {
    btn.disabled = false;
  }
}
