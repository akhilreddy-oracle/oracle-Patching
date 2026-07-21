import { el } from "./dom.js";

const ACTOR_KEY = "opu-webapp-actor";

export function getActor() {
  return localStorage.getItem(ACTOR_KEY) || "";
}

export function mountActorWidget(container) {
  const input = el("input", {
    type: "text",
    class: "actor-input",
    placeholder: "acting as…",
    value: getActor(),
    title: "Not a security boundary — no auth system exists yet. The CLI's own separation-of-duties checks (requester/approver/operator must differ) are the real enforcement; this just fills that field.",
  });
  input.addEventListener("input", () => localStorage.setItem(ACTOR_KEY, input.value));
  container.appendChild(el("div", { class: "actor-widget" }, [el("label", { text: "Acting as" }), input]));
}
