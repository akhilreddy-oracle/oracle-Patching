import { el, badge, classifyStatus } from "./dom.js";
import { apiFetch, getReadSignal } from "./api.js";
import { startRun, pollRun } from "./runs.js";

const TOOLS = Object.freeze({
  refresh_discovery: { label: "Refresh discovery", required: ["host_id"] },
  refresh_readiness: { label: "Refresh readiness", required: ["host_id"] },
  create_patch_plan: { label: "Create patch plan", required: ["host_id", "plan_id", "patch_id", "database", "window_start", "window_end"] },
  create_backup: { label: "Create backup request", required: ["host_id", "request_id", "database", "backup_parent", "window_start", "window_end"] },
  analyze_backup: { label: "Analyze backup", required: ["request_id"] },
  execute_backup: { label: "Execute backup preparation", required: ["request_id"] },
  select_backup: { label: "Select backup for patch readiness", required: ["host_id", "request_id"] },
  dispatch_plan: { label: "Dispatch patch plan", required: ["plan_id"] },
  execute_plan: { label: "Execute patch plan", required: ["plan_id"] },
});
const LABELS = { host_id: "Host", plan_id: "Plan", request_id: "Backup request", database: "Database",
  patch_id: "Patch", backup_parent: "Backup parent", window_start: "Window start", window_end: "Window end" };
const text = value => typeof value === "string" ? value : JSON.stringify(value ?? null);
const identifier = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const endpoint = id => `/api/assistant/conversations/${encodeURIComponent(id)}`;

export function actionAvailability(action, { canChat = true, busy = false, now = Date.now() } = {}) {
  if (!TOOLS[action?.tool]) return { allowed: false, reason: "This action type is not supported by this application." };
  if (action.state !== "pending") return { allowed: false, reason: action.state === "unknown"
    ? "Outcome unknown. Inspect the existing native workflow before starting more work."
    : action.state === "executing" ? "This action is already running."
    : action.state === "expired" ? "This proposal has expired. Ask for a new proposal using current evidence." : "This action is no longer pending." };
  if (!identifier(action.id) || !/^[a-f0-9]{64}$/.test(action.digest || "")) return { allowed: false, reason: "The action identity or confirmation digest is missing or invalid. Refresh this conversation." };
  if (typeof action.expires_at !== "string" || !Number.isFinite(Date.parse(action.expires_at)) || Date.parse(action.expires_at) <= now) {
    return { allowed: false, reason: "This proposal has expired. Ask for a new proposal using current evidence." };
  }
  const args = action.arguments;
  if (!args || typeof args !== "object" || Array.isArray(args)
      || Object.keys(args).some(key => !TOOLS[action.tool].required.includes(key))
      || TOOLS[action.tool].required.some(key => typeof args[key] !== "string" || !args[key].trim())) {
    return { allowed: false, reason: "The proposed action is missing its exact target or required settings." };
  }
  if (!canChat) return { allowed: false, reason: "An authenticated, permitted session is required to confirm an action." };
  if (busy) return { allowed: false, reason: "Wait for the current operation or inspect its unresolved outcome." };
  return { allowed: true, reason: "Confirmation runs only the displayed action. Native approval and authorization checks still apply." };
}

export function actionLinks(action) {
  const args = action?.arguments || {}, links = [];
  if (identifier(args.host_id)) links.push({ label: "Open host workspace", href: `#/hosts/${encodeURIComponent(args.host_id)}/readiness` });
  if (identifier(args.plan_id) && (action.tool !== "create_patch_plan" || action.state === "completed")) {
    links.push({ label: "Review plan and approvals", href: `#/plans/${encodeURIComponent(args.plan_id)}` });
  }
  if (identifier(args.request_id) && (action.tool !== "create_backup" || action.state === "completed")) {
    links.push({ label: "Review backup and approvals", href: `#/recovery/${encodeURIComponent(args.request_id)}` });
  }
  return links;
}

export async function renderAssistant(mount, conversationId = null) {
  const signal = getReadSignal();
  mount.innerHTML = "";
  mount.appendChild(el("header", { class: "assistant-heading" }, [
    el("h1", { text: "Patching assistant" }),
    el("p", { text: "Ask about your databases, review readiness and prepare actions to confirm. Patch and backup approvals stay in their native workflows." }),
  ]));
  const notice = el("div", { class: "assistant-notice", role: "status", "aria-live": "polite" });
  const configPanel = el("section", { class: "panel assistant-config", "aria-label": "Assistant configuration" });
  mount.appendChild(configPanel);
  mount.appendChild(notice);
  const fail = error => {
    if (signal?.aborted || error.name === "AbortError") return;
    notice.setAttribute("role", "alert");
    notice.textContent = error.message || "The assistant request failed. Refresh to inspect its saved state.";
  };
  let config;
  try { config = await (await apiFetch("/api/assistant/config", { signal })).json(); }
  catch (error) {
    fail(error);
    configPanel.appendChild(el("p", { text: "Assistant configuration could not be loaded." }));
    const retry = el("button", { type: "button", text: "Reload assistant" });
    retry.addEventListener("click", () => renderAssistant(mount, conversationId));
    configPanel.appendChild(retry);
    return;
  }
  if (signal?.aborted) return;
  const canChat = config.enabled === true && config.configured === true && config.can_chat === true;
  configPanel.appendChild(el("h2", { text: "Local model" }));
  configPanel.appendChild(badge(canChat ? "Configured" : !config.enabled ? "Disabled" : !config.configured ? "Not configured" : "Session required", canChat ? "neutral" : "warn"));
  configPanel.appendChild(el("p", { text: `Provider: ${text(config.provider || "Not configured")} · Model: ${text(config.model || "Not selected")}` }));
  if (config.reason || !canChat) configPanel.appendChild(el("p", { text: config.reason || "Sign in with a permitted account to use the assistant." }));
  if (config.can_chat === false && config.reason) configPanel.appendChild(el("p", { text: "An authenticated, permitted company or service identity is required to use the assistant." }));
  const layout = el("div", { class: "assistant-layout" });
  const sidebar = el("aside", { class: "panel assistant-conversations", "aria-label": "Saved conversations" });
  sidebar.appendChild(el("h2", { text: "Conversations" }));
  const newButton = el("button", { type: "button", text: "New conversation" });
  newButton.disabled = !canChat;
  sidebar.appendChild(newButton);
  const list = el("nav", { "aria-label": "Conversation history" });
  sidebar.appendChild(list);
  const detail = el("section", { class: "assistant-detail", "aria-label": "Current conversation" });
  layout.appendChild(sidebar); layout.appendChild(detail); mount.appendChild(layout);
  let conversation = null, localBusy = false, uncertain = false, draft = "", revision = 0;
  const observed = new Set();
  const setNotice = value => { notice.setAttribute("role", "status"); notice.textContent = value; };
  const unresolved = () => (conversation?.actions || []).some(action => ["executing", "unknown"].includes(action.state));
  const busy = () => localBusy || uncertain || conversation?.busy === true;

  async function refreshList() {
    const data = await (await apiFetch("/api/assistant/conversations", { signal })).json();
    if (signal?.aborted) return;
    list.innerHTML = "";
    const rows = Array.isArray(data.conversations) ? data.conversations : [];
    if (!rows.length) list.appendChild(el("p", { class: "helper", text: "No saved conversations yet." }));
    for (const row of rows) {
      if (!identifier(row.id)) continue;
      const link = el("a", { href: `#/assistant/${encodeURIComponent(row.id)}`, class: "assistant-conversation-link",
        ...(row.id === conversationId ? { "aria-current": "page" } : {}) }, [
        el("span", { text: row.title || "Untitled conversation" }),
        el("time", { datetime: row.updated_at || "", text: row.updated_at || "" }),
      ]);
      list.appendChild(link);
    }
  }

  async function refreshConversation({ watch = true } = {}) {
    if (!conversationId) return;
    const current = ++revision;
    const data = await (await apiFetch(endpoint(conversationId), { signal })).json();
    if (signal?.aborted || current !== revision) return;
    if (data.conversation?.id !== conversationId) throw new Error("The server returned a different conversation. Refresh before continuing.");
    conversation = data.conversation;
    uncertain = false;
    renderConversation();
    if (watch && conversation.busy && identifier(conversation.active_run_id)) void watchRun(conversation.active_run_id);
  }

  async function watchRun(runId) {
    if (observed.has(runId)) return;
    observed.add(runId);
    try {
      const run = await pollRun(runId, { signal, onTick: record => {
        if (!signal?.aborted) setNotice(`Operation ${record.status}. Run ${runId}.`);
      } });
      if (run.status === "failed") throw new Error(run.error?.message || "The operation failed. Inspect its saved action and native workflow.");
      setNotice("Operation finished. Review the saved response and any native result below.");
    } catch (error) { fail(error); }
    finally {
      localBusy = false;
      if (!signal?.aborted) {
        try { await refreshConversation({ watch: false }); await refreshList(); }
        catch (error) { fail(error); }
      }
      observed.delete(runId);
    }
  }

  async function runOperation(url, body, { clearDraft = false } = {}) {
    localBusy = true;
    renderConversation();
    setNotice("Submitting the selected operation…");
    try {
      const runId = await startRun(url, body);
      if (clearDraft) draft = "";
      if (signal?.aborted) return;
      conversation = { ...conversation, busy: true, active_run_id: runId };
      try { await refreshConversation({ watch: false }); }
      catch (error) { fail(error); }
      await watchRun(runId);
    } catch (error) {
      localBusy = false;
      uncertain = !(error.status >= 400 && error.status < 500);
      fail(uncertain ? new Error(`${error.message || "Submission outcome is unknown."} Refresh this conversation to inspect whether the request was accepted before sending more work.`) : error);
      if (!signal?.aborted) renderConversation();
    }
  }

  function renderAction(action) {
    const availability = actionAvailability(action, { canChat, busy: busy() || unresolved() });
    const card = el("article", { class: "assistant-action panel", "aria-label": `Proposed action: ${TOOLS[action.tool]?.label || text(action.tool)}` });
    const title = el("div", { class: "assistant-action-heading" }, [
      el("h3", { text: TOOLS[action.tool]?.label || "Unsupported action" }),
      badge(action.state === "completed" ? "Operation finished" : action.state || "unknown", action.state === "completed" ? "neutral" : classifyStatus(action.state)),
    ]);
    card.appendChild(title);
    card.appendChild(el("p", { class: "assistant-action-summary", text: action.summary || "Review the exact action settings before confirming." }));
    const args = action.arguments && typeof action.arguments === "object" && !Array.isArray(action.arguments) ? action.arguments : {};
    const fields = el("dl", { class: "assistant-action-targets" });
    for (const [key, value] of Object.entries(args)) {
      fields.appendChild(el("dt", { text: LABELS[key] || key }));
      fields.appendChild(el("dd", { class: "mono", text: text(value) }));
    }
    card.appendChild(fields);
    if (action.expires_at) card.appendChild(el("p", { class: "helper", text: `Proposal expires: ${text(action.expires_at)}` }));
    const links = actionLinks(action);
    if (links.length) card.appendChild(el("nav", { class: "assistant-action-links", "aria-label": "Native workflow review" },
      links.map(link => el("a", { href: link.href, text: link.label }))));
    if (action.result !== undefined) {
      const resultState = action.result?.outcome?.status || action.result?.outcome?.plan_state || action.result?.outcome?.state || action.result?.status || action.result?.state;
      if (resultState) card.appendChild(el("p", {}, [document.createTextNode("Native result: "), badge(resultState, classifyStatus(resultState))]));
      card.appendChild(el("details", {}, [el("summary", { text: "Native operation result" }), el("pre", { text: JSON.stringify(action.result, null, 2) })]));
    }
    const actionError = action.error || action.result?.error;
    if (actionError) card.appendChild(el("p", { class: "assistant-action-error", text: typeof actionError === "string" ? actionError : actionError.message || JSON.stringify(actionError) }));
    if (["pending", "unknown", "executing", "expired"].includes(action.state)) card.appendChild(el("p", { class: "helper", text: availability.reason }));
    if (action.state === "pending") {
      const confirm = el("button", { type: "button", text: "Confirm and run" });
      confirm.disabled = !availability.allowed;
      confirm.addEventListener("click", async () => {
        const current = actionAvailability(action, { canChat, busy: busy() || unresolved() });
        if (!current.allowed) { setNotice(current.reason); renderConversation(); return; }
        await runOperation(`${endpoint(conversationId)}/actions/${encodeURIComponent(action.id)}/execute`, { digest: action.digest });
      });
      const dismiss = el("button", { type: "button", class: "button-secondary", text: "Dismiss proposal" });
      dismiss.disabled = !canChat || busy() || !identifier(action.id);
      dismiss.addEventListener("click", async () => {
        if (!canChat || busy()) return;
        localBusy = true; renderConversation();
        try {
          await apiFetch(`${endpoint(conversationId)}/actions/${encodeURIComponent(action.id)}/dismiss`, { method: "POST", body: "{}" });
          localBusy = false;
          if (!signal?.aborted) { await refreshConversation(); setNotice("Proposal dismissed."); }
        } catch (error) { localBusy = false; fail(error); if (!signal?.aborted) renderConversation(); }
      });
      card.appendChild(el("div", { class: "assistant-action-buttons" }, [confirm, dismiss]));
    }
    card.appendChild(el("details", { class: "assistant-action-identity" }, [el("summary", { text: "Action identity and confirmation digest" }),
      el("pre", { text: `Action: ${text(action.id)}\nTool: ${text(action.tool)}\nDigest: ${text(action.digest)}` })]));
    return card;
  }

  function renderConversation() {
    detail.innerHTML = "";
    if (!conversation) {
      detail.appendChild(el("div", { class: "panel assistant-empty" }, [el("h2", { text: "Start with your patching question" }),
        el("p", { text: "Choose a saved conversation or create a new one. You can ask which databases need attention, explain a readiness blocker or prepare a patch plan for review." })]));
      return;
    }
    const refresh = el("button", { type: "button", class: "button-secondary", text: "Refresh conversation" });
    refresh.addEventListener("click", async () => { try { await refreshConversation(); } catch (error) { fail(error); } });
    detail.appendChild(el("header", { class: "assistant-conversation-heading" }, [el("h2", { text: conversation.title || "Conversation" }), refresh]));
    const transcript = el("div", { class: "assistant-transcript", role: "log", "aria-label": "Conversation messages", "aria-live": "polite", "aria-relevant": "additions text" });
    for (const message of conversation.messages || []) {
      if (!["user", "assistant"].includes(message.role)) continue;
      transcript.appendChild(el("article", { class: `assistant-message assistant-message-${message.role}`, "aria-label": message.role === "user" ? "Your message" : "Assistant response" }, [
        el("p", { class: "assistant-message-author", text: message.role === "user" ? "You" : "Patching assistant" }),
        el("div", { class: "assistant-message-content", text: message.content || "" }),
        el("time", { datetime: message.created_at || "", text: message.created_at || "" }),
      ]));
    }
    if (!(conversation.messages || []).length) transcript.appendChild(el("p", { class: "helper", text: "No messages yet. Ask a question below." }));
    detail.appendChild(transcript);
    if ((conversation.actions || []).length) detail.appendChild(el("section", { class: "assistant-actions", "aria-label": "Actions to review" }, [
      el("h2", { text: "Actions and results" }), ...(conversation.actions || []).map(renderAction),
    ]));
    if (conversation.busy) detail.appendChild(el("p", { role: "status", text: conversation.active_run_id
      ? "An operation is in progress. Its existing run is being inspected; no action is resubmitted."
      : "An operation is in progress. Refresh this conversation to inspect its saved result." }));
    const form = el("form", { class: "panel assistant-composer", "aria-label": "Send a message" });
    const input = el("textarea", { rows: "4", value: draft, placeholder: "For example: Explain the readiness blockers for sourcedb", "aria-label": "Message", required: "", maxlength: "8000" });
    input.disabled = !canChat;
    input.addEventListener("input", () => { draft = input.value; });
    const send = el("button", { type: "submit", text: "Send message" });
    send.disabled = !canChat || busy();
    form.addEventListener("submit", async event => {
      event.preventDefault();
      draft = input.value;
      if (!canChat || busy()) return;
      if (!draft.trim()) { setNotice("Enter a message before sending."); return; }
      await runOperation(`${endpoint(conversationId)}/messages`, { content: draft.trim() }, { clearDraft: true });
    });
    input.addEventListener("keydown", event => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !send.disabled) { event.preventDefault(); form.requestSubmit(); }
    });
    form.appendChild(el("label", { class: "form-field" }, [el("span", { text: "Message" }), input]));
    form.appendChild(el("p", { class: "helper", text: "Enter adds a new line. Ctrl+Enter or ⌘+Enter sends your message." }));
    form.appendChild(send); detail.appendChild(form);
  }

  newButton.addEventListener("click", async () => {
    if (!canChat || newButton.disabled) return;
    newButton.disabled = true;
    try {
      const data = await (await apiFetch("/api/assistant/conversations", { method: "POST", body: "{}" })).json();
      if (!identifier(data.conversation?.id)) throw new Error("The server did not return a valid conversation identity.");
      if (!signal?.aborted) location.hash = `#/assistant/${encodeURIComponent(data.conversation.id)}`;
    } catch (error) { fail(error); }
    finally { newButton.disabled = !canChat; }
  });
  renderConversation();
  if (!canChat) return;
  try { await refreshList(); await refreshConversation(); }
  catch (error) { fail(error); }
}
