import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch } from "./api.js";

export function factText(fact) {
  if (!fact || fact.status !== "verified") return "Unknown";
  return typeof fact.value === "object" ? JSON.stringify(fact.value) : String(fact.value);
}

export function evidenceReport(planId) {
  const section = el("section", { class: "panel", "aria-label": "Patch evidence report" });
  const content = el("div");
  const message = el("p", { class: "helper-text", role: "status", "aria-live": "polite", text: "Compare saved, verified observations and export an evidence report." });
  const controls = el("div", { class: "pipeline-controls" });
  const load = el("button", { type: "button", text: "View before and after report" });
  controls.appendChild(load);
  section.appendChild(el("h2", { text: "Before and after evidence" }));
  section.appendChild(message); section.appendChild(controls); section.appendChild(content);
  load.addEventListener("click", async () => {
    load.disabled = true;
    try {
      const response = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/report`);
      const report = await response.json();
      content.innerHTML = "";
      message.textContent = `${report.after_scope}. ${report.interpretation}`;
      const rows = (report.comparison || []).map(row => el("tr", {}, [el("th", { scope: "row", text: row.label }),
        el("td", { text: factText(row.before) }), el("td", { text: factText(row.after) }),
        el("td", { text: row.after?.source?.label || "No verified evidence" })]));
      content.appendChild(el("div", { class: "table-scroll" }, [el("table", {}, [el("thead", {}, [el("tr", {}, ["Check", "Before", "After", "Evidence"].map(text => el("th", { text })))]), el("tbody", {}, rows)])]));
      content.appendChild(el("p", {}, [document.createTextNode("Rollback: "), badge(report.rollback?.status || "unknown", classifyStatus(report.rollback?.status)),
        document.createTextNode(` ${report.rollback?.reason || "Native validation has not run."}`)]));
      for (const task of report.evidence?.tasks || []) {
        if (task.status !== "failed") continue;
        const failure = el("section", { class: "card card-failure" }, [
          el("h3", { text: `Saved failure: ${task.task_id}` }),
          el("p", { text: task.postcondition?.detail || "Task failed" }),
          el("p", { class: "helper-text", text: `Recorded ${task.finished_at || "at an unknown time"}. These are saved diagnostics, not a new database check.` }),
        ]);
        for (const diagnostic of task.failure_diagnostics || []) {
          failure.appendChild(el("pre", { class: "run-log run-log-error", text: diagnostic.text }));
        }
        if (!task.failure_diagnostics?.length) failure.appendChild(el("p", { text: "No verified diagnostic log is available. Inspect the evidence gaps before considering a retry." }));
        failure.appendChild(el("p", { class: "helper-text", text: task.next_action || "Review verified evidence before retrying." }));
        content.appendChild(failure);
      }
      if (report.gaps?.length) content.appendChild(el("details", {}, [el("summary", { text: `${report.gaps.length} evidence gap(s)` }),
        el("ul", {}, report.gaps.map(gap => el("li", { text: `${gap.source}: ${gap.reason}` })))]));
    } catch (error) { message.textContent = `Report unavailable: ${error.message}`; }
    finally { load.disabled = false; }
  });
  for (const [format, label] of [["json", "Export evidence JSON"], ["html", "Export printable HTML"], ["csv", "Export comparison CSV"]]) {
    const button = el("button", { type: "button", text: label });
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await apiFetch(`/api/plans/${encodeURIComponent(planId)}/report?format=${format}`);
        const url = URL.createObjectURL(await response.blob());
        const link = el("a", { href: url, download: `${planId.replace(/[^A-Za-z0-9_.-]/g, "_")}-evidence.${format}` });
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { message.textContent = `Export unavailable: ${error.message}`; }
      finally { button.disabled = false; }
    });
    controls.appendChild(button);
  }
  return section;
}
