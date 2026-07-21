const hostSelect = document.getElementById("host-select");
const refreshBtn = document.getElementById("refresh-btn");
const statusPill = document.getElementById("status-pill");
const app = document.getElementById("app");
const errorTpl = document.getElementById("tpl-error");

function setStatus(state, label) {
  statusPill.textContent = label;
  statusPill.className = `pill pill-${state}`;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function badge(value, kind) {
  const kinds = { ok: "badge-ok", warn: "badge-warn", bad: "badge-bad", neutral: "badge-neutral" };
  return el("span", { class: `badge ${kinds[kind] || kinds.neutral}`, text: value ?? "unknown" });
}

function classifyStatus(value) {
  if (value === null || value === undefined) return "neutral";
  const v = String(value).toLowerCase();
  if (["active", "healthy", "normal", "passed", "running", "success", "complete", "detected"].some((s) => v.includes(s))) return "ok";
  if (["failed", "blocked", "down", "not running", "error"].some((s) => v.includes(s))) return "bad";
  if (["unavailable", "unknown", "partial"].some((s) => v.includes(s))) return "warn";
  return "neutral";
}

async function loadHosts() {
  const res = await fetch("/api/hosts");
  const data = await res.json();
  hostSelect.innerHTML = "";
  for (const host of data.hosts) {
    hostSelect.appendChild(el("option", { value: host.id, text: host.label }));
  }
  if (data.hosts.length > 0) runDiscovery();
}

async function runDiscovery() {
  const hostId = hostSelect.value;
  if (!hostId) return;
  setStatus("loading", "discovering…");
  refreshBtn.disabled = true;
  try {
    const res = await fetch(`/api/hosts/${encodeURIComponent(hostId)}/discovery`);
    const data = await res.json();
    if (!res.ok) {
      renderError(data);
      setStatus("error", `failed (${res.status})`);
      return;
    }
    renderDiscovery(data);
    setStatus("ok", "collected " + (data.collected_at || ""));
  } catch (err) {
    renderError({ message: String(err) });
    setStatus("error", "request failed");
  } finally {
    refreshBtn.disabled = false;
  }
}

function renderError(data) {
  const frag = errorTpl.content.cloneNode(true);
  frag.querySelector(".error-message").textContent = data.message || "Unknown error";
  frag.querySelector(".error-detail").textContent = data.stderr || "";
  app.innerHTML = "";
  app.appendChild(frag);
}

function renderDiscovery(data) {
  app.innerHTML = "";

  const host = data.host || {};
  const cluster = data.cluster || {};
  const runtime = cluster.runtime || {};

  app.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("Host: "), el("strong", { text: host.name || "unknown" })]),
      el("span", {}, [document.createTextNode("OS: "), el("strong", { text: host.os || "unknown" })]),
      el("span", {}, [document.createTextNode("Kernel: "), el("strong", { text: host.kernel || "unknown" })]),
      el("span", {}, [document.createTextNode("Collected: "), el("strong", { text: data.collected_at || "unknown" })]),
      el("span", {}, [document.createTextNode("Collector: "), el("strong", { text: `${data.collector?.name || "?"} v${data.collector?.version || "?"}` })]),
    ])
  );

  // Cluster / topology card
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
  app.appendChild(
    el("section", { class: "card" }, [
      el("h2", { text: "Cluster / Topology" }),
      clusterBody,
      cluster.nodes && cluster.nodes.length ? el("div", { style: "margin-top:12px" }, [nodeList]) : document.createTextNode(""),
    ])
  );

  // Oracle homes card
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
  app.appendChild(el("section", { class: "card" }, [el("h2", { text: `Oracle Homes (${(data.oracle_homes || []).length})` }), homesTable]));

  // Databases card
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
  app.appendChild(el("section", { class: "card" }, [el("h2", { text: `Databases (${(data.databases || []).length})` }), dbTable]));

  // Warnings
  if (data.warnings && data.warnings.length) {
    app.appendChild(
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

function row(label, valueNode) {
  return el("tr", {}, [el("th", { text: label }), el("td", {}, [valueNode])]);
}
function th(text) {
  return el("th", { text });
}
function td(text, cls) {
  return el("td", { class: cls || "" , text: text ?? "—"});
}
function tdBadge(value, kind) {
  return el("td", {}, [badge(value, kind)]);
}

hostSelect.addEventListener("change", runDiscovery);
refreshBtn.addEventListener("click", runDiscovery);

loadHosts();
