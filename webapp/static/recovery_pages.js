import { el, badge, classifyStatus, renderErrorBox } from "./dom.js";
import { apiFetch } from "./api.js";
import { newestFirst } from "./host_scope.js";
import { runToCompletion, RunStartError } from "./runs.js";
import { getActor } from "./actor.js";
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
  if (!data.requests?.length) {
    mount.appendChild(
      el("p", {
        class: "empty-state",
        text: "No recovery preparations yet. Use the lab demo route below only for fixture-based drills — set Acting as and API token first.",
      })
    );
  } else {
    const table = el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Request ID" }), el("th", { text: "State" }), el("th", { text: "Requester" })])]),
      el(
        "tbody",
        {},
        newestFirst(data.requests).map((r) =>
          el("tr", {}, [
            el("td", {}, [el("a", { href: `#/recovery/${encodeURIComponent(r.request_id)}`, text: r.request_id })]),
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
    if (!requireNonEmpty(requestId, errBox, "Request ID")) return;
    const actor = requireActor(requester, errBox, "Requester");
    if (!actor) return;
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "building fixture…";
    try {
      const record = await runToCompletion("/api/recovery/testmode-demo", {
        request_id: requestId.value.trim(),
        requester: actor,
      });
      if (record.status === "failed") {
        logBox.classList.add("run-log-error");
        logBox.textContent = formatRunFailure(record);
      } else {
        location.hash = `#/recovery/${encodeURIComponent(requestId.value.trim())}`;
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
    renderRequest(body, requestId, data, refresh);
  }

  await refresh();
}

function stateGuidance(req) {
  const map = {
    awaiting_approval: "Next: approve with a different actor than the requester (SoD).",
    approved: "Next: authorize while the declared window is open.",
    authorized: "Next: execute. Execute’s actor must equal the authorizer.",
    completed: "Done — use the recovery-evidence.json path when creating a patch plan.",
    failed_services_restored: "Failed after services restore attempt — reconcile, then inspect failure phase.",
    validation_failed: "Validation failed — reconcile and inspect failure details.",
    recovery_required: "Recovery required — reconcile after reviewing failure phase.",
  };
  return map[req.state] || `State: ${req.state}.`;
}

function renderRequest(body, requestId, req, refresh) {
  body.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("State: "), badge(req.state, classifyStatus(req.state))]),
      el("span", {}, [document.createTextNode("Requester: "), el("strong", { text: req.requester || "—" })]),
      el("span", {}, [document.createTextNode("Window: "), el("strong", { text: `${req.maintenance_window?.start || "?"} → ${req.maintenance_window?.end || "?"}` })]),
      el("span", {}, [document.createTextNode("Backup root: "), el("code", { class: "mono", text: req.backup?.root || "—" })]),
    ])
  );
  body.appendChild(nextStepBanner(stateGuidance(req)));

  const controls = el("section", { class: "card" });
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  controls.appendChild(errBox);

  if (req.state === "awaiting_approval") {
    controls.appendChild(el("h2", { text: "Approve" }));
    controls.appendChild(helperText("Actor must differ from the requester (SoD). Synced with Acting as."));
    const actor = el("input", { type: "text", value: getActor() });
    bindActorField(actor);
    const ticket = el("input", { type: "text", placeholder: "approval ticket / change #" });
    const btn = el("button", { type: "button", text: "Approve" });
    btn.addEventListener("click", async () => {
      clearFormError(errBox);
      if (!requireToken(errBox)) return;
      const who = requireActor(actor, errBox, "Actor");
      if (!who) return;
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
        "Stops the (fake) listener, shuts down/remounts the (fake) database, runs a real RMAN backup, reopens services. Execute’s actor must equal the authorizer.",
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
      helperText("This exact file is a valid --recovery-evidence input for opu-patch-plan create.")
    );
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

async function runAction(logBox, btn, url, payload, refresh, errBox) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.classList.remove("run-log-error");
  logBox.textContent = "working…";
  try {
    const record = await runToCompletion(url, payload);
    if (record.status === "failed") {
      logBox.classList.add("run-log-error");
      logBox.textContent = formatRunFailure(record);
      if (errBox) showFormError(errBox, record.error?.message || "Action failed");
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
      await refresh({ message: msg });
    } catch (_refreshErr) {
      // Keep the inline error if the refresh itself fails.
    }
  } finally {
    btn.disabled = false;
  }
}
