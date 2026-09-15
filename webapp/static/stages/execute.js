import { el, badge, classifyStatus } from "../dom.js";
import { apiFetch } from "../api.js";
import { runToCompletion, RunStartError } from "../runs.js";
import { executionFailure, reconciliationCard } from "../run_reconciliation.js";
import { executionConsole } from "../execution_console.js";
import { evidenceReport } from "../report_view.js";
import { executionWindow } from "../plan_window.js";
import { extjobInspection } from "../extjob_inspection.js";
import { getActor } from "../actor.js";
import { planBelongsToHost, newestFirst } from "../host_scope.js";
import {
  field,
  bindActorField,
  formErrorBox,
  showFormError,
  clearFormError,
  requireActor,
  requireToken,
  helperText,
  formatRunFailure,
  failureCard,
} from "../ux.js";

export async function renderExecuteStage(mount, hostId, failure = null) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "stage-head" }, [
      el("h2", { class: "stage-title", text: "Execute" }),
      el("p", {
        class: "stage-lead",
        text: "Dispatch authorized plans and run sealed tasks for this host.",
      }),
    ])
  );

  if (failure) mount.appendChild(reconciliationCard(failure, (next) => renderExecuteStage(mount, hostId, next)) || failureCard(failure));
  const res = await apiFetch("/api/plans");
  const data = await res.json();
  const plans = newestFirst(data.plans || []).filter((p) => planBelongsToHost(p, hostId));
  const actionable = plans.filter((p) =>
    ["execution_authorized", "running", "paused", "succeeded", "failed"].includes(p.state)
  );

  if (!actionable.length) {
    mount.appendChild(
      helperText("No authorized or running plans for this host. Approve and authorize from Plan / plan detail first.", "warn")
    );
    mount.appendChild(
      el("p", { class: "stage-next" }, [
        el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/plan`, text: "← Plan stage" }),
      ])
    );
    return;
  }

  for (const summary of actionable) {
    mount.appendChild(await planExecutePanel(summary.plan_id, (error) => {
      if (mount.isConnected) return renderExecuteStage(mount, hostId, error);
    }, Boolean(failure?.run_id)));
  }
}

async function planExecutePanel(planId, reload, unresolved = false) {
  const panel = el("section", { class: "panel" });
  const res = await apiFetch(`/api/plans/${encodeURIComponent(planId)}`);
  const plan = await res.json();
  if (!res.ok) {
    panel.appendChild(helperText(plan.message || "Failed to load plan", "error"));
    return panel;
  }
  if (plan.unresolved_run) {
    unresolved = true;
    panel.appendChild(reconciliationCard({ run_id: plan.unresolved_run.run_id, record: plan.unresolved_run }, reload));
  }

  panel.appendChild(
    el("div", { class: "step-card-head" }, [
      el("h3", { text: planId }),
      badge(plan.state, classifyStatus(plan.state)),
    ])
  );
  panel.appendChild(
    el("p", { class: "helper-text" }, [
      el("a", { class: "back-link", href: `#/plans/${encodeURIComponent(planId)}`, text: "Full plan detail" }),
    ])
  );

  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  panel.appendChild(errBox);

  const tasks = await loadTasks(planId);
  const window = executionWindow(plan);
  if (["execution_authorized", "running", "paused"].includes(plan.state) && !window.open) {
    panel.appendChild(helperText(window.detail, "warn"));
  }

  if (plan.state === "execution_authorized") {
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const btn = el("button", { type: "button", text: "Dispatch" });
    btn.disabled = !window.open || unresolved;
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (unresolved) return;
      const currentWindow = executionWindow(plan);
      if (!currentWindow.open) { showFormError(errBox, currentWindow.detail); return; }
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox);
      if (!who) return;
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/dispatch`, { actor: who }, reload, errBox);
    });
    panel.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
  } else if (plan.state === "running") {
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const btn = el("button", { type: "button", text: "Execute next" });
    const btnAll = el("button", { type: "button", text: "Execute remaining" });
    btn.disabled = unresolved || !window.open;
    btnAll.disabled = unresolved || !window.open;
    btn.addEventListener("click", async () => {
      if (unresolved) return;
      clearFormError(errBox);
      const currentWindow = executionWindow(plan);
      if (!currentWindow.open) { showFormError(errBox, currentWindow.detail); return; }
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox);
      if (!who) return;
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/execute-next`, { actor: who }, reload, errBox);
    });
    btnAll.addEventListener("click", async () => {
      if (unresolved) return;
      clearFormError(errBox);
      const currentWindow = executionWindow(plan);
      if (!currentWindow.open) { showFormError(errBox, currentWindow.detail); return; }
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox);
      if (!who) return;
      await runAction(
        logBox,
        btnAll,
        `/api/plans/${encodeURIComponent(planId)}/execute-remaining`,
        { actor: who },
        reload,
        errBox
      );
    });
    panel.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn, btnAll]));
  } else if (plan.state === "paused") {
    const failed = tasks.find((t) => t.status === "failed");
    panel.appendChild(
      helperText(
        failed
          ? `Paused after ${failed.task_id} failed. Inspect the saved failure in the evidence report. Retry requires a corrected blocker, an open window and a retryable native outcome. Completed tasks are preserved.`
          : "Plan is paused. Inspect tasks before continuing.",
        "warn"
      )
    );
    if (failed) {
      const actor = el("input", { type: "text", value: getActor() });
      bindActorField(actor);
      const btn = el("button", { type: "button", text: `Retry ${failed.task_id}` });
      btn.disabled = unresolved || !window.open;
      btn.addEventListener("click", async () => {
        clearFormError(errBox);
        if (unresolved) return;
        const currentWindow = executionWindow(plan);
        if (!currentWindow.open) { showFormError(errBox, currentWindow.detail); return; }
        if (!requireToken(errBox)) return;
        const who = requireActor(actor, errBox);
        if (!who) return;
        await runAction(
          logBox,
          btn,
          `/api/plans/${encodeURIComponent(planId)}/retry-task`,
          { actor: who, task_id: failed.task_id },
          reload,
          errBox
        );
      });
      panel.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
    }
  }

  panel.appendChild(renderTaskTable(tasks));
  const inspection = extjobInspection(planId, plan, tasks, unresolved);
  if (inspection) panel.appendChild(inspection);
  panel.appendChild(logBox);
  panel.appendChild(executionConsole(planId));
  panel.appendChild(evidenceReport(planId));
  return panel;
}

async function loadTasks(planId) {
  const res = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/tasks`);
  const data = await res.json();
  return data.tasks || [];
}

function renderTaskTable(tasks) {
  if (!tasks.length) return helperText("No tasks materialized yet.");
  return el("table", {}, [
    el("thead", {}, [
      el("tr", {}, [el("th", { text: "Task" }), el("th", { text: "Stage" }), el("th", { text: "Node" }), el("th", { text: "Status" })]),
    ]),
    el(
      "tbody",
      {},
      tasks.map((t) =>
        el("tr", {}, [
          el("td", { text: t.task_id || "—" }),
          el("td", { text: t.stage || "—" }),
          el("td", { text: t.node || t.target?.node || "—" }),
          el("td", {}, [badge(t.status, classifyStatus(t.status))]),
        ])
      )
    ),
  ]);
}

async function runAction(logBox, btn, url, body, refresh, errBox) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.classList.remove("run-log-error");
  logBox.textContent = "working…";
  try {
    const record = await runToCompletion(url, body);
    if (record.status === "failed") {
      logBox.classList.add("run-log-error");
      logBox.textContent = formatRunFailure(record);
      showFormError(errBox, record.error?.message || "Action failed");
    } else {
      logBox.textContent = "succeeded";
      clearFormError(errBox);
    }
    await refresh(record.status === "failed"
      ? { message: record.error?.message || "Action failed", detail: formatRunFailure(record) }
      : null);
  } catch (err) {
    const msg = err instanceof RunStartError ? err.message : String(err);
    logBox.classList.add("run-log-error");
    logBox.textContent = msg;
    showFormError(errBox, msg);
    try {
      await refresh(executionFailure(err));
    } catch {
      /* keep the error visible even if reload fails */
    }
  } finally {
    btn.disabled = false;
  }
}
