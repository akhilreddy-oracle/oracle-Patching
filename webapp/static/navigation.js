import { el, renderErrorBox } from "./dom.js";
import { setReadSignal } from "./api.js";
import { renderAuthRecovery } from "./auth_recovery.js";

/** Each navigation owns its mount and cancellable reads. Late results may only
 * update a detached mount; they can never replace a newer page or its errors. */
export function createPageRenderer(app, loadPage) {
  let controller;
  let generation = 0;
  const render = async ({ focus = true } = {}) => {
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    const current = ++generation;
    setReadSignal(signal);
    const view = el("div", { class: "route-view", "aria-busy": "true" });
    const loading = el("p", { class: "empty-state", role: "status", text: "Loading…" });
    view.appendChild(loading);
    app.replaceChildren(view);
    try {
      await loadPage(view, signal);
      if (signal.aborted || current !== generation) return;
    } catch (error) {
      if (signal.aborted || current !== generation || error.name === "AbortError") return;
      if (error.status === 401) {
        renderAuthRecovery(view, { message: error.message, signal, retry: () => render() });
      } else {
        renderErrorBox(view, { message: error.message || "Could not load this page." });
      }
      const retry = el("button", { type: "button", text: "Try again" });
      retry.addEventListener("click", () => render());
      view.appendChild(retry);
    } finally {
      // Remove this navigation's indicator even when its renderer appends
      // content. A detached indicator cannot affect a newer page.
      loading.remove();
      view.setAttribute("aria-busy", "false");
    }
    if (focus && current === generation) {
      const heading = view.querySelector("h1, h2");
      heading?.setAttribute("tabindex", "-1");
      heading?.focus();
      view.querySelector(".auth-recovery")?.scrollIntoView({ block: "start" });
    }
  };
  render.cancel = () => { ++generation; controller?.abort(); };
  return render;
}
