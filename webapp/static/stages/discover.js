import { el, badge, classifyStatus, row, th, td, tdBadge, renderErrorBox } from "../dom.js";
import { apiFetch } from "../api.js";
import { runToCompletion, RunStartError } from "../runs.js";
import {
  helperText,
  formErrorBox,
  showFormError,
  clearFormError,
  requireToken,
  formatRunFailure,
} from "../ux.js";
import { renderDiscoveryPhases } from "../discovery_phases.js";

const CLUSTER_BADGE_HINTS = {
  unavailable: "Clusterware unavailable — CRS down or missing permission. Not a database status.",
  not_applicable: "No Clusterware on this host (normal for standalone). Database status is separate.",
  failed: "Cluster probe failed — see warnings.",
  unknown: "Cluster state not reported.",
  partial: "Cluster evidence incomplete across nodes.",
  detected: "Clusterware present in inventory.",
};

const DB_BADGE_HINTS = {
  complete: "SYSDBA runtime evidence collected successfully.",
  unavailable: "Database runtime probe failed (instance down, SQL*Plus, or Oracle owner).",
  unknown: "Database runtime not reported.",
  open: "Instance is OPEN.",
};

export async function renderDiscoverStage(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "stage-head" }, [
      el("h2", { class: "stage-title", text: "Discover" }),
      el("p", { class: "stage-lead", text: "One live SSH snapshot via opu-topology-discover, mapped to phases A–E." }),
    ])
  );

  const toolbar = el("div", { class: "toolbar" }, [
    el("button", { id: "discover-run", type: "button", text: "Run live discovery" }),
    el("span", { id: "discover-status", class: "status-chip", role: "status", "aria-live": "polite", text: "idle" }),
  ]);
  mount.appendChild(toolbar);
  const errBox = formErrorBox();
  mount.appendChild(errBox);
  const logBox = el("pre", { class: "run-log", style: "display:none" });
  mount.appendChild(logBox);
  const body = el("div", { class: "stage-body" });
  mount.appendChild(body);

  async function showCached() {
    body.innerHTML = "";
    try {
      const res = await apiFetch(`/api/hosts/${encodeURIComponent(hostId)}/pipeline`);
      const pipe = await res.json();
      const disc = (pipe.steps || []).find((s) => s.step === "discovery");
      if (!disc?.done || !disc.evidence) {
        body.appendChild(helperText("No live discovery evidence yet. Run live discovery (30–90s).", "warn"));
        return;
      }
      renderTopology(body, disc.evidence, disc.phases || [], hostId);
      const statusEl = toolbar.querySelector("#discover-status");
      statusEl.textContent = disc.phases_status || disc.status || "cached";
      statusEl.className = `status-chip is-${classifyStatus(disc.phases_status || disc.status) === "ok" ? "ok" : "warn"}`;
    } catch (err) {
      body.appendChild(helperText(`Could not load pipeline state: ${err}`, "error"));
    }
  }

  toolbar.querySelector("#discover-run").addEventListener("click", async () => {
    const btn = toolbar.querySelector("#discover-run");
    const statusEl = toolbar.querySelector("#discover-status");
    clearFormError(errBox);
    if (!requireToken(errBox)) return;
    btn.disabled = true;
    statusEl.textContent = "running…";
    statusEl.className = "status-chip is-loading";
    logBox.style.display = "block";
    logBox.classList.remove("run-log-error");
    logBox.textContent = "starting live SSH…";
    try {
      const record = await runToCompletion(
        `/api/hosts/${encodeURIComponent(hostId)}/pipeline/discovery`,
        {},
        {
          onTick: (rec) => {
            statusEl.textContent = rec.status;
            logBox.textContent = `status: ${rec.status}\n` + (rec.log_tail || []).join("\n");
          },
        }
      );
      if (record.status === "failed") {
        logBox.classList.add("run-log-error");
        logBox.textContent = formatRunFailure(record);
        statusEl.textContent = "failed";
        statusEl.className = "status-chip is-bad";
        renderErrorBox(body, record.error || { message: "Discovery failed" });
      } else {
        logBox.textContent = "succeeded";
        statusEl.textContent = "ok";
        statusEl.className = "status-chip is-ok";
        await showCached();
      }
    } catch (err) {
      const msg = err instanceof RunStartError ? err.message : String(err);
      showFormError(errBox, msg);
      logBox.classList.add("run-log-error");
      logBox.textContent = msg;
      statusEl.textContent = "error";
      statusEl.className = "status-chip is-bad";
    } finally {
      btn.disabled = false;
    }
  });

  await showCached();
}

function annotatedBadge(value, hints = {}, status = value) {
  const node = badge(value, classifyStatus(status));
  const key = String(status || "").toLowerCase();
  if (hints[key]) node.title = hints[key];
  return node;
}

function clusterDisplayStatus(cluster) {
  const status = String(cluster?.status || "").toLowerCase();
  const nodes = cluster?.nodes || [];
  if (status === "unavailable" && !nodes.length && !cluster?.grid_home) {
    return "not_applicable";
  }
  return cluster?.status;
}

function dbRuntimeLabel(runtime = {}) {
  const status = runtime.status || "unknown";
  if (runtime.instance_state) return `${status} · ${runtime.instance_state}`;
  return status;
}

function renderTopology(mount, data, phases, hostId) {
  mount.innerHTML = "";
  const host = data.host || {};
  const cluster = data.cluster || {};
  const runtime = cluster.runtime || {};
  const clusterStatus = clusterDisplayStatus(cluster);

  mount.appendChild(
    el("div", { class: "meta-row" }, [
      el("span", {}, [document.createTextNode("Host "), el("strong", { text: host.name || "—" })]),
      el("span", {}, [document.createTextNode("OS "), el("strong", { text: host.os || "—" })]),
      el("span", {}, [document.createTextNode("Kernel "), el("strong", { text: host.kernel || "—" })]),
      el("span", {}, [document.createTextNode("Collected "), el("strong", { text: data.collected_at || "—" })]),
    ])
  );

  mount.appendChild(
    el("section", { class: "panel" }, [
      renderDiscoveryPhases(phases, { heading: "Phases A–E" }),
    ])
  );

  mount.appendChild(
    el("section", { class: "panel" }, [
      el("h3", { class: "panel-title", text: "Cluster" }),
      el("table", {}, [
        el("tbody", {}, [
          row("Status", annotatedBadge(clusterStatus, CLUSTER_BADGE_HINTS)),
          row("Grid home", el("span", { class: "mono", text: cluster.grid_home || "—" })),
          row("Active version", el("span", { text: runtime.active_version || "—" })),
          row(
            "Upgrade state",
            clusterStatus === "not_applicable"
              ? el("span", { text: "—" })
              : annotatedBadge(runtime.upgrade_state, CLUSTER_BADGE_HINTS)
          ),
        ]),
      ]),
      cluster.nodes?.length
        ? el(
            "div",
            { class: "node-list" },
            cluster.nodes.map((n) => annotatedBadge(`${n.name} (${n.status})`, CLUSTER_BADGE_HINTS, n.status))
          )
        : document.createTextNode(""),
    ])
  );

  mount.appendChild(
    el("section", { class: "panel" }, [
      el("h3", { class: "panel-title", text: `Oracle homes (${(data.oracle_homes || []).length})` }),
      el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [th("Path"), th("Owner"), th("Version"), th("OPatch"), th("XML"), th("Patches")]),
        ]),
        el(
          "tbody",
          {},
          (data.oracle_homes || []).map((home) =>
            el("tr", {}, [
              td(home.path, "mono wrap"),
              td(home.owner),
              td(home.version || "—"),
              td(home.opatch_version || "—"),
              tdBadge(home.opatch_inventory_xml_status, classifyStatus(home.opatch_inventory_xml_status)),
              td(String((home.patches || []).length)),
            ])
          )
        ),
      ]),
    ])
  );

  mount.appendChild(
    el("section", { class: "panel" }, [
      el("h3", { class: "panel-title", text: `Databases (${(data.databases || []).length})` }),
      el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [th("DB unique name"), th("Home"), th("Runtime"), th("Open mode")]),
        ]),
        el(
          "tbody",
          {},
          (data.databases || []).map((db) =>
            el("tr", {}, [
              td(db.db_unique_name),
              td(db.oracle_home, "mono wrap"),
              el("td", {}, [annotatedBadge(dbRuntimeLabel(db.runtime), DB_BADGE_HINTS, db.runtime?.instance_state || db.runtime?.status)]),
              td(db.runtime?.open_mode || "—"),
            ])
          )
        ),
      ]),
    ])
  );

  if (data.warnings?.length) {
    mount.appendChild(
      el("section", { class: "panel panel-warn" }, [
        el("h3", { class: "panel-title", text: `Warnings (${data.warnings.length})` }),
        el(
          "ul",
          {},
          data.warnings.map((w) => el("li", { text: typeof w === "string" ? w : JSON.stringify(w) }))
        ),
      ])
    );
  }

  mount.appendChild(
    el("p", { class: "stage-next" }, [
      el("a", {
        href: `#/hosts/${encodeURIComponent(hostId)}/readiness`,
        text: "Continue to Readiness →",
      }),
    ])
  );
}
