import { el, badge, classifyStatus } from "./dom.js";

const REQUIRED_LABELS = {
  always: "Required for all topologies",
  rac_or_grid: "Required for RAC / Grid; N/A on single-instance",
  database_family: "Required for database-family; N/A for Grid-only",
};

/** Render live discovery phases derived server-side from snapshot evidence. */
export function renderDiscoveryPhases(phases, { heading = "Discovery phases" } = {}) {
  const wrap = el("div", { class: "discovery-phases-wrap" });
  if (heading) wrap.appendChild(el("h3", { class: "discovery-phases-heading", text: heading }));
  if (!phases || !phases.length) {
    wrap.appendChild(el("p", { class: "helper-text helper-warn", text: "No discovery phases yet — run live SSH discovery." }));
    return wrap;
  }
  const list = el("ul", { class: "discovery-phases" });
  for (const phase of phases) {
    const req = REQUIRED_LABELS[phase.required] || phase.required || "";
    const item = el("li", { class: "discovery-phase" }, [
      el("span", { class: "discovery-phase-letter", text: phase.letter || "?" }),
      badge(phase.status || "unknown", classifyStatus(phase.status)),
      el("div", { class: "discovery-phase-body" }, [
        el("div", { class: "discovery-phase-title", text: phase.label || phase.id || "phase" }),
        el("div", { class: "discovery-phase-summary", text: phase.summary || "" }),
        el(
          "div",
          { class: "discovery-phase-meta" },
          [
            el("span", { class: "discovery-phase-req", text: req }),
            document.createTextNode(phase.why ? ` · ${phase.why}` : ""),
          ]
        ),
        phase.unblocks
          ? el("div", { class: "discovery-phase-meta", text: `Unblocks: ${phase.unblocks}` })
          : document.createTextNode(""),
        phase.detail
          ? el("div", { class: "discovery-phase-meta", text: phase.detail })
          : document.createTextNode(""),
      ]),
    ]);
    list.appendChild(item);
  }
  wrap.appendChild(list);
  return wrap;
}
