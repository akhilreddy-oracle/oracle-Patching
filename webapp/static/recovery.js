import { el, badge, classifyStatus, renderErrorBox } from "./dom.js";
import { runToCompletion, RunStartError } from "./runs.js";
import { getActor } from "./actor.js";

function field(labelText, inputEl) {
  return el("label", { class: "form-field" }, [el("span", { text: labelText }), inputEl]);
}

export async function renderRecoveryList(mount) {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { text: "Recovery Preparations" }));
  mount.appendChild(
    el("section", { class: "card" }, [
      el("h2", { text: "TEST_MODE recovery demo" }),
      el("p", { class: "estate-card-meta" }, [
        document.createTextNode(
          "Builds a fake standalone Oracle home and runs a real RMAN backup (create → analyze → approve → authorize → execute) through the real, unmodified opu-database-recovery-prepare binary, producing a real recovery-evidence.json usable as opu-patch-plan create --recovery-evidence. "
        ),
        el("a", { href: "#/recovery/new", text: "Create one →" }),
      ]),
    ])
  );

  const res = await fetch("/api/recovery");
  const data = await res.json();
  if (!data.requests.length) {
    mount.appendChild(el("p", { class: "empty-state", text: "No recovery preparations yet." }));
    return;
  }

  const table = el("table", {}, [
    el("thead", {}, [el("tr", {}, [el("th", { text: "Request ID" }), el("th", { text: "State" }), el("th", { text: "Requester" })])]),
    el(
      "tbody",
      {},
      data.requests.map((r) =>
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

export async function renderRecoveryNew(mount) {
  mount.innerHTML = "";
  mount.appendChild(el("h2", { text: "New TEST_MODE recovery demo" }));

  const requestId = el("input", { type: "text", value: `recovery-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  const logBox = el("pre", { class: "run-log", style: "display:none" });

  const form = el("div", { class: "pipeline-form" }, [
    el("div", { class: "form-grid" }, [field("Request ID", requestId), field("Requester (actor)", requester)]),
  ]);

  const btn = el("button", { type: "button", text: "Build fixture and create request" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    logBox.style.display = "block";
    logBox.textContent = "building fixture…";
    try {
      const record = await runToCompletion("/api/recovery/testmode-demo", { request_id: requestId.value, requester: requester.value });
      if (record.status === "failed") {
        logBox.textContent = `FAILED: ${record.error?.message || "unknown error"}\n${record.error?.stderr || ""}`;
      } else {
        location.hash = `#/recovery/${encodeURIComponent(requestId.value)}`;
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

export async function renderRecoveryDetail(mount, requestId) {
  mount.innerHTML = "";
  const header = el("div", { class: "host-header" }, [
    el("a", { class: "back-link", href: "#/recovery", text: "← Recovery" }),
    el("h2", { text: requestId }),
  ]);
  mount.appendChild(header);

  const body = el("div");
  mount.appendChild(body);

  async function refresh() {
    body.innerHTML = "";
    const res = await fetch(`/api/recovery/${encodeURIComponent(requestId)}`);
    const data = await res.json();
    if (!res.ok) {
      renderErrorBox(body, data);
      return;
    }
    renderRequest(body, requestId, data, refresh);
  }

  await refresh();
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

  const controls = el("section", { class: "card" });
  const logBox = el("pre", { class: "run-log", style: "display:none" });

  if (req.state === "awaiting_approval") {
    controls.appendChild(el("h2", { text: "Approve" }));
    const actor = el("input", { type: "text", value: getActor() });
    const ticket = el("input", { type: "text", placeholder: "approval ticket / change #" });
    const btn = el("button", { type: "button", text: "Approve" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/approve`, { actor: actor.value, approval_ticket: ticket.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), field("Ticket", ticket), btn]));
  } else if (req.state === "approved") {
    controls.appendChild(el("h2", { text: "Authorize" }));
    controls.appendChild(el("p", { class: "estate-card-error", text: "Authorize only succeeds while the declared window is open." }));
    const actor = el("input", { type: "text", value: getActor() });
    const btn = el("button", { type: "button", text: "Authorize" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/authorize`, { actor: actor.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
  } else if (req.state === "authorized") {
    controls.appendChild(el("h2", { text: "Execute" }));
    controls.appendChild(
      el("p", { class: "estate-card-error", text: "Real work: stops the (fake) listener, shuts down and remounts the (fake) database, runs a real RMAN backup, reopens services. Execute's actor must equal the authorizer." })
    );
    const actor = el("input", { type: "text", value: getActor() });
    const btn = el("button", { type: "button", text: "Execute" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/execute`, { actor: actor.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
  } else if (req.state === "completed") {
    const evidencePath = req.result?.recovery_evidence?.path;
    controls.appendChild(el("h2", { text: "Completed" }));
    controls.appendChild(
      el("p", { class: "estate-card-meta" }, [
        document.createTextNode("recovery-evidence.json: "),
        el("code", { class: "mono", text: evidencePath || "—" }),
      ])
    );
    controls.appendChild(
      el("p", { class: "estate-card-meta", text: "This exact file is a valid --recovery-evidence input for opu-patch-plan create." })
    );
  } else if (["failed_services_restored", "validation_failed", "recovery_required"].includes(req.state)) {
    controls.appendChild(el("h2", { text: "Failed — services restoration attempted automatically" }));
    controls.appendChild(el("p", { class: "estate-card-error", text: `Failure phase: ${req.failure?.phase || "unknown"}. Services restored: ${req.failure?.services_restored}` }));
    const actor = el("input", { type: "text", value: getActor() });
    const btn = el("button", { type: "button", text: "Reconcile" });
    btn.addEventListener("click", async () => {
      await runAction(logBox, btn, `/api/recovery/${encodeURIComponent(requestId)}/reconcile`, { actor: actor.value }, refresh);
    });
    controls.appendChild(el("div", { class: "pipeline-controls" }, [field("Actor", actor), btn]));
  } else {
    controls.appendChild(el("h2", { text: "No actions available for this state" }));
  }

  controls.appendChild(logBox);
  body.appendChild(controls);

  body.appendChild(
    el("details", { class: "pipeline-result" }, [el("summary", { text: "Request JSON" }), el("pre", { class: "mono", text: JSON.stringify(req, null, 2) })])
  );
}

async function runAction(logBox, btn, url, payload, refresh) {
  btn.disabled = true;
  logBox.style.display = "block";
  logBox.textContent = "working…";
  try {
    const record = await runToCompletion(url, payload);
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
