import { el, badge, classifyStatus } from "../dom.js";
import { apiFetch } from "../api.js";
import { runToCompletion, RunStartError } from "../runs.js";
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
  requireNonEmpty,
  helperText,
  formatRunFailure,
} from "../ux.js";
import { backupPolicyChooser, getBackupPolicy, hydrateBackupPolicy, savedPolicyFromSteps } from "../backup_policy.js";

import { wizardContext, targetSummary, planReview, maintenanceWindowError } from "../patch_wizard.js";

export async function renderPlanStage(mount, hostId, { onEvidenceChanged, showTargetSummary = false } = {}) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "stage-head" }, [
      el("h2", { class: "stage-title", text: "Plan" }),
      el("p", {
        class: "stage-lead",
        text: "Seal a plan from readiness evidence, then approve and authorize with separation of duties.",
      }),
    ])
  );

  const pipeline = await apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/plan-preview`);
  const pipelineData = await pipeline.json();
  if (!pipeline.ok) throw new Error(pipelineData.message || "Could not load readiness evidence");
  const context = wizardContext(pipelineData.steps || [], hostId);
  onEvidenceChanged?.(pipelineData.steps || []);
  if (showTargetSummary) mount.appendChild(targetSummary(context));
  mount.appendChild(planReview(context));
  hydrateBackupPolicy(hostId, savedPolicyFromSteps(pipelineData.steps || []));
  mount.appendChild(backupPolicyChooser(hostId));
  mount.appendChild(helperText("Plan creation uses the saved readiness policy. Re-evaluate readiness after changing these draft controls."));
  if (getBackupPolicy(hostId) === "waive") {
    mount.appendChild(
      helperText(
        "Backup waived — plan create will omit --recovery-evidence when the sealed policy has require_backup=false.",
        "warn"
      )
    );
  }

  const createPanel = el("section", { class: "panel" });
  createPanel.appendChild(el("h3", { class: "panel-title", text: "Create sealed plan" }));
  mount.appendChild(createPanel);
  mountCreateForm(createPanel, hostId, context, pipelineData.confirmation, pipelineData.reason);

  const listPanel = el("section", { class: "panel" });
  listPanel.appendChild(el("h3", { class: "panel-title", text: "Plans for this host" }));
  mount.appendChild(listPanel);

  const res = await apiFetch("/api/plans");
  const data = await res.json();
  const plans = newestFirst(data.plans || []).filter((p) => planBelongsToHost(p, hostId));

  if (!plans.length) {
    listPanel.appendChild(helperText("No plans for this host yet. Create one after ready_for_approval."));
    return;
  }

  listPanel.appendChild(
    el("table", {}, [
      el("thead", {}, [
        el("tr", {}, [el("th", { text: "Plan" }), el("th", { text: "State" }), el("th", { text: "Patch" }), el("th", { text: "" })]),
      ]),
      el(
        "tbody",
        {},
        plans.map((p) =>
          el("tr", {}, [
            el("td", { class: "mono", text: p.plan_id }),
            el("td", {}, [badge(p.state, classifyStatus(p.state))]),
            el("td", { text: p.patch_id || "—" }),
            el("td", {}, [
              el("a", {
                class: "back-link",
                href: `#/plans/${encodeURIComponent(p.plan_id)}`,
                text: "Open",
              }),
              document.createTextNode(" · "),
              el("a", {
                class: "back-link",
                href: `#/hosts/${encodeURIComponent(hostId)}/execute`,
                text: "Execute",
              }),
            ]),
          ])
        )
      ),
    ])
  );
}

function mountCreateForm(panel, hostId, context, confirmation, reviewReason) {
  const planId = el("input", { type: "text", value: `${hostId}-${Date.now().toString(36)}` });
  const requester = el("input", { type: "text", value: getActor() });
  bindActorField(requester);
  const now = new Date();
  const windowStart = el("input", {
    type: "text",
    value: new Date(now.getTime() - 5 * 60000).toISOString().replace(/\.\d+Z$/, "Z"),
  });
  const windowEnd = el("input", {
    type: "text",
    value: new Date(now.getTime() + 4 * 3600000).toISOString().replace(/\.\d+Z$/, "Z"),
  });
  const errBox = formErrorBox();
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  const btn = el("button", { type: "button", text: "Create plan" });
  const reviewed = Boolean(context.bound && confirmation
    && /^[a-f0-9]{64}$/.test(confirmation.expected_creation_binding_sha256)
    && confirmation.patch_id === context.procedure?.patch_id
    && confirmation.database === (context.procedure?.target?.database_unique_name ?? null));
  btn.disabled = !reviewed;
  if (!reviewed) panel.appendChild(helperText(reviewReason || "Reload Plan to review the current evidence before creating a plan.", "warn"));

  btn.addEventListener("click", async () => {
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    if (!reviewed) { showFormError(errBox, reviewReason || "Reload Plan and review the current evidence before creating a plan."); return; }
    const windowError = maintenanceWindowError(windowStart.value.trim(), windowEnd.value.trim());
    if (windowError) { showFormError(errBox, windowError); return; }
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
        ...confirmation,
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

  panel.appendChild(
    el("div", { class: "pipeline-form" }, [
      el("div", { class: "form-grid" }, [
        field("Requester", requester, "Synced with session Acting as"),
        field("Window start (UTC)", windowStart),
        field("Window end (UTC)", windowEnd),
      ]),
      el("details", { class: "advanced-settings" }, [el("summary", { text: "Advanced settings" }), field("Plan ID", planId)]),
      errBox,
      btn,
      logBox,
    ])
  );
}
