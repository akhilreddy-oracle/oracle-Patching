import { el } from "./dom.js";
import { getApiToken, setApiToken } from "./api.js";

/** Keep authentication recovery beside the failed page, including when the
 * Session rail is below the fold. This only changes the browser credential;
 * the server still authenticates and authorizes every subsequent request. */
export function renderAuthRecovery(mount, { message, signal, retry }) {
  const tokenInput = el("input", {
    id: "recovery-api-token", type: "password", autocomplete: "off",
    placeholder: "Paste your personal API token", required: "",
    "aria-label": "Sign-in API token", "aria-describedby": "sign-in-help",
  });
  const form = el("form", { class: "auth-recovery-form" }, [
    el("label", { for: "recovery-api-token", text: "API token" }),
    tokenInput,
    el("button", { type: "submit", text: "Sign in" }),
  ]);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const token = tokenInput.value.trim();
    if (!token) { tokenInput.focus(); return; }
    const unchanged = token === getApiToken();
    setApiToken(token);
    tokenInput.value = "";
    // A changed token triggers the application's normal page refresh. An
    // unchanged token still needs a retry (for example after a server repair).
    if (unchanged) retry();
  });
  const company = el("div", { class: "auth-recovery-company" });
  mount.replaceChildren(el("section", { class: "error-box auth-recovery", role: "alert" }, [
    el("h2", { text: "Sign in to continue" }),
    el("p", { class: "error-message", text: message || "Authentication required." }),
    el("p", { id: "sign-in-help", text: "Enter the API token issued for your account. An expired token or an old shared lab token will not work when individual accounts are enabled." }),
    el("p", { text: "Each browser and address has its own sign-in. Signing in at 127.0.0.1 does not sign you in at localhost." }),
    form,
    company,
  ]));
  // Public configuration also makes company sign-in available at the point
  // of failure. Never send the entered API token to a login URL.
  fetch("/api/auth/config", { signal }).then(async (response) => {
    if (!response.ok) throw new Error("Company sign-in configuration is unavailable.");
    const config = await response.json();
    if (signal?.aborted || !config.configured) return;
    company.appendChild(el("a", { class: "btn", href: config.login_url, text: config.label || "Company sign in" }));
  }).catch((error) => {
    if (error.name !== "AbortError" && !signal?.aborted) {
      company.appendChild(el("p", { text: "Company sign-in configuration could not be loaded. Try again if you use company login." }));
    }
  });
}
