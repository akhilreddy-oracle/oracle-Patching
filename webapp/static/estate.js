import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch, getReadSignal } from "./api.js";
import { runToCompletion } from "./runs.js";
import { helperText } from "./ux.js";
import { refreshHostNav } from "./shell.js";
import { liveDiscoveryAccess } from "./actor.js";

import { renderFleet } from "./fleet.js";

export async function renderEstate(mount) {
  const signal = getReadSignal();
  mount.innerHTML = "";
  mount.appendChild(
    el("header", { class: "ws-header" }, [
      el("div", { class: "ws-header-text" }, [
        el("p", { class: "ws-kicker", text: "Estate" }),
        el("h1", { class: "ws-title", text: "Hosts" }),
        el("p", { class: "ws-sub", text: "Open a host workspace for live discovery and the patching lifecycle." }),
      ]),
    ])
  );

  const fleet = el("section", { class: "panel fleet-dashboard" });
  mount.appendChild(fleet);
  await renderFleet(fleet);
  if (signal?.aborted) return;

  const toolbar = el("div", { class: "toolbar" }, [
    el("button", { id: "estate-refresh-btn", type: "button", text: "Refresh live SSH" }),
    el("span", { id: "estate-refresh-status", class: "status-chip", role: "status", "aria-live": "polite", text: "" }),
  ]);
  const refreshBtn = toolbar.querySelector("#estate-refresh-btn");
  const permissionHint = helperText("", "warn");
  permissionHint.setAttribute("id", "estate-discovery-permission");
  refreshBtn.setAttribute("aria-describedby", "estate-discovery-permission");
  const updateAccess = () => {
    const access = liveDiscoveryAccess();
    refreshBtn.disabled = !access.allowed;
    permissionHint.textContent = access.reason;
    permissionHint.hidden = access.allowed;
    return access.allowed;
  };
  updateAccess();
  mount.appendChild(toolbar);
  mount.appendChild(permissionHint);
  const detail = el("p", { id: "estate-refresh-detail", class: "helper-text", text: "" });
  mount.appendChild(detail);

  const list = el("div", { class: "host-picker" });
  mount.appendChild(list);

  let data;
  try {
    const res = await apiFetch("/api/estate", { signal });
    data = await res.json();
    if (signal?.aborted) return;
    if (!res.ok) {
      list.appendChild(el("p", { class: "empty-state", text: data.message || "Failed to load estate" }));
      return;
    }
  } catch (err) {
    list.appendChild(el("p", { class: "empty-state", text: `Failed to load estate: ${err}` }));
    return;
  }

  if (!data.hosts?.length) {
    list.appendChild(
      helperText("No hosts configured. Add entries to webapp/hosts.json and restart the webapp.", "warn")
    );
    return;
  }

  paint(list, data.hosts);

  const refreshStatus = toolbar.querySelector("#estate-refresh-status");

  refreshBtn.addEventListener("click", async () => {
    if (signal?.aborted || !updateAccess()) return;
    refreshBtn.disabled = true;
    refreshStatus.textContent = "refreshing…";
    refreshStatus.className = "status-chip is-loading";
    detail.textContent = "";
    const outcomes = new Map();
    const ids = data.hosts.map((h) => h.id);
    let done = 0;

    await Promise.all(
      ids.map(async (hostId) => {
        try {
          const record = await runToCompletion(`/api/hosts/${encodeURIComponent(hostId)}/pipeline/discovery`, {}, { signal });
          outcomes.set(
            hostId,
            record.status === "failed"
              ? { status: "error", message: record.error?.message || "discovery failed" }
              : { status: "ok", message: "ok" }
          );
        } catch (err) {
          outcomes.set(hostId, { status: "error", message: err.message || String(err) });
        } finally {
          done += 1;
          refreshStatus.textContent = `${done}/${ids.length}`;
        }
      })
    );
    // Accepted discoveries continue independently, but their former page must
    // not issue new reads or reset the active host after navigation/sign-in.
    if (signal?.aborted) return;

    try {
      const res = await apiFetch("/api/estate", { signal });
      const fresh = await res.json();
      if (signal?.aborted) return;
      if (res.ok && fresh.hosts) {
        data = fresh;
        for (const host of data.hosts) {
          const o = outcomes.get(host.id);
          if (o?.status === "error" && host.status === "ok") {
            outcomes.set(host.id, { status: "used-cache", message: o.message });
          }
        }
        paint(list, data.hosts, outcomes);
        await renderFleet(fleet);
        if (signal?.aborted) return;
        await refreshHostNav(null);
      }
    } catch {
      if (signal?.aborted) return;
      paint(list, data.hosts, outcomes);
    }

    const failed = [...outcomes.values()].filter((o) => o.status !== "ok");
    if (!failed.length) {
      refreshStatus.textContent = `ok · ${ids.length}`;
      refreshStatus.className = "status-chip is-ok";
    } else {
      refreshStatus.textContent = `${failed.length} issue(s)`;
      refreshStatus.className = "status-chip is-bad";
      detail.textContent = ids
        .filter((id) => outcomes.get(id)?.status !== "ok")
        .map((id) => `${id}: ${outcomes.get(id).message}`)
        .join(" · ");
      detail.className = "helper-text helper-warn";
    }
    updateAccess();
  });
}

function paint(list, hosts, outcomes = new Map()) {
  list.innerHTML = "";
  for (const host of hosts) {
    list.appendChild(hostRow(host, outcomes.get(host.id)));
  }
}

function databaseBadgeLabel(db) {
  const name = db.db_unique_name || "DB";
  const state = db.instance_state || db.runtime_status || "unknown";
  return `${name} ${state}`;
}

function databaseBadgeKind(db) {
  const runtime = String(db.runtime_status || "").toLowerCase();
  const state = String(db.instance_state || "").toLowerCase();
  if (state) return classifyStatus(state);
  if (runtime === "unavailable") return "bad";
  return classifyStatus(runtime || state);
}

function clusterBadge(host) {
  const status = String(host.cluster_status || "").toLowerCase();
  if (status === "not_applicable") {
    const node = badge("no CRS", "ok");
    node.title = "No Clusterware on this host (normal for standalone). Database status is separate.";
    return node;
  }
  if (!host.cluster_status) return null;
  const node = badge(host.cluster_status, classifyStatus(host.cluster_status));
  if (status === "unavailable") {
    node.title = "Clusterware unavailable — CRS down or missing permission. Not a database status.";
  }
  return node;
}

function hostRow(host, outcome) {
  const row = el("a", {
    class: "host-picker-row",
    href: `#/hosts/${encodeURIComponent(host.id)}/discover`,
  });

  const dbNames = (host.databases || [])
    .map((d) => d.db_unique_name)
    .filter(Boolean)
    .slice(0, 3)
    .join(", ");
  const metaBits =
    host.status === "error"
      ? host.error?.message || "Discovery failed"
      : host.status === "pending"
        ? "No live evidence yet"
        : [
            host.active_version || (host.cluster_status === "not_applicable" ? "standalone" : "—"),
            `${host.node_count ?? 0} nodes`,
            `${host.oracle_home_count ?? 0} homes`,
            dbNames ? `DBs: ${dbNames}` : `${host.database_count ?? 0} DBs`,
          ].join(" · ");

  const left = el("div", { class: "host-picker-main" }, [
    el("div", { class: "host-picker-name", text: host.label || host.id }),
    el("div", { class: "host-picker-meta", text: metaBits }),
  ]);

  const right = el("div", { class: "host-picker-side" }, [
    badge(host.status, host.status === "ok" ? "ok" : host.status === "pending" ? "neutral" : "bad"),
  ]);
  const cluster = clusterBadge(host);
  if (cluster) right.appendChild(cluster);
  for (const db of host.databases || []) {
    right.appendChild(badge(databaseBadgeLabel(db), databaseBadgeKind(db)));
  }
  if (outcome) {
    const kind = outcome.status === "ok" ? "ok" : outcome.status === "used-cache" ? "warn" : "bad";
    right.appendChild(badge(outcome.status === "ok" ? "refresh ok" : outcome.status, kind));
  }

  row.appendChild(left);
  row.appendChild(right);
  return row;
}
