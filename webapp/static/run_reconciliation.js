import { el } from "./dom.js";
import { apiFetch } from "./api.js";
import { getActor } from "./actor.js";
import { startRun, pollRun } from "./runs.js";
import { field, bindActorField, helperText, formErrorBox, showFormError, clearFormError, requireActor, requireToken } from "./ux.js";

export function executionFailure(error) {
  const runId = error.runId || (error.data?.error === "reconciliation_required" ? error.data.run_id : null);
  return { message: error.message || String(error), run_id: runId, record: error.record };
}

/** Inspect an existing launch through the existing API; never execute or retry. */
export function reconciliationCard(failure, refresh) {
  if (!failure?.run_id) return null;
  const runId = failure.run_id;
  const record = failure.record;
  const context = record?.context || {};
  const isMaintenance = context.lock_recovery === true;
  const panel = el("section", { class: "card card-failure", role: "alert" }, [
    el("h2", { text: isMaintenance ? "Lock recovery outcome needs reconciliation" : "Execution outcome needs reconciliation" }),
    helperText(`Run ${runId}${record?.context?.task_id ? ` · ${record.context.task_id}` : ""}`),
    helperText("Execution is blocked until the existing launch has a verified result. This action checks its records without starting a task.", "warn"),
  ]);
  if (record?.error) {
    const detail = [record.error.message, record.error.stderr].filter(Boolean).join("\n\n");
    panel.appendChild(el("pre", { class: "run-log run-log-error", text: detail }));
  }
  const actor = el("input", { type: "text", value: getActor() });
  bindActorField(actor);
  const errBox = formErrorBox();
  const btn = el("button", { type: "button", text: "Inspect and reconcile" });
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    const who = requireActor(actor, errBox);
    if (!who) return;
    btn.disabled = true;
    try {
      const response = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/reconcile`, {
        method: "POST", body: JSON.stringify({ actor: who }),
      });
      const result = await response.json();
      if (["succeeded", "failed"].includes(result.status)) {
        await refresh({ title: "Run reconciled", message: isMaintenance ? `Lock recovery run reconciled as ${result.status}. Review the original execution and pending validation before continuing.` : `Existing run reconciled as ${result.status}. Review the task results before continuing.`, detail: result.error?.stderr || result.error?.message });
      } else {
        await refresh({ message: "The execution outcome is still unverified. Further execution remains blocked.", run_id: runId, record: result });
      }
    } catch (error) {
      showFormError(errBox, error.message || String(error));
    } finally { btn.disabled = false; }
  });
  const controls = el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]);
  panel.appendChild(controls);
  const diagnostic = record?.error?.stderr || "";
  const lockRejected = !isMaintenance && typeof context.plan_id === "string" && context.plan_id.length > 0 &&
    /^003-validate(?:-|$)/.test(context.task_id || "") && record?.error?.result?.task_status === "pending" &&
    /another Oracle executor owns this host or its lock file is unsafe|another Oracle operation owns the (?:shared )?host lock/.test(diagnostic);
  if (lockRejected && !context.lock_recovery_run_id) {
    const inspectBtn = el("button", { type: "button", text: "Inspect host lock" });
    const inspection = el("div", { class: "lock-inspection" });
    controls.appendChild(inspectBtn);
    panel.appendChild(inspection);
    let busy = false;
    let eligibility = null;
    let recoverBtn = null;
    const setBusy = value => {
      busy = value;
      btn.disabled = value;
      inspectBtn.disabled = value;
      if (recoverBtn) recoverBtn.disabled = value;
    };
    const runLockOperation = async operation => {
      if (busy) return;
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox);
      if (!who || (operation === "recover" && !eligibility)) return;
      setBusy(true);
      let maintenanceRunId = null;
      try {
        const submitted = await startRun(`/api/plans/${encodeURIComponent(context.plan_id)}/lock-${operation}`, { actor: who, run_id: runId });
        maintenanceRunId = submitted;
        const completed = await pollRun(submitted);
        if (completed.status !== "succeeded") {
          const error = completed.error || {};
          throw new Error([error.message || "Host lock operation failed", error.stderr].filter(Boolean).join("\n\n"));
        }
        const result = completed.result || {};
        if (operation === "recover") {
          if (result.status !== "completed" || result.execution_run_id !== runId || result.maintenance_run_id !== submitted) {
            await refresh({ message: "Lock recovery has no verified completion. Reconcile its existing run before continuing.", run_id: submitted, record: completed });
            return;
          }
          await refresh({ title: "Inherited host lock recovered", message: "Database and listener health were verified. Validation remains pending; review the plan before continuing." });
          return;
        }
        eligibility = result.status === "eligible" && result.recovery_eligible === true &&
          result.plan_id === context.plan_id && result.task_id === context.task_id && result.execution_run_id === runId &&
          Array.isArray(result.blockers) && result.blockers.length === 0 ? result : null;
        inspection.replaceChildren();
        if (!eligibility) {
          const blockers = Array.isArray(result.blockers) ? result.blockers.filter(item => typeof item === "string") : [];
          inspection.appendChild(helperText("Host lock recovery is blocked. No services were changed.", "warn"));
          inspection.appendChild(el("pre", { class: "run-log run-log-error", text: blockers.length ? blockers.join("\n") : "Inspection did not prove eligibility for this execution." }));
          recoverBtn = null;
          return;
        }
        const database = typeof result.target?.database_unique_name === "string" && result.target.database_unique_name ? result.target.database_unique_name : "bound";
        inspection.appendChild(helperText(`Inspection proved the expected inherited lock holders. Recovery performs a controlled restart of this plan’s ${database} database and listener, then verifies health. Validation stays pending.`, "warn"));
        recoverBtn = el("button", { type: "button", text: "Recover inherited lock" });
        recoverBtn.addEventListener("click", () => runLockOperation("recover"));
        inspection.appendChild(recoverBtn);
      } catch (error) {
        const unknown = executionFailure(error);
        if (unknown.run_id) {
          await refresh(unknown);
        } else if (operation === "recover" && maintenanceRunId) {
          // A transport failure after submission cannot authorize another
          // restart. Keep the existing run available for reconciliation.
          await refresh({ message: error.message || String(error), run_id: maintenanceRunId,
            record: { context: { lock_recovery: true, plan_id: context.plan_id }, error: { message: error.message || String(error) } } });
        } else {
          showFormError(errBox, error.message || String(error));
        }
      } finally { setBusy(false); }
    };
    inspectBtn.addEventListener("click", async () => {
      if (busy) return;
      eligibility = null;
      recoverBtn = null;
      inspection.replaceChildren();
      await runLockOperation("inspect");
    });
  } else if (context.lock_recovery_run_id) {
    panel.appendChild(helperText(`Lock recovery run ${context.lock_recovery_run_id} was already submitted. Reconcile the existing execution to check its recorded outcome.`, "warn"));
  }
  panel.appendChild(errBox);
  return panel;
}
