import { el, badge } from "./dom.js";
import { apiFetch } from "./api.js";
import { getActor } from "./actor.js";
import { pollRun } from "./runs.js";
import { field, bindActorField, formErrorBox, showFormError, clearFormError, requireActor, requireToken, helperText } from "./ux.js";

export function canInspectExtjob(plan, tasks) {
  if (!plan || !Array.isArray(tasks) || tasks.some(task => !task || typeof task !== "object") || plan.intent !== "patch_apply" || plan.procedure?.adapter !== "database_single_instance_opatch" || !Array.isArray(plan.nodes) || plan.nodes.length !== 1 || tasks.length !== 5) return false;
  const expected = [[`001-precheck-${plan.nodes[0]}`, "precheck"], [`002-apply-${plan.nodes[0]}`, "apply"], [`003-validate-${plan.nodes[0]}`, "validate"], ["004-datapatch-local", "datapatch"], ["005-final-validate-local", "final_validate"]];
  const rows = expected.map(([id, stage]) => tasks.filter(task => task.task_id === id && task.stage === stage));
  return rows.every(matches => matches.length === 1) && rows.slice(0, 4).every(([task]) => task.status === "succeeded") &&
    ((plan.state === "running" && rows[4][0].status === "pending") || (plan.state === "paused" && rows[4][0].status === "failed"));
}

/** Read-only comparison. The server independently verifies custody and authority. */
export function extjobInspection(planId, plan, tasks, blocked = false) {
  if (!canInspectExtjob(plan, tasks)) return null;
  const panel = el("section", { class: "panel extjob-inspection" }, [
    el("h3", { class: "panel-title", text: "Inspect final-validation prerequisite" }),
    helperText("Inspect extjob ownership, mode and its registered archive reference. This read-only operation can run after the patch window closes. It does not retry a task, repair permissions or approve a repair."),
    helperText("A controller administrator must register a trusted comparison reference before inspection. A matching archive alone does not establish Oracle publisher authenticity."),
  ]);
  const actor = el("input", { type: "text", value: getActor() });
  bindActorField(actor);
  const inspect = el("button", { type: "button", text: "Inspect extjob (read-only)" });
  const saved = el("button", { type: "button", text: "Load saved inspection" });
  const err = formErrorBox(), result = el("div", { "aria-live": "polite" });
  let busy = false, eligible = true;
  const setBusy = value => { busy = value; inspect.disabled = value || blocked || !eligible; saved.disabled = value; };
  setBusy(false);
  if (blocked) panel.appendChild(helperText("An existing operation must finish or be reconciled before another inspection can start.", "warn"));
  if (plan.sod?.operator) panel.appendChild(helperText(`Inspection actor must be the sealed authorizer: ${plan.sod.operator}.`));
  panel.appendChild(el("div", { class: "pipeline-controls" }, [field("Inspection actor", actor), inspect, saved]));
  panel.appendChild(err); panel.appendChild(result);

  const validRunId = value => typeof value === "string" && /^[a-f0-9]{12}$/.test(value);
  const sameHash = (left, right) => typeof left === "string" && /^[a-f0-9]{64}$/.test(left) && left === right;
  const json = async url => (await apiFetch(url)).json();
  function checkRun(record, expectedId) {
    if (!validRunId(expectedId) || record?.run_id !== expectedId || record.kind !== "extjob_inspect" || record.key !== `plan:${planId}:execute`) throw new Error("The existing run belongs to another operation. Inspect its execution timeline before continuing.");
  }
  async function display(record, expectedId) {
    checkRun(record, expectedId);
    if (record.status !== "succeeded") throw new Error(record.error?.message || `Inspection run is ${record.status}; no verified comparison is available.`);
    const report = record.result;
    if (!report || report.plan_id !== planId || report.read_only !== true || report.mutation_authorized !== false || !["inspected", "blocked"].includes(report.status)) throw new Error("The saved inspection does not match this plan.");
    let latestPlan, latestTasks = [], refreshError = null;
    try {
      const [currentPlan, currentTasks] = await Promise.all([
        json(`/api/plans/${encodeURIComponent(planId)}`), json(`/api/plans/${encodeURIComponent(planId)}/tasks`),
      ]);
      latestPlan = currentPlan; latestTasks = currentTasks?.tasks;
      eligible = currentPlan?.plan_id === planId && canInspectExtjob(currentPlan, latestTasks);
      blocked = blocked || Boolean(currentPlan?.unresolved_run);
    } catch (error) {
      if (error.name === "AbortError") throw error;
      refreshError = error.message || String(error);
      eligible = false;
    }
    const final = Array.isArray(latestTasks) ? latestTasks.find(task => task?.task_id === "005-final-validate-local") : null;
    const authority = report.authority;
    const current = eligible && !blocked && sameHash(report.plan_sha256, latestPlan.plan_sha256) &&
      authority?.plan_state === latestPlan.state && authority?.final_task_id === final.task_id && authority?.final_task_status === final.status &&
      (final.status !== "failed" || sameHash(authority?.final_task_result_sha256, final.task_result_sha256)) &&
      sameHash(authority?.native_datapatch_evidence_sha256, latestTasks.find(task => task.stage === "datapatch").evidence_sha256) &&
      sameHash(authority?.native_apply_evidence_sha256, latestTasks.find(task => task.stage === "apply").evidence_sha256);
    result.appendChild(helperText(`Saved inspection run ${record.run_id}. Loading this result does not execute or retry a task.`));
    result.appendChild(badge(report.status === "blocked" ? "Comparison blocked" : "Comparison inspected", "warn"));
    if (!current) result.appendChild(helperText("Historical or incomplete authority: this result does not establish the current prerequisite. Run a new inspection when eligible.", "warn"));
    if (refreshError) result.appendChild(helperText(`Current plan authority could not be refreshed: ${refreshError}`, "warn"));
    if (current) result.appendChild(helperText("The saved authority matches the latest plan and task records. File observations are from this inspection; they do not prove the host is unchanged now."));
    if (report.error) result.appendChild(helperText(String(report.error).slice(0, 2000), "warn"));
    if (current && report.current) {
      result.appendChild(el("p", { text: `Observed extjob: UID ${report.current.uid} · mode ${report.current.mode}` }));
      result.appendChild(el("p", { class: "mono", text: `SHA-256: ${report.current.sha256}` }));
    }
    result.appendChild(helperText("Verified content provenance and separate authorization are required before considering a supported repair."));
    result.appendChild(el("details", { class: "pipeline-result" }, [el("summary", { text: "Saved inspection JSON" }), el("pre", { class: "mono", text: JSON.stringify(report, null, 2) })]));
  }
  inspect.addEventListener("click", async () => {
    if (busy || blocked || !eligible) return;
    clearFormError(err); result.innerHTML = "";
    if (!requireToken(err)) return;
    const who = requireActor(actor, err, "Inspection actor");
    if (!who) return;
    if (plan.sod?.operator && who !== plan.sod.operator) { showFormError(err, "Inspection actor must match the sealed plan authorizer."); return; }
    setBusy(true);
    try {
      const response = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/extjob-inspect`, {
        method: "POST", body: JSON.stringify({ actor: who }), acceptedStatuses: [409],
      });
      const submitted = await response.json();
      if (!validRunId(submitted.run_id) || (!response.ok && !(response.status === 409 && submitted.error === "run_in_progress"))) throw new Error(submitted.message || "Inspection did not return a valid managed run.");
      if (response.status === 409) {
        // The shared execution reservation may belong to a patch task. Read
        // its identity once and reject it before joining or showing progress.
        checkRun(await json(`/api/runs/${encodeURIComponent(submitted.run_id)}`), submitted.run_id);
      }
      const record = await pollRun(submitted.run_id, {
        onTick: item => { checkRun(item, submitted.run_id); result.textContent = `Inspection run ${item.run_id} · ${item.status}`; },
      });
      result.innerHTML = ""; await display(record, submitted.run_id);
    } catch (error) { showFormError(err, error.message || String(error)); }
    finally { setBusy(false); }
  });
  saved.addEventListener("click", async () => {
    if (busy) return;
    clearFormError(err); result.innerHTML = ""; setBusy(true);
    try {
      const response = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/execution`);
      const data = await response.json();
      const runs = (Array.isArray(data.runs) ? data.runs : []).filter(run => run?.kind === "extjob_inspect" && validRunId(run.run_id)).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      if (!runs.length) { result.appendChild(helperText("No saved extjob inspection is available for this plan.")); return; }
      const run = await apiFetch(`/api/runs/${encodeURIComponent(runs[0].run_id)}`);
      await display(await run.json(), runs[0].run_id);
    } catch (error) { showFormError(err, error.message || String(error)); }
    finally { setBusy(false); }
  });
  return panel;
}
