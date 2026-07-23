import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch } from "./api.js";

export async function renderEstate(mount) {
  mount.innerHTML = "";
  mount.appendChild(el("p", { class: "empty-state", text: "Loading estate…" }));

  let data;
  try {
    const res = await apiFetch("/api/estate");
    data = await res.json();
  } catch (err) {
    mount.innerHTML = "";
    mount.appendChild(el("p", { class: "empty-state", text: `Failed to load estate: ${err}` }));
    return;
  }

  mount.innerHTML = "";

  if (!data.hosts || data.hosts.length === 0) {
    mount.appendChild(el("p", { class: "empty-state", text: "No hosts configured in webapp/hosts.json." }));
    return;
  }

  const grid = el("div", { class: "estate-grid" });
  for (const host of data.hosts) {
    grid.appendChild(hostCard(host));
  }
  mount.appendChild(grid);
}

function hostCard(host) {
  const link = el("a", { class: "estate-card", href: `#/hosts/${encodeURIComponent(host.id)}` });

  link.appendChild(
    el("div", { class: "estate-card-head" }, [
      el("h3", { text: host.label }),
      badge(host.status, host.status === "ok" ? "ok" : "bad"),
    ])
  );

  if (host.status === "error") {
    link.appendChild(el("p", { class: "estate-card-error", text: host.error?.message || "Discovery failed" }));
    return link;
  }

  const stats = el("div", { class: "estate-card-stats" }, [
    stat("Cluster", host.cluster_status, classifyStatus(host.cluster_status)),
    stat("Version", host.active_version || "—", "neutral"),
    stat("Nodes", String(host.node_count ?? 0), "neutral"),
    stat("Homes", String(host.oracle_home_count ?? 0), "neutral"),
    stat("Databases", String(host.database_count ?? 0), "neutral"),
    stat("Warnings", String(host.warning_count ?? 0), host.warning_count ? "warn" : "ok"),
  ]);
  link.appendChild(stats);
  link.appendChild(el("p", { class: "estate-card-meta", text: `Collected ${host.collected_at || "—"}` }));

  return link;
}

function stat(label, value, kind) {
  return el("div", { class: "estate-stat" }, [
    el("span", { class: "estate-stat-label", text: label }),
    badge(value, kind),
  ]);
}
