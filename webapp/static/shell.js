import { el } from "./dom.js";
import { apiFetch, getReadSignal, setSessionCsrf } from "./api.js";
import { getActor, setActor, onActorChange, setSessionIdentity, authenticatedActor } from "./actor.js";
import { getApiToken, setApiToken, TOKEN_EVENT } from "./api.js";

/** Soft lab default when localStorage is empty after hard refresh. Not an approver/operator. */
const LAB_DEFAULT_ACTOR = "lab-operator";

let hostsCache = [];
let hostRequest = 0;
let sessionNote;

export function clearHostCache() {
  hostsCache = [];
  ++hostRequest;
  document.getElementById("rail-hosts")?.replaceChildren();
}

export async function refreshSession() {
  const signal = getReadSignal();
  try {
    const response = await apiFetch("/api/session", { signal });
    const session = await response.json();
    signal?.throwIfAborted();
    setSessionIdentity(session);
    setSessionCsrf(session.csrf_token);
    if (sessionNote) sessionNote.textContent = session.rbac_enabled
      ? `Authenticated as ${session.display_name || session.actor}. Roles: ${(session.roles || []).join(", ")}.${session.expires_at ? ` Session expires ${new Date(session.expires_at * 1000).toLocaleTimeString()}.` : ""}`
      : "Lab session: select an actor for separation of duties.";
  } catch (error) {
    if (!signal?.aborted && error.name !== "AbortError") {
      setSessionIdentity(null);
      setSessionCsrf(null);
      if (sessionNote) sessionNote.textContent = error.message;
    }
    throw error;
  }
}


/** Focus Session rail field and briefly highlight the panel (used when Create plan etc. fail validation). */
export function focusSessionField(which = "actor") {
  const panel = document.getElementById("rail-session");
  const input = document.getElementById(which === "token" ? "session-api-token" : "session-acting-as");
  if (panel) {
    panel.classList.add("is-attention");
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    window.setTimeout(() => panel.classList.remove("is-attention"), 2400);
  }
  if (input) {
    input.focus();
    input.select?.();
  }
}

export function mountSession(container) {
  container.innerHTML = "";
  if (!getActor()) setActor(LAB_DEFAULT_ACTOR);

  const actorInput = el("input", {
    id: "session-acting-as",
    type: "text",
    class: "session-input",
    placeholder: "e.g. alice (requester)",
    value: getActor(),
    title: "SoD identity — prefills Requester/Actor. Use a different person to approve/authorize later.",
    "aria-label": "Acting as",
  });
  actorInput.addEventListener("input", () => setActor(actorInput.value));
  onActorChange((value) => {
    actorInput.readOnly = Boolean(authenticatedActor());
    if (document.activeElement !== actorInput || actorInput.readOnly) actorInput.value = value;
  });

  const tokenInput = el("input", {
    id: "session-api-token",
    type: "password",
    class: "session-input",
    placeholder: "Your personal API token",
    value: getApiToken(),
    autocomplete: "off",
    "aria-label": "API token",
  });
  tokenInput.addEventListener("input", () => setApiToken(tokenInput.value.trim()));
  window.addEventListener(TOKEN_EVENT, () => {
    if (document.activeElement !== tokenInput) tokenInput.value = getApiToken();
  });

  sessionNote = el("p", { class: "rail-muted", role: "status", "aria-live": "polite", text: "Enter an API token to load your session." });
  container.appendChild(el("div", { class: "rail-section-label", text: "Session" }));
  container.appendChild(
    el("div", { class: "session-fields" }, [
      el("label", { class: "session-field" }, [
        el("span", { text: "Acting as" }),
        actorInput,
      ]),
      el("label", { class: "session-field" }, [
        el("span", { text: "API token" }),
        tokenInput,
      ]),
    ])
  );
  container.appendChild(sessionNote);
  const company = el("div", { class: "company-login" });
  container.appendChild(company);
  fetch("/api/auth/config").then(async (res) => {
    if (!res.ok) throw new Error("Company login configuration needs administrator attention.");
    const config = await res.json();
    if (!config.configured) return;
    company.appendChild(el("a", { class: "btn", href: config.login_url, text: config.label || "Company sign in" }));
    const logout = el("button", { type: "button", class: "btn btn-secondary", text: "Sign out" });
    logout.addEventListener("click", async () => {
      try {
        await apiFetch("/api/auth/logout", { method: "POST", body: "{}" });
        setSessionCsrf(null); setSessionIdentity(null); setApiToken(""); location.assign("/");
      } catch (error) { sessionNote.textContent = error.message; }
    });
    company.appendChild(logout);
  }).catch((error) => { company.appendChild(el("p", { class: "rail-muted", text: error.message })); });
}

export async function refreshHostNav(activeHostId) {
  const mount = document.getElementById("rail-hosts");
  if (!mount) return hostsCache;

  const request = ++hostRequest;
  const signal = getReadSignal();
  try {
    const res = await apiFetch("/api/estate", { signal });
    const data = await res.json();
    signal?.throwIfAborted();
    if (request !== hostRequest) return hostsCache;
    if (res.ok && Array.isArray(data.hosts)) {
      hostsCache = data.hosts;
    }
  } catch (error) {
    if (request !== hostRequest || error.name === "AbortError") throw error;
    hostsCache = [];
    mount.replaceChildren(el("p", { class: "rail-muted", role: "status", text: error.message }));
    throw error;
  }

  mount.innerHTML = "";
  if (!hostsCache.length) {
    mount.appendChild(el("p", { class: "rail-muted", text: "No hosts in hosts.json" }));
    return hostsCache;
  }

  for (const host of hostsCache) {
    const href = `#/hosts/${encodeURIComponent(host.id)}/discover`;
    const link = el("a", {
      class: `rail-host${host.id === activeHostId ? " is-active" : ""}`,
      ...(host.id === activeHostId ? { "aria-current": "page" } : {}),
      href,
    }, [
      el("span", { class: "rail-host-label", text: host.label || host.id }),
      el("span", {
        class: `rail-host-status status-${host.status === "ok" ? "ok" : host.status === "pending" ? "pending" : "bad"}`,
        text: host.status || "?",
      }),
    ]);
    mount.appendChild(link);
  }
  return hostsCache;
}

export function syncSecondaryNav(active) {
  document.querySelectorAll(".rail-link[data-nav]").forEach((link) => {
    const selected = link.getAttribute("data-nav") === active;
    link.classList.toggle("is-active", selected);
    if (selected) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}
