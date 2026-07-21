import { el, badge, classifyStatus, row, th, td, tdBadge, renderErrorBox } from "./dom.js";

export async function renderHost(mount, hostId) {
  mount.innerHTML = "";

  const header = el("div", { class: "host-header" }, [
    el("a", { class: "back-link", href: "#/estate", text: "← Estate" }),
    el("span", { id: "host-status-pill", class: "pill pill-loading", text: "discovering…" }),
    el("button", { id: "host-refresh-btn", type: "button", text: "Refresh" }),
    el("a", { class: "back-link", href: `#/hosts/${encodeURIComponent(hostId)}/pipeline`, text: "Readiness pipeline →" }),
  ]);
  mount.appendChild(header);

  const body = el("div", { id: "host-body" });
  mount.appendChild(body);

  const statusPill = header.querySelector("#host-status-pill");
  const refreshBtn = header.querySelector("#host-refresh-btn");

  async function load() {
    statusPill.textContent = "discovering…";
    statusPill.className = "pill pill-loading";
    refreshBtn.disabled = true;
    try {
      const res = await fetch(`/api/hosts/${encodeURIComponent(hostId)}/discovery`);
      const data = await res.json();
      if (!res.ok) {
        renderErrorBox(body, data);
        statusPill.textContent = `failed (${res.status})`;
        statusPill.className = "pill pill-error";
        return;
      }
      renderDiscovery(body, data);
      statusPill.textContent = "collected " + (data.collected_at || "");
      statusPill.className = "pill pill-ok";
    } catch (err) {
      renderErrorBox(body, { message: String(err) });
      statusPill.textContent = "request failed";
      statusPill.className = "pill pill-error";
    } finally {
      refreshBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener("click", load);
  await load();
}

function renderDiscovery(mount, data) {
  mount.innerHTML = "";

  const host = data.host || {};
  const cluster = data.cluster || {};
  const runtime = cluster.runtime || {};

  mount.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("Host: "), el("strong", { text: host.name || "unknown" })]),
      el("span", {}, [document.createTextNode("OS: "), el("strong", { text: host.os || "unknown" })]),
      el("span", {}, [document.createTextNode("Kernel: "), el("strong", { text: host.kernel || "unknown" })]),
      el("span", {}, [document.createTextNode("Collected: "), el("strong", { text: data.collected_at || "unknown" })]),
      el("span", {}, [document.createTextNode("Collector: "), el("strong", { text: `${data.collector?.name || "?"} v${data.collector?.version || "?"}` })]),
    ])
  );

  const clusterBody = el("table", {}, [
    el("tbody", {}, [
      row("Status", badge(cluster.status, classifyStatus(cluster.status))),
      row("Grid home", el("span", { class: "mono", text: cluster.grid_home || "—" })),
      row("Active version", el("span", { text: runtime.active_version || "—" })),
      row("Upgrade state", badge(runtime.upgrade_state, classifyStatus(runtime.upgrade_state))),
      row("Active patch level", el("span", { text: runtime.active_patch_level || "—" })),
    ]),
  ]);
  const nodeList = el(
    "div",
    { class: "node-list" },
    (cluster.nodes || []).map((n) => badge(`${n.name} (${n.status})`, classifyStatus(n.status)))
  );
  mount.appendChild(
    el("section", { class: "card" }, [
      el("h2", { text: "Cluster / Topology" }),
      clusterBody,
      cluster.nodes && cluster.nodes.length ? el("div", { style: "margin-top:12px" }, [nodeList]) : document.createTextNode(""),
    ])
  );

  const homesTable = el("table", {}, [
    el("thead", {}, [el("tr", {}, [th("Path"), th("Owner"), th("Version"), th("OPatch"), th("Inventory source"), th("XML status"), th("Patches")])]),
    el(
      "tbody",
      {},
      (data.oracle_homes || []).map((home) =>
        el("tr", {}, [
          td(home.path, "mono wrap"),
          td(home.owner),
          td(home.version || "—"),
          td(home.opatch_version || "—"),
          tdBadge(home.patch_inventory_source, classifyStatus(home.patch_inventory_source)),
          tdBadge(home.opatch_inventory_xml_status, classifyStatus(home.opatch_inventory_xml_status)),
          td(String((home.patches || []).length)),
        ])
      )
    ),
  ]);
  mount.appendChild(el("section", { class: "card" }, [el("h2", { text: `Oracle Homes (${(data.oracle_homes || []).length})` }), homesTable]));

  const dbTable = el("table", {}, [
    el("thead", {}, [el("tr", {}, [th("DB unique name"), th("Oracle home"), th("srvctl status"), th("Runtime status")])]),
    el(
      "tbody",
      {},
      (data.databases || []).map((db) =>
        el("tr", {}, [
          td(db.db_unique_name),
          td(db.oracle_home, "mono wrap"),
          td(db.srvctl_status, "wrap"),
          tdBadge(db.runtime?.status, classifyStatus(db.runtime?.status)),
        ])
      )
    ),
  ]);
  mount.appendChild(el("section", { class: "card" }, [el("h2", { text: `Databases (${(data.databases || []).length})` }), dbTable]));

  if (data.warnings && data.warnings.length) {
    mount.appendChild(
      el("section", { class: "card warnings" }, [
        el("h2", { text: `Warnings (${data.warnings.length})` }),
        el(
          "ul",
          {},
          data.warnings.map((w) => el("li", { text: typeof w === "string" ? w : JSON.stringify(w) }))
        ),
      ])
    );
  }
}
