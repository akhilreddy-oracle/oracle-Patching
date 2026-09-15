import { apiFetch } from "./api.js";
import { el, badge } from "./dom.js";

export async function renderValidation(mount) {
  mount.appendChild(el("h1", { text: "Release validation" }));
  mount.appendChild(el("p", { class: "helper-text", text: "Test evidence belongs to a specific source revision and configuration. Fixture tests, live lab verification and production approval are separate levels." }));
  const response = await apiFetch("/api/validation");
  const evidence = await response.json();
  for (const [key, label] of [["fixture_tested", "Fixture tests"], ["live_lab_verified", "Live lab verification"], ["production_approved", "Production approval"]]) {
    const value = evidence[key] || { status: "unknown", reason: "No evidence available" };
    mount.appendChild(el("section", { class: "panel" }, [
      el("h2", { text: label }), badge(value.status, value.status === "passed" ? "ok" : value.status === "failed" ? "bad" : "neutral"),
      el("p", { text: value.reason || "Evidence verified for this source." }),
      ...(value.scopes?.length ? [el("p", { text: `Checked: ${value.scopes.join(", ")}` })] : []),
    ]));
  }
}
