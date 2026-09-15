import { apiFetch } from "./api.js";
import { el, badge, classifyStatus } from "./dom.js";

export async function renderApprovals(mount) {
  mount.appendChild(el("h1", { text: "Approval inbox" }));
  mount.appendChild(el("p", { class: "helper-text", text: "Review the target, recovery evidence, maintenance window and separation of duties before approving. Approval and execution authorization remain separate actions." }));
  const response = await apiFetch("/api/approvals");
  const { items } = await response.json();
  if (!items.length) mount.appendChild(el("p", { class: "empty-state", text: "No requests awaiting approval or execution authorization." }));
  for (const item of items) {
    const route = item.kind === "plan" ? "plans" : "recovery";
    const target = item.target || {};
    mount.appendChild(el("article", { class: "card approval-card" }, [
      el("h2", { text: item.id }), badge(item.state, classifyStatus(item.state)),
      el("p", { text: `${item.kind === "plan" ? "Patch plan" : "Backup preparation"} · ${target.database_unique_name || item.host_id || "Target in sealed request"}` }),
      el("p", { text: `Requested by ${item.requester || "unknown"}${item.self_requested ? " · An independent approver is required." : ""}` }),
      ...(item.window ? [el("p", { class: "helper-text", text: `Maintenance window: ${item.window.start || "unknown"} → ${item.window.end || "unknown"}` })] : []),
      el("a", { class: "btn", href: `#/${route}/${encodeURIComponent(item.id)}`, text: "Review request" }),
    ]));
  }
}
