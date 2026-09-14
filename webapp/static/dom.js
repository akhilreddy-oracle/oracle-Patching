export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "value" && tag === "textarea") node.value = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

export function badge(value, kind) {
  const kinds = { ok: "badge-ok", warn: "badge-warn", bad: "badge-bad", neutral: "badge-neutral" };
  return el("span", { class: `badge ${kinds[kind] || kinds.neutral}`, text: value ?? "unknown" });
}

const STATUS_KINDS = new Map([
  ...["active", "healthy", "normal", "passed", "pass", "running", "success", "complete", "completed",
    "collected", "consistent", "detected", "succeeded", "ok", "ready_for_catalog", "ready_for_planning",
    "ready_for_approval", "not_applicable", "no crs", "open"].map((status) => [status, "ok"]),
  ...["failed", "fail", "blocked", "blocker", "down", "not running", "not_running", "error", "paused",
    "inactive", "unhealthy", "abnormal", "validation_failed", "failed_services_restored",
    "recovery_required", "unreadable"].map((status) => [status, "bad"]),
  ...["unavailable", "unknown", "partial", "partially_collected", "incomplete", "awaiting", "pending",
    "awaiting_approval", "approved", "authorized", "execution_authorized", "mounted", "started"].map((status) => [status, "warn"]),
]);

/** Classify the status enum, never a composed label or success substring. */
export function classifyStatus(value) {
  return STATUS_KINDS.get(String(value ?? "").trim().toLowerCase()) || "neutral";
}

export function row(label, valueNode) {
  return el("tr", {}, [el("th", { text: label }), el("td", {}, [valueNode])]);
}
export function th(text) {
  return el("th", { text });
}
export function td(text, cls) {
  return el("td", { class: cls || "", text: text ?? "—" });
}
export function tdBadge(value, kind) {
  return el("td", {}, [badge(value, kind)]);
}

export function renderErrorBox(mount, data) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "error-box", role: "alert" }, [
      el("h2", { text: "Request failed" }),
      el("p", { class: "error-message", text: data.message || "Unknown error" }),
      el("pre", { class: "error-detail", text: data.stderr || "" }),
    ])
  );
}
