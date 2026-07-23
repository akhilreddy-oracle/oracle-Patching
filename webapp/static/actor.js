import { el } from "./dom.js";
import { getApiToken, setApiToken } from "./api.js";

const ACTOR_KEY = "opu-webapp-actor";

export function getActor() {
  return localStorage.getItem(ACTOR_KEY) || "";
}

export function mountActorWidget(container) {
  const actorInput = el("input", {
    type: "text",
    class: "actor-input",
    placeholder: "acting as…",
    value: getActor(),
    title: "Separation-of-duties identity sent to opu-patch-plan (requester/approver/operator must differ).",
  });
  actorInput.addEventListener("input", () => localStorage.setItem(ACTOR_KEY, actorInput.value));

  const tokenInput = el("input", {
    type: "password",
    class: "actor-input",
    placeholder: "API token",
    value: getApiToken(),
    title: "Bearer token from webapp/var/api-token (or OPU_WEBAPP_TOKEN). Required for every /api call.",
    autocomplete: "off",
  });
  tokenInput.addEventListener("input", () => setApiToken(tokenInput.value.trim()));

  container.appendChild(
    el("div", { class: "actor-widget" }, [
      el("label", { text: "Acting as" }),
      actorInput,
      el("label", { text: "API token" }),
      tokenInput,
    ])
  );
}
