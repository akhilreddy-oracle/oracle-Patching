export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

export function badge(value, kind) {
  const kinds = { ok: "badge-ok", warn: "badge-warn", bad: "badge-bad", neutral: "badge-neutral" };
  return el("span", { class: `badge ${kinds[kind] || kinds.neutral}`, text: value ?? "unknown" });
}

export function classifyStatus(value) {
  if (value === null || value === undefined) return "neutral";
  const v = String(value).toLowerCase();
  if (["active", "healthy", "normal", "passed", "running", "success", "complete", "detected", "succeeded", "ok"].some((s) => v.includes(s))) return "ok";
  if (["failed", "blocked", "down", "not running", "error", "paused"].some((s) => v.includes(s))) return "bad";
  if (["unavailable", "unknown", "partial", "awaiting", "pending"].some((s) => v.includes(s))) return "warn";
  return "neutral";
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
    el("div", { class: "error-box" }, [
      el("h2", { text: "Request failed" }),
      el("p", { class: "error-message", text: data.message || "Unknown error" }),
      el("pre", { class: "error-detail", text: data.stderr || "" }),
    ])
  );
}
