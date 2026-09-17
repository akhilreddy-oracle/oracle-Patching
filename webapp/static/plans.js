import { el, badge, classifyStatus, renderErrorBox } from "./dom.js";
import { apiFetch } from "./api.js";
import { newestFirst } from "./host_scope.js";
import { runToCompletion, RunStartError } from "./runs.js";
import { executionFailure, reconciliationCard } from "./run_reconciliation.js";
import { executionConsole } from "./execution_console.js";
import { evidenceReport } from "./report_view.js";
import { executionWindow } from "./plan_window.js";
import { extjobInspection } from "./extjob_inspection.js";
import { getActor, setActor, authenticatedActor } from "./actor.js";
import {
  field,
  bindActorField,
  formErrorBox,
  showFormError,
  clearFormError,
  requireActor,
  requireToken,
  requireNonEmpty,
  emptyWithCta,
  helperText,
  nextStepBanner,
  pageIntro,
  formatRunFailure,
  failureCard,
} from "./ux.js";

export async function renderPlanList(mount) {
  mount.innerHTML = "";
  mount.appendChild(
    el("header", { class: "ws-header" }, [
      el("div", { class: "ws-header-text" }, [
        el("p", { class: "ws-kicker", text: "Indexes" }),
        el("h1", { class: "ws-title", text: "All plans" }),
        el("p", { class: "ws-sub", text: "Sealed change records across the estate. Prefer creating plans from a host workspace." }),
      ]),
    ])
  );
  mount.appendChild(
    nextStepBanner("Create from a host after ready_for_approval.", "#/estate", "Hosts →")
  );

  const res = await apiFetch("/api/plans");
  const data = await res.json();

  if (!data.plans?.length) {
    mount.appendChild(
      emptyWithCta(
        "No plans yet. Finish live host discovery and the readiness pipeline to ready_for_approval, then create a plan from that host.",
        "#/estate",
        "Open estate →"
      )
    );
  } else {
    const table = el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Plan ID" }), el("th", { text: "State" }), el("th", { text: "Patch" }), el("th", { text: "Requester" })])]),
      el(
        "tbody",
        {},
        newestFirst(data.plans).map((p) =>
          el("tr", {}, [
            el("td", {}, [el("a", { href: `#/plans/${encodeURIComponent(p.plan_id)}`, text: p.plan_id })]),
            el("td", {}, [badge(p.state, classifyStatus(p.state))]),
            el("td", { text: p.patch_id || "—" }),
            el("td", { text: p.requester || "—" }),
          ])
        )
      ),
    ]);
    mount.appendChild(el("section", { class: "card" }, [table]));
  }

  // Lab-only fixture path — kept behind an explicit demo route, not the primary CTA.
  mount.appendChild(
    el("details", { class: "card", style: "margin-top:16px" }, [
      el("summary", { text: "Lab only: TEST_MODE demo plan" }),
      el("p", { class: "helper-text" }, [
        document.createTextNode(
          "Optional fixture path for offline lab demos. Builds a fake Oracle or Grid home — it does not touch configured estate hosts. Prefer live hosts from the estate."
        ),
      ]),
      el("p", { class: "empty-cta-row" }, [
        el("a", { class: "empty-cta", href: "#/plans/demo-new", text: "Open TEST_MODE demo route →" }),
      ]),
    ])
  );
}

export async function renderPlanNew(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(
    pageIntro(
      "Sealed change",
      `New plan · ${hostId}`,
      "Requester must match Acting as in Session. Separation of duties later requires a different approver and operator."
    )
  );

  const planId = el("input", { type: "text", value: `${hostId}-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  bindActorField(requester);
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60000);
  const end = new Date(now.getTime() + 4 * 3600000);
  const windowStart = el("input", { type: "text", value: start.toISOString().replace(/\.\d+Z$/, "Z") });
  const windowEnd = el("input", { type: "text", value: end.toISOString().replace(/\.\d+Z$/, "Z") });

  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });

  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Plan ID", planId),
      field("Requester (actor)", requester, "Synced with Acting as in Session"),
      field("Window start (UTC)", windowStart),
      field("Window end (UTC)", windowEnd),
    ]),
  ]);

  const btn = el("button", { type: "button", text: "Create plan" });
  btn.addEventListener("click", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    const submittedId = requireNonEmpty(planId, errBox, "Plan ID");
    if (!submittedId) return;
    const actor = requireActor(requester, errBox, "Requester");
    if (!actor) return;
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "creating…";
    try {
      const record = await runToCompletion("/api/plans", {
        plan_id: submittedId,
        requester: actor,
        host_id: hostId,
        window_start: windowStart.value,
        window_end: windowEnd.value,
      });
      if (record.status === "failed") {
        logBox.classList.add("run-log-error");
        logBox.textContent = formatRunFailure(record);
      } else {
        location.hash = `#/plans/${encodeURIComponent(submittedId)}`;
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

export async function renderPlanDemoNew(mount) {
  mount.innerHTML = "";
  mount.appendChild(
    pageIntro(
      "Lab demo",
      "TEST_MODE patch plan",
      "Builds a fake Oracle or Grid home and drives a real sealed apply plan through the executor. Configured estate hosts are not touched."
    )
  );

  const planId = el("input", { type: "text", value: `demo-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  bindActorField(requester);
  const adapter = el("select", {}, [
    el("option", { value: "standalone", text: "Standalone database (single instance)" }),
    el("option", { value: "rac", text: "RAC database (two-node rolling)" }),
    el("option", { value: "grid", text: "Grid Infrastructure (two-node rolling)" }),
  ]);
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60000);
  const end = new Date(now.getTime() + 4 * 3600000);
  const windowStart = el("input", { type: "text", value: start.toISOString().replace(/\.\d+Z$/, "Z") });
  const windowEnd = el("input", { type: "text", value: end.toISOString().replace(/\.\d+Z$/, "Z") });

  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Plan ID", planId),
      field("Requester (actor)", requester, "Synced with Acting as in Session"),
      field("Fixture adapter", adapter),
      field("Window start (UTC)", windowStart),
      field("Window end (UTC)", windowEnd),
    ]),
  ]);

  const btn = el("button", { type: "button", text: "Build fixture and create plan" });
  btn.addEventListener("click", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    const submittedId = requireNonEmpty(planId, errBox, "Plan ID");
    if (!submittedId) return;
    const actor = requireActor(requester, errBox, "Requester");
    if (!actor) return;
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "building fixture…";
    try {
      const record = await runToCompletion("/api/plans/testmode-demo", {
        plan_id: submittedId,
        requester: actor,
        adapter: adapter.value,
        window_start: windowStart.value,
        window_end: windowEnd.value,
      });
      if (record.status === "failed") {
        logBox.classList.add("run-log-error");
        logBox.textContent = formatRunFailure(record);
      } else {
        location.hash = `#/plans/${encodeURIComponent(submittedId)}`;
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

export async function renderRollbackNew(mount, sourcePlanId) {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { text: `New rollback plan — source ${sourcePlanId}` }));
  mount.appendChild(
    helperText(
      "Target, artifact, and README rollback condition come from the source plan’s sealed final_validate evidence — nothing re-supplied by hand."
    )
  );

  const planId = el("input", { type: "text", value: `${sourcePlanId}-rollback-${Date.now().toString(36)}` });
  if (!getActor()) setActor("rollback-admin");
  const requester = el("input", { type: "text", value: getActor() });
  bindActorField(requester);
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60000);
  const end = new Date(now.getTime() + 4 * 3600000);
  const windowStart = el("input", { type: "text", value: start.toISOString().replace(/\.\d+Z$/, "Z") });
  const windowEnd = el("input", { type: "text", value: end.toISOString().replace(/\.\d+Z$/, "Z") });

  // Rollback approver and operator must differ from every worker that
  // executed the source apply; show them up front so three distinct actors
  // can be lined up before the plan is created.
  try {
    const res = await apiFetch(`/api/plans/${encodeURIComponent(sourcePlanId)}/tasks`);
    const data = await res.json();
    if (res.ok) {
      const workers = [...new Set((data.tasks || []).map((t) => t.claimed_by).filter(Boolean))];
      if (workers.length) {
        mount.appendChild(
          helperText(
            `SoD: the rollback approver and operator must each differ from the rollback requester and from every source-apply worker (${workers.join(", ")}). The requester itself may be any actor.`,
            "warn"
          )
        );
      }
    }
  } catch (_err) {
    // Non-fatal: the controller still enforces SoD at approve/authorize.
  }

  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Rollback plan ID", planId),
      field("Requester (actor)", requester, "Synced with Acting as in Session"),
      field("Window start (UTC)", windowStart),
      field("Window end (UTC)", windowEnd),
    ]),
  ]);

  const btn = el("button", { type: "button", text: "Create rollback plan" });
  btn.addEventListener("click", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    const submittedId = requireNonEmpty(planId, errBox, "Rollback plan ID");
    if (!submittedId) return;
    const actor = requireActor(requester, errBox, "Requester");
    if (!actor) return;
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "creating…";
    try {
      const record = await runToCompletion(`/api/plans/${encodeURIComponent(sourcePlanId)}/create-rollback`, {
        plan_id: submittedId,
        requester: actor,
        source_plan_id: sourcePlanId,
        window_start: windowStart.value,
        window_end: windowEnd.value,
      });
      if (record.status === "failed") {
        logBox.classList.add("run-log-error");
        logBox.textContent = formatRunFailure(record);
      } else {
        location.hash = `#/plans/${encodeURIComponent(submittedId)}`;
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

export async function renderPlanDetail(mount, planId) {
  mount.innerHTML = "";

  const header = el("div", { class: "host-header" }, [
    el("a", { class: "back-link", href: "#/plans", text: "← Plans" }),
    el("h2", { text: planId }),
  ]);
  mount.appendChild(header);

  const body = el("div");
  mount.appendChild(body);

  async function refresh(failure) {
    body.innerHTML = "";
    const res = await apiFetch(`/api/plans/${encodeURIComponent(planId)}`);
    const data = await res.json();
    const display = data.unresolved_run
      ? { run_id: data.unresolved_run.run_id, record: data.unresolved_run }
      : failure;
    const card = reconciliationCard(display, refresh) || failureCard(display);
    if (card) body.appendChild(card);
    if (!res.ok) {
      renderErrorBox(body, data);
      return;
    }
    await renderPlan(body, planId, data, refresh, Boolean(display?.run_id));
  }

  await refresh();
}

/**
 * Client-side mirror of opu-patch-plan separation-of-duties gates so the
 * operator sees who may act before the request is sent. The controller
 * remains authoritative (exit 77 on violation).
 */
function identityChangeHint() {
  return authenticatedActor()
    ? "Switch Session to the required person’s API token."
    : "Change Acting as in Session to the required person.";
}

function sodCheck(plan, step, actor) {
  const sod = plan.sod || {};
  const who = (actor || "").trim();
  if (!who) return null;
  const barred = sod.barred || {};
  const label = (name) => (name === sod.requester ? `${name} (requester)` : name === sod.approver ? `${name} (approver)` : (sod.source_apply_actors || []).includes(name) ? `${name} (source apply worker)` : name);
  if (step === "approve" && (barred.approve || []).includes(who)) {
    return `${label(who)} cannot approve this plan. Approver must differ from the requester${(sod.source_apply_actors || []).length ? " and from every source-apply worker" : ""}. ${identityChangeHint()}`;
  }
  if (step === "authorize" && (barred.authorize || []).includes(who)) {
    return `${label(who)} cannot authorize this plan. Operator must differ from the requester and the approver${(sod.source_apply_actors || []).length ? " and from every source-apply worker" : ""}. ${identityChangeHint()}`;
  }
  if (step === "dispatch") {
    const allowed = (sod.allowed || {}).dispatch || [];
    if (allowed.length && !allowed.includes(who)) {
      return `Only the authorizing operator (${allowed.join(", ")}) may dispatch this plan. ${identityChangeHint()}`;
    }
  }
  return null;
}

function sodHint(plan, step) {
  const sod = plan.sod || {};
  const parts = [];
  if (sod.requester) parts.push(`requester ${sod.requester}`);
  if (sod.approver) parts.push(`approver ${sod.approver}`);
  if (sod.operator) parts.push(`operator ${sod.operator}`);
  if ((sod.source_apply_actors || []).length) parts.push(`source apply worker(s) ${sod.source_apply_actors.join(", ")}`);
  const barred = (sod.barred || {})[step] || [];
  const tail =
    step === "dispatch"
      ? `Dispatch must be done by the authorizing operator${sod.operator ? ` (${sod.operator})` : ""}.`
      : barred.length
        ? `Cannot ${step}: ${barred.join(", ")}.`
        : "";
  return [parts.length ? `Sealed actors so far: ${parts.join("; ")}.` : "", tail].filter(Boolean).join(" ");
}

function stateGuidance(plan) {
  if (["execution_authorized", "running", "paused"].includes(plan.state)) {
    const windowState = executionWindow(plan);
    if (!windowState.open) return windowState.detail;
  }
  const map = {
    awaiting_approval: "Next: approve with a different actor than the requester (SoD). Ticket required if ITSM is on.",
    approved: "Next: authorize only while the maintenance window is open. Use a different actor than approver/requester as required by SoD.",
    execution_authorized: "Next: dispatch to materialize the task list and start the plan.",
    running: "Next: execute the next task (or remaining tasks). Stop on failed/blocked tasks.",
    paused: "Plan is paused after a failed task. Open the evidence report to inspect its saved failure. A retry needs a corrected blocker, an open window and native eligibility; completed tasks are preserved.",
    succeeded: plan.intent === "patch_apply" ? "Apply succeeded — you can create a rollback plan from this result." : "Plan succeeded.",
    failed: "Plan failed — inspect tasks and evidence before retrying or rolling back.",
  };
  return map[plan.state] || `State: ${plan.state}. No further UI action may be available.`;
}

/**
 * A sealed plan that can no longer move forward (window closed, readiness
 * expired, or its sealed evidence was regenerated). Explains every reason and
 * routes the operator to a fresh plan instead of a button that exits 66.
 */
function deadPlanCard(plan, viability) {
  const hostId = viability.host_id;
  const docs = (viability.documents || []).filter((d) => d.status !== "ok");
  const kids = [
    el("h2", { text: "This plan cannot proceed" }),
    el("p", { class: "helper-text helper-warn", text: `State is ${plan.state}, but opu-patch-plan will refuse the next step for these reasons:` }),
    el("ul", { class: "blocked-summary" }, (viability.blockers || []).map((b) => el("li", { text: b }))),
  ];
  if (docs.length) {
    kids.push(
      el("details", { class: "pipeline-result" }, [
        el("summary", { text: `Sealed documents (${docs.length} affected)` }),
        el("ul", { class: "blocked-summary" }, docs.map((d) => el("li", {}, [el("strong", { text: `${d.name}: ${d.status} ` }), el("code", { class: "mono", text: d.path || "" })]))),
      ])
    );
  }
  const links = [];
  if (hostId) {
    links.push(el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/readiness`, class: "back-link", text: `→ Re-run readiness for ${hostId}` }));
    links.push(document.createTextNode("  "));
    links.push(el("a", { href: `#/hosts/${encodeURIComponent(hostId)}/plan`, class: "back-link", text: `→ Create a new plan for ${hostId}` }));
  }
  if (links.length) kids.push(el("p", { class: "next-step" }, links));
  return el("section", { class: "card card-failure" }, kids);
}

async function renderPlan(body, planId, plan, refresh, unresolved = false) {
  body.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("Intent: "), el("strong", { text: plan.intent || "—" })]),
      el("span", {}, [document.createTextNode("State: "), badge(plan.state, classifyStatus(plan.state))]),
      el("span", {}, [document.createTextNode("Patch: "), el("strong", { text: plan.patch_id || "—" })]),
      el("span", {}, [document.createTextNode("Requester: "), el("strong", { text: plan.requester || "—" })]),
      el("span", {}, [document.createTextNode("Window: "), el("strong", { text: `${plan.maintenance_window?.start || "?"} → ${plan.maintenance_window?.end || "?"}` })]),
    ])
  );
  const viability = plan.viability || null;
  const dead = Boolean(viability && viability.pre_execution && viability.blockers?.length);
  if (dead) {
    body.appendChild(deadPlanCard(plan, viability));
  } else {
    body.appendChild(nextStepBanner(unresolved ? "Next: inspect and reconcile the interrupted run before executing another task." : stateGuidance(plan)));
  }

  if (plan.intent === "patch_rollback" && plan.source_apply) {
    body.appendChild(
      el("section", { class: "card" }, [
        el("h2", { text: "Rollback lineage" }),
        el("p", { class: "helper-text" }, [
          document.createTextNode("Source apply plan file: "),
          el("code", { class: "mono", text: plan.source_apply.plan_path || "—" }),
        ]),
        el("p", { class: "helper-text" }, [
          document.createTextNode("Sealed final_validate evidence: "),
          el("code", { class: "mono", text: plan.source_apply.final_evidence_path || "—" }),
        ]),
        plan.rollback_procedure
          ? el("p", { class: "helper-text", text: `Expected: binary ${plan.rollback_procedure.expected_binary_before} → ${plan.rollback_procedure.expected_binary_after}, SQL action ${plan.rollback_procedure.expected_sql_action_before} → ${plan.rollback_procedure.expected_sql_action_after}` })
          : document.createTextNode(""),
      ])
    );
  }

  if (plan.state === "succeeded" && plan.intent === "patch_apply") {
    body.appendChild(
      el("p", { class: "next-step", style: "margin-bottom:14px" }, [
        el("a", { href: `#/plans/rollback-new/${encodeURIComponent(planId)}`, class: "back-link", text: "→ Create a rollback plan from this succeeded plan" }),
      ])
    );
  }

  const controls = el("section", { class: "card" });
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  controls.appendChild(errBox);

  // Live SoD feedback on the actor field: warn as soon as the typed actor is barred.
  const bindSod = (input, step) => {
    const note = helperText("", "error");
    note.style.display = "none";
    const update = () => {
      const msg = sodCheck(plan, step, input.value);
      note.textContent = msg || "";
      note.style.display = msg ? "block" : "none";
      input.classList.toggle("is-invalid", Boolean(msg));
    };
    input.addEventListener("input", update);
    update();
    return note;
  };
  const guardSod = (step, who) => {
    const msg = sodCheck(plan, step, who);
    if (msg) showFormError(errBox, msg);
    return !msg;
  };

  if (dead) {
    // opu-patch-plan would refuse every step; do not offer buttons that can only fail.
    controls.appendChild(el("h2", { text: "No actions available" }));
    controls.appendChild(
      helperText("This sealed plan can no longer be approved, authorized or dispatched. Re-run the readiness pipeline and create a new plan.", "warn")
    );
  } else if (plan.state === "awaiting_approval") {
    controls.appendChild(el("h2", { text: "Approve" }));
    controls.appendChild(helperText("Actor must differ from the requester (SoD). Synced with Acting as."));
    const hint = sodHint(plan, "approve");
    if (hint) controls.appendChild(helperText(hint));
    await appendItsmBanner(controls);
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const sodNote = bindSod(actor, "approve");
    const ticket = el("input", { type: "text", placeholder: "approval ticket / change #" });
    const btn = el("button", { type: "button", text: "Approve" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who || !guardSod("approve", who)) return;
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/approve`, { actor: who, approval_ticket: ticket.value }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), field("Ticket", ticket), btn]));
    controls.appendChild(sodNote);
  } else if (plan.state === "approved") {
    controls.appendChild(el("h2", { text: "Authorize" }));
    controls.appendChild(helperText("Authorize only succeeds while the declared maintenance window is open.", "warn"));
    const hint = sodHint(plan, "authorize");
    if (hint) controls.appendChild(helperText(hint));
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const sodNote = bindSod(actor, "authorize");
    const btn = el("button", { type: "button", text: "Authorize" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who || !guardSod("authorize", who)) return;
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/authorize`, { actor: who }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), btn]));
    controls.appendChild(sodNote);
  } else if (plan.state === "execution_authorized") {
    controls.appendChild(el("h2", { text: "Dispatch" }));
    controls.appendChild(
      helperText(
        "Materializes the immutable task list and starts the plan. Live task execution still goes through Execute next / remaining after dispatch.",
        "warn"
      )
    );
    const hint = sodHint(plan, "dispatch");
    if (hint) controls.appendChild(helperText(hint));
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const sodNote = bindSod(actor, "dispatch");
    const btn = el("button", { type: "button", text: "Dispatch" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who || !guardSod("dispatch", who)) return;
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/dispatch`, { actor: who }, refresh, errBox);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), btn]));
    controls.appendChild(sodNote);
  } else if (["running", "paused", "succeeded"].includes(plan.state)) {
    controls.appendChild(el("h2", { text: "Tasks" }));
    if (plan.state === "running") {
      const windowState = executionWindow(plan);
      const actor = el("input", { type: "text", value: getActor() });
      bindActorField(actor);
      const btn = el("button", { type: "button", text: "Execute next task" });
      const btnAll = el("button", { type: "button", text: "Execute remaining tasks" });
      btn.disabled = unresolved || !windowState.open;
      btnAll.disabled = unresolved || !windowState.open;
      controls.appendChild(
        helperText(
          "TEST_MODE fixtures run locally. Live plans sync sealed state to the task node over SSH and pull evidence back. Execute remaining runs until idle, success, or a failed/blocked task.",
          "warn"
        )
      );
      btn.addEventListener("click", async () => {
        if (unresolved) return;
        clearFormError(errBox);
        const currentWindow = executionWindow(plan);
        if (!currentWindow.open) { showFormError(errBox, currentWindow.detail); return; }
        if (!requireToken(errBox)) return;
        const who = requireActor(actor, errBox, "Actor");
        if (!who) return;
        await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/execute-next`, { actor: who }, refresh, errBox);
      });
      btnAll.addEventListener("click", async () => {
        if (unresolved) return;
        clearFormError(errBox);
        const currentWindow = executionWindow(plan);
        if (!currentWindow.open) { showFormError(errBox, currentWindow.detail); return; }
        if (!requireToken(errBox)) return;
        const who = requireActor(actor, errBox, "Actor");
        if (!who) return;
        await runAction(logBox, btnAll, `/api/plans/${encodeURIComponent(planId)}/execute-remaining`, { actor: who }, refresh, errBox);
      });
      controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor, "Synced with Acting as"), btn, btnAll]));
    }
    controls.appendChild(await taskTable(planId, plan, unresolved));
  } else {
    controls.appendChild(el("h2", { text: "No actions available for this state" }));
    controls.appendChild(helperText(stateGuidance(plan)));
  }

  controls.appendChild(logBox);
  body.appendChild(controls);
  body.appendChild(executionConsole(planId));
  body.appendChild(evidenceReport(planId));

  body.appendChild(
    el("details", { class: "pipeline-result" }, [el("summary", { text: "Plan JSON" }), el("pre", { class: "mono", text: JSON.stringify(plan, null, 2) })])
  );
}

async function taskTable(planId, plan, unresolved) {
  const res = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/tasks`);
  const data = await res.json();
  if (!data.tasks?.length) {
    return helperText("No tasks materialized yet.");
  }
  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "Task" }), el("th", { text: "Stage" }), el("th", { text: "Node" }), el("th", { text: "Status" })])]),
    el(
      "tbody",
      {},
      data.tasks.map((t) =>
        el("tr", {}, [
          el("td", { text: t.task_id || "—" }),
          el("td", { text: t.stage || "—" }),
          el("td", { text: t.node || t.target?.node || "—" }),
          el("td", {}, [badge(t.status, classifyStatus(t.status))]),
        ])
      )
    ),
  ]);
  const inspection = extjobInspection(planId, plan, data.tasks, unresolved);
  return inspection ? el("div", {}, [table, inspection]) : table;
}

async function appendItsmBanner(controls) {
  try {
    const res = await apiFetch("/api/itsm/tickets");
    if (!res.ok) return;
    const data = await res.json();
    if (!data.enabled) return;
    const approved = (data.tickets || []).filter((t) => t.state === "approved").map((t) => t.ticket);
    controls.appendChild(
      helperText(
        `ITSM enforcement is ON — ticket must be an approved change. Approved: ${approved.length ? approved.join(", ") : "none"}.`,
        "warn"
      )
    );
  } catch {
    // Banner is informational only; approval is still gated server-side.
  }
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
      if (errBox) showFormError(errBox, record.error?.message || "Action failed");
      // Re-fetch so the state badge/tasks reflect pause/fail, keeping the error visible.
      await refresh({ message: record.error?.message || "Action failed", detail: formatRunFailure(record) });
    } else {
      logBox.textContent = "succeeded";
      if (errBox) clearFormError(errBox);
      await refresh();
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
