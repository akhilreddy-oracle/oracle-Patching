import { el } from "./dom.js";
import { getApiToken, setApiToken } from "./api.js";

const ACTOR_KEY = "opu-webapp-actor";
const ACTOR_EVENT = "opu-actor-change";
let identity = null;

export function authenticatedActor() {
  return identity?.rbac_enabled && identity.actor ? identity.actor : null;
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

export function mountActorWidget(container) {
  const actorInput = el("input", {
    id: "topbar-acting-as",
    type: "text",
    class: "actor-input",
    placeholder: "operator id…",
    value: getActor(),
    title: "Separation-of-duties identity (requester / approver / operator must differ). Prefills Requester and Actor fields on forms.",
    "aria-label": "Acting as",
  });
  actorInput.addEventListener("input", () => setActor(actorInput.value));
  onActorChange((value) => {
    if (document.activeElement !== actorInput) actorInput.value = value;
  });

  const tokenInput = el("input", {
    id: "topbar-api-token",
    type: "password",
    class: "actor-input actor-token",
    placeholder: "paste API token",
    value: getApiToken(),
    title: "Bearer token from webapp/var/api-token (or OPU_WEBAPP_TOKEN). Required for every /api call.",
    autocomplete: "off",
    "aria-label": "API token",
  });
  tokenInput.addEventListener("input", () => setApiToken(tokenInput.value.trim()));

  container.appendChild(
    el("div", { class: "actor-widget" }, [
      el("div", { class: "actor-widget-block" }, [
        el("label", { for: "topbar-acting-as", text: "Acting as" }),
        actorInput,
      ]),
      el("div", { class: "actor-widget-block" }, [
        el("label", { for: "topbar-api-token", text: "API token" }),
        tokenInput,
      ]),
      el("p", {
        class: "actor-hint",
        text: "Acting as prefills Requester / Actor on plans & recovery. Token required for all API calls.",
      }),
    ])
  );
}
