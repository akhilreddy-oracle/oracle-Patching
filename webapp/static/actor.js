const ACTOR_KEY = "opu-webapp-actor";
const ACTOR_EVENT = "opu-actor-change";
let identity = null;

export function authenticatedActor() {
  return identity?.rbac_enabled && identity.actor ? identity.actor : null;
}

/** UI admission hint from /api/session; the server authorizes every request. */
export function companySessionState() {
  if (identity?.mode !== "company") return "absent";
  return identity.rbac_enabled === true && typeof identity.actor === "string" && identity.actor
    && typeof identity.csrf_token === "string" && identity.csrf_token
    && typeof identity.expires_at === "number" && Number.isFinite(identity.expires_at)
    && identity.expires_at * 1000 > Date.now() ? "active" : "unavailable";
}

/** Use server-derived admission; never infer execution rights from role names. */
export function liveDiscoveryAccess() {
  if (!identity || typeof identity.permissions?.live_discovery !== "boolean") {
    return { allowed: false, reason: "Live discovery permissions are unavailable. Sign in or reload this page to refresh your session." };
  }
  if (identity.mode === "company" && companySessionState() !== "active") {
    return { allowed: false, reason: "Company session expired or unavailable. Sign in again before running live discovery." };
  }
  return identity.permissions.live_discovery
    ? { allowed: true, reason: "" }
    : { allowed: false, reason: "Your account cannot run live discovery. Sign in with an account permitted to execute host operations." };
}

export function setSessionIdentity(session) {
  identity = session;
  setActor(authenticatedActor() || getActor());
}


export function getActor() {
  return localStorage.getItem(ACTOR_KEY) || "";
}

export function setActor(value) {
  const next = authenticatedActor() || (value == null ? "" : String(value));
  localStorage.setItem(ACTOR_KEY, next);
  window.dispatchEvent(new CustomEvent(ACTOR_EVENT, { detail: next }));
}

/** Subscribe to Acting-as changes. Returns an unsubscribe function. */
export function onActorChange(callback) {
  const handler = (event) => callback(event.detail);
  window.addEventListener(ACTOR_EVENT, handler);
  return () => window.removeEventListener(ACTOR_EVENT, handler);
}
