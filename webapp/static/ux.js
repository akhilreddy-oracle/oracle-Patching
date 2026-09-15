import { el } from "./dom.js";
import { getActor, setActor, onActorChange, authenticatedActor, companySessionState } from "./actor.js";
import { getApiToken } from "./api.js";
import { focusSessionField } from "./shell.js";

/** Bind an actor/requester input to the session "Acting as" value (two-way). */
export function bindActorField(input) {
  input.value = getActor();
  input.readOnly = Boolean(authenticatedActor());
  input.placeholder = input.placeholder || "synced with session Acting as";
  input.setAttribute("data-actor-bound", "1");
  input.addEventListener("input", () => {
    input.classList.remove("is-invalid");
    setActor(input.value);
  });
  const unsubscribe = onActorChange((value) => {
    if (!input.isConnected) { unsubscribe(); return; }
    input.readOnly = Boolean(authenticatedActor());
    if (document.activeElement !== input || input.readOnly) input.value = value;
    if (value.trim()) input.classList.remove("is-invalid");
  });
  return unsubscribe;
}

export function field(labelText, inputEl, hintText) {
  const kids = [el("span", { text: labelText }), inputEl];
  if (hintText) kids.push(el("span", { class: "helper-text", text: hintText }));
  return el("label", { class: "form-field" }, kids);
}

export function helperText(text, kind) {
  const cls = kind === "warn" ? "helper-text helper-warn" : kind === "error" ? "helper-text helper-error" : "helper-text";
  return el("p", { class: cls, text });
}

/**
 * Persistent failure card for detail pages that re-render on refresh: the
 * page re-fetches state (so badges are fresh) while the last action's error
 * survives the re-render.
 */
export function failureCard(failure) {
  if (!failure) return null;
  const pre = el("pre", { class: "run-log run-log-error", text: failure.detail || failure.message || "Action failed" });
  return el("section", { class: "card card-failure", role: "alert" }, [
    el("h2", { text: failure.title || "Last action failed" }),
    failure.message && failure.detail ? el("p", { class: "form-error", style: "display:block", text: failure.message }) : document.createTextNode(""),
    pre,
  ]);
}

export function formErrorBox() {
  const box = el("p", { class: "form-error", role: "alert", style: "display:none" });
  return box;
}

export function showFormError(box, message) {
  if (!box) return;
  box.textContent = message;
  box.style.display = "block";
}

export function clearFormError(box) {
  if (!box) return;
  box.textContent = "";
  box.style.display = "none";
}

/** Returns trimmed actor or null after surfacing an error. */
export function requireActor(input, errBox, label = "Actor") {
  const value = (input?.value ?? "").trim();
  if (!value) {
    showFormError(
      errBox,
      `${label} is required. Set Acting as in the session panel, or fill this field (they stay in sync).`
    );
    input?.classList.add("is-invalid");
    focusSessionField("actor");
    return null;
  }
  input?.classList.remove("is-invalid");
  setActor(value);
  clearFormError(errBox);
  return value;
}

export function requireToken(errBox) {
  const companySession = companySessionState();
  if (companySession === "active") return true;
  if (companySession !== "absent") {
    showFormError(errBox, "Company session expired or unavailable. Sign in again before continuing.");
    return false;
  }
  if (!getApiToken()) {
    showFormError(errBox, "Sign in with your company account, or enter an API token in the session panel.");
    focusSessionField("token");
    return false;
  }
  return true;
}

export function requireNonEmpty(input, errBox, label) {
  const value = (input?.value ?? "").trim();
  if (!value) {
    showFormError(errBox, `${label} is required.`);
    input?.focus();
    return null;
  }
  clearFormError(errBox);
  return value;
}

export function isAbsolutePath(value) {
  return /^\//.test(String(value || "").trim());
}

export function pageIntro(kicker, title, lead) {
  return el("header", { class: "page-intro" }, [
    el("p", { class: "page-kicker", text: kicker }),
    el("h1", { class: "page-title", text: title }),
    el("p", { class: "page-lead", text: lead }),
  ]);
}

export function emptyWithCta(message, href, ctaLabel) {
  const kids = [el("p", { text: message })];
  if (href && ctaLabel) {
    kids.push(el("p", { class: "empty-cta-row" }, [el("a", { class: "empty-cta", href, text: ctaLabel })]));
  }
  return el("div", { class: "empty-state" }, kids);
}

export function nextStepBanner(text, href, linkLabel) {
  const kids = [el("span", { text: text })];
  if (href && linkLabel) {
    kids.push(document.createTextNode(" "));
    kids.push(el("a", { class: "back-link", href, text: linkLabel }));
  }
  return el("div", { class: "next-step" }, kids);
}

export function explainStatus(status, { done = false } = {}) {
  if (!done) {
    return { kind: "neutral", text: "Not run — no cached evidence for this step yet." };
  }
  const s = String(status || "").toLowerCase();
  if (s === "blocked") {
    return {
      kind: "warn",
      text: "Blocked is a completed safety result (not a crash). Cached evidence may be stale if you changed earlier steps — fix findings, then re-run this step.",
    };
  }
  if (s.includes("fail") || s === "error") {
    return { kind: "error", text: "Failed — open the run log or evidence JSON for the error detail." };
  }
  if (
    s === "ready_for_approval" ||
    s === "ready_for_catalog" ||
    s === "ready_for_planning" ||
    s === "passed" ||
    s === "pass" ||
    s === "consistent" ||
    s === "ok" ||
    s === "succeeded"
  ) {
    return { kind: "ok", text: `Completed with status “${status}”.` };
  }
  if (s === "partial") {
    return {
      kind: "warn",
      text: "Partial — some discovery phases are incomplete. Open the phase list before reconcile.",
    };
  }
  if (s === "not_applicable") {
    return {
      kind: "ok",
      text: "Not applicable for this topology (for example no Clusterware on a standalone host).",
    };
  }
  if (s === "unavailable") {
    return {
      kind: "warn",
      text: "Unavailable means that probe could not report — for cluster this may be CRS down/permission; for databases it means runtime evidence failed. Check the phase that is unavailable.",
    };
  }
  return { kind: "neutral", text: `Cached evidence present (status: ${status || "unknown"}).` };
}

export function formatRunFailure(record) {
  const msg = record?.error?.message || "unknown error";
  const stderr = record?.error?.stderr || "";
  const lines = [`FAILED: ${msg}`];
  if (stderr.trim()) {
    const tail = stderr.trim().split("\n").slice(-12).join("\n");
    lines.push("", "— last stderr —", tail);
  }
  return lines.join("\n");
}

export function summarizeBlockedEvidence(evidence) {
  if (!evidence || typeof evidence !== "object") return null;
  const bits = [];
  if (Array.isArray(evidence.findings) && evidence.findings.length) {
    for (const f of evidence.findings.slice(0, 5)) {
      bits.push(typeof f === "string" ? f : f.message || f.code || JSON.stringify(f));
    }
  }
  const artifactReason = evidence.artifact?.reason;
  if (artifactReason && evidence.artifact?.status && evidence.artifact.status !== "ready_for_catalog") {
    bits.push(String(artifactReason));
  }
  if (Array.isArray(evidence.gates)) {
    for (const g of evidence.gates.filter((x) => x.status === "blocker" || x.status === "blocked").slice(0, 5)) {
      bits.push(`${g.name || "gate"}: ${g.status}${g.detail ? ` — ${g.detail}` : ""}`);
    }
  }
  if (Array.isArray(evidence.checks)) {
    // Collector findings already narrate each failed prerequisite; avoid
    // duplicating them and only add per-home lines with OPatch's own detail.
    const hasFindings = Array.isArray(evidence.findings) && evidence.findings.length > 0;
    for (const c of evidence.checks.filter((x) => x.status === "blocked").slice(0, 5)) {
      const home = c.home || c.node || "home";
      const reasons = [];
      if (c.opatch?.status && c.opatch.status !== "passed") {
        reasons.push(`opatch ${c.opatch.status}${c.opatch.actual_version ? ` (${c.opatch.actual_version} < ${c.opatch.required_version})` : ""}`);
      }
      if (c.platform?.status && c.platform.status !== "passed") reasons.push(`platform ${c.platform.status}`);
      const prereqLine = (label, check) => {
        if (!check?.status || check.status === "passed") return;
        const detail = !hasFindings && check.detail ? ` — ${check.detail}` : "";
        reasons.push(`${label} ${check.status}${check.exit_code != null ? ` (exit ${check.exit_code})` : ""}${detail}`);
      };
      prereqLine("applicability", c.applicability_check);
      prereqLine("conflict", c.conflict_check);
      if (!hasFindings || reasons.some((r) => r.startsWith("opatch") || r.startsWith("platform"))) {
        bits.push(`${c.node ? `${c.node}: ` : ""}${home}: ${reasons.join("; ") || "blocked"}`);
      }
    }
  }
  if (!bits.length) return null;
  return bits;
}

/**
 * Classify a blocked artifact/compatibility result into a remediation the UI
 * can offer directly (no host login). Returns null when nothing is actionable.
 */
export function mediaRemediation(evidence) {
  if (!evidence || typeof evidence !== "object") return null;
  const text = [
    ...(Array.isArray(evidence.findings) ? evidence.findings : []),
    evidence.artifact?.reason,
    ...(Array.isArray(evidence.checks)
      ? evidence.checks.flatMap((c) => [c.applicability_check?.detail, c.conflict_check?.detail])
      : []),
  ]
    .filter(Boolean)
    .map(String)
    .join("\n");
  if (/missing files\/ payload|artifact is incomplete|one-level down|Unable to create Patch Object|No patch location specified/i.test(text)) {
    return {
      kind: "incomplete_media",
      title: "Staged patch media is incomplete on this host",
      text:
        "The directory has OPatch metadata (etc/config, README) but no files/ payload, so OPatch cannot build a Patch Object. " +
        "Stage the complete media below — copy it from another managed host that already has it, or unpack a patch zip already on this host — then re-run the steps from Artifact inspection.",
    };
  }
  if (/not readable by Oracle Home owner|Permission denied/i.test(text)) {
    return {
      kind: "permissions",
      title: "Oracle Home owner cannot read the staged media",
      text: "Re-stage the media below so ownership and permissions are set for the Oracle Home owner.",
    };
  }
  return null;
}
