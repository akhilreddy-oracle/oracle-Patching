import { el, badge, classifyStatus, renderErrorBox } from "./dom.js";
import { runToCompletion, RunStartError } from "./runs.js";
import { getActor } from "./actor.js";

export async function renderPlanList(mount) {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { text: "Plans" }));

  const res = await fetch("/api/plans");
  const data = await res.json();

  mount.appendChild(
    el("section", { class: "card" }, [
      el("h2", { text: "TEST_MODE demo plan" }),
      el("p", { class: "estate-card-meta" }, [
        document.createTextNode(
          "Builds a self-contained fake Oracle home and drives a real standalone-database apply plan (precheck → apply → validate → datapatch → final_validate) through the real, unmodified executor binary. No real host is touched. "
        ),
        el("a", { href: "#/plans/demo-new", text: "Create one →" }),
      ]),
    ])
  );

  if (!data.plans.length) {
    mount.appendChild(el("p", { class: "empty-state", text: "No plans yet. Create one from a host's readiness pipeline once it reaches ready_for_approval, or use the TEST_MODE demo above." }));
    return;
  }

  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "Plan ID" }), el("th", { text: "State" }), el("th", { text: "Patch" }), el("th", { text: "Requester" })])]),
    el(
      "tbody",
      {},
      data.plans.map((p) =>
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

export async function renderPlanNew(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { text: `New plan — ${hostId}` }));

  const planId = el("input", { type: "text", value: `${hostId}-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60000);
  const end = new Date(now.getTime() + 4 * 3600000);
  const windowStart = el("input", { type: "text", value: start.toISOString().replace(/\.\d+Z$/, "Z") });
  const windowEnd = el("input", { type: "text", value: end.toISOString().replace(/\.\d+Z$/, "Z") });

  const logBox = el("pre", { class: "run-log", style: "display:none" });

  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Plan ID", planId),
      field("Requester (actor)", requester),
      field("Window start (UTC)", windowStart),
      field("Window end (UTC)", windowEnd),
    ]),
  ]);

  const btn = el("button", { type: "button", text: "Create plan" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.textContent = "creating…";
    try {
      const record = await runToCompletion("/api/plans", {
        plan_id: planId.value,
        requester: requester.value,
        host_id: hostId,
        window_start: windowStart.value,
        window_end: windowEnd.value,
      });
      if (record.status === "failed") {
        logBox.textContent = `FAILED: ${record.error?.message || "unknown error"}\n${record.error?.stderr || ""}`;
      } else {
        location.hash = `#/plans/${encodeURIComponent(planId.value)}`;
      }
    } catch (err) {
      logBox.textContent = err instanceof RunStartError ? err.message : String(err);
    } finally {
      btn.disabled = false;
    }
  });
  form.appendChild(btn);
  form.appendChild(logBox);

  mount.appendChild(el("section", { class: "card" }, [form]));
}

export async function renderPlanDemoNew(mount) {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { text: "New TEST_MODE demo plan" }));
  mount.appendChild(
    el("p", { class: "estate-card-meta", text: "Fresh fake Oracle home + patch directory built on the backend host, real opu-artifact-inspect run against it, then a real plan created from that evidence. Nothing here touches a configured estate host." })
  );

  const planId = el("input", { type: "text", value: `demo-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  const now = new Date();
  const start = new Date(now.getTime() - 5 * 60000);
  const end = new Date(now.getTime() + 4 * 3600000);
  const windowStart = el("input", { type: "text", value: start.toISOString().replace(/\.\d+Z$/, "Z") });
  const windowEnd = el("input", { type: "text", value: end.toISOString().replace(/\.\d+Z$/, "Z") });

  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [
      field("Plan ID", planId),
      field("Requester (actor)", requester),
      field("Window start (UTC)", windowStart),
      field("Window end (UTC)", windowEnd),
    ]),
  ]);

  const btn = el("button", { type: "button", text: "Build fixture and create plan" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.textContent = "building fixture…";
    try {
      const record = await runToCompletion("/api/plans/testmode-demo", {
        plan_id: planId.value,
        requester: requester.value,
        window_start: windowStart.value,
        window_end: windowEnd.value,
      });
      if (record.status === "failed") {
        logBox.textContent = `FAILED: ${record.error?.message || "unknown error"}\n${record.error?.stderr || ""}`;
      } else {
        location.hash = `#/plans/${encodeURIComponent(planId.value)}`;
      }
    } catch (err) {
      logBox.textContent = err instanceof RunStartError ? err.message : String(err);
    } finally {
      btn.disabled = false;
    }
  });
  form.appendChild(btn);
  form.appendChild(logBox);

  mount.appendChild(el("section", { class: "card" }, [form]));
}

function field(labelText, inputEl) {
  return el("label", { class: "form-field" }, [el("span", { text: labelText }), inputEl]);
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

  async function refresh() {
    body.innerHTML = "";
    const res = await fetch(`/api/plans/${encodeURIComponent(planId)}`);
    const data = await res.json();
    if (!res.ok) {
      renderErrorBox(body, data);
      return;
    }
    renderPlan(body, planId, data, refresh);
  }

  await refresh();
}

function renderPlan(body, planId, plan, refresh) {
  body.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("State: "), badge(plan.state, classifyStatus(plan.state))]),
      el("span", {}, [document.createTextNode("Patch: "), el("strong", { text: plan.patch_id || "—" })]),
      el("span", {}, [document.createTextNode("Requester: "), el("strong", { text: plan.requester || "—" })]),
      el("span", {}, [document.createTextNode("Window: "), el("strong", { text: `${plan.maintenance_window?.start || "?"} → ${plan.maintenance_window?.end || "?"}` })]),
    ])
  );

  const controls = el("section", { class: "card" });
  const logBox = el("pre", { class: "run-log", style: "display:none" });

  if (plan.state === "awaiting_approval") {
    controls.appendChild(el("h2", { text: "Approve" }));
    const actor = el("input", { type: "text", value: getActor() });
    const ticket = el("input", { type: "text", placeholder: "approval ticket / change #" });
    const btn = el("button", { type: "button", text: "Approve" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/approve`, { actor: actor.value, approval_ticket: ticket.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), field("Ticket", ticket), btn]));
  } else if (plan.state === "approved") {
    controls.appendChild(el("h2", { text: "Authorize" }));
    controls.appendChild(el("p", { class: "estate-card-error", text: "Authorize only succeeds while the declared maintenance window is open." }));
    const actor = el("input", { type: "text", value: getActor() });
    const btn = el("button", { type: "button", text: "Authorize" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/authorize`, { actor: actor.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
  } else if (plan.state === "execution_authorized") {
    controls.appendChild(el("h2", { text: "Dispatch" }));
    controls.appendChild(
      el("p", { class: "estate-card-error", text: "Materializes the immutable task list and starts the plan. Actual task execution (running the real executor binary) is not wired into this UI yet — see README note below." })
    );
    const actor = el("input", { type: "text", value: getActor() });
    const btn = el("button", { type: "button", text: "Dispatch" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/dispatch`, { actor: actor.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
  } else if (["running", "paused", "succeeded"].includes(plan.state)) {
    controls.appendChild(el("h2", { text: "Tasks" }));
    if (plan.state === "running") {
      const actor = el("input", { type: "text", value: getActor() });
      const btn = el("button", { type: "button", text: "Execute next task (TEST_MODE)" });
      btn.addEventListener("click", async () => {
        await runAction(logBox, btn, `/api/plans/${encodeURIComponent(planId)}/execute-next`, { actor: actor.value }, refresh);
      });
      controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
    }
    controls.appendChild(await taskTable(planId));
  } else {
    controls.appendChild(el("h2", { text: "No actions available for this state" }));
  }

  controls.appendChild(logBox);
  body.appendChild(controls);

  body.appendChild(
    el("details", { class: "pipeline-result" }, [el("summary", { text: "Plan JSON" }), el("pre", { class: "mono", text: JSON.stringify(plan, null, 2) })])
  );
}

async function taskTable(planId) {
  const res = await fetch(`/api/plans/${encodeURIComponent(planId)}/tasks`);
  const data = await res.json();
  return el("table", {}, [
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
}

async function runAction(logBox, btn, url, body, refresh) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.textContent = "working…";
  try {
    const record = await runToCompletion(url, body);
    if (record.status === "failed") {
      logBox.textContent = `FAILED: ${record.error?.message || "unknown error"}\n${record.error?.stderr || ""}`;
    } else {
      logBox.textContent = "succeeded";
      await refresh();
    }
  } catch (err) {
    logBox.textContent = err instanceof RunStartError ? err.message : String(err);
  } finally {
    btn.disabled = false;
  }
}
