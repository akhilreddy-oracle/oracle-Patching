import { test, expect } from './fixtures.mjs';

// UI contract fixtures only: this file does not exercise a real model/backend.
const digest = 'a'.repeat(64);
const now = () => new Date().toISOString();
const proposal = (overrides = {}) => ({ id: 'action-1', tool: 'execute_plan', arguments: { plan_id: 'source-plan' },
  summary: 'Execute the remaining tasks in source-plan. Native approvals remain required.',
  state: 'pending', expires_at: new Date(Date.now() + 600_000).toISOString(), digest, ...overrides });

function install(fixture, options = {}) {
  const state = { enabled: true, configured: true, can_chat: true, busyPolls: 0, ...options };
  state.conversation ||= { id: 'chat-1', title: 'Source database review', messages: [], actions: [], busy: false };
  fixture.session = { rbac_enabled: true, actor: 'fixture-operator', roles: ['operator'] };
  fixture.custom = async ({ url, method, request, send: respond, route }) => {
    const send = async (...args) => { await respond(...args); return true; };
    const path = url.pathname;
    if (method === 'GET' && path === '/api/assistant/config') return send({ ...state, provider: 'ollama', model: 'deterministic-ui-fixture', reason: state.reason || null });
    if (method === 'GET' && path === '/api/assistant/conversations') return send({ conversations: [{ id: 'chat-1', title: state.conversation.title, updated_at: now() }] });
    if (method === 'POST' && path === '/api/assistant/conversations') return send({ conversation: state.conversation });
    if (method === 'GET' && path === '/api/assistant/conversations/chat-1') return send({ conversation: state.conversation });
    if (method === 'POST' && path.endsWith('/chat-1/messages')) {
      if (state.rejectMessage) return send({ message: state.rejectMessage }, 503);
      state.conversation.messages.push({ role: 'user', content: request.postDataJSON().content, created_at: now() });
      state.conversation.busy = true; state.conversation.active_run_id = 'model123'; state.mode = 'message';
      return send({ run_id: 'model123' }, 202);
    }
    if (method === 'GET' && path === '/api/runs/model123') {
      if (state.busyPolls++ === 0 && !state.finishImmediately) return send({ status: 'running', run_id: 'model123' });
      if (state.conversation.busy) {
        state.conversation.actions = state.proseOnly ? [] : [proposal()];
        state.conversation.messages.push({ role: 'assistant', content: state.proseOnly
          ? 'The patch plan is prepared for review.' : 'Review this proposal before confirming. <img src=x onerror=alert(1)>', created_at: now(),
          action_receipt: { source: 'controller', native_actions_started: 0,
            proposals: state.conversation.actions.map(({ id, tool, state: actionState }) => ({ id, tool, state: actionState })) } });
        state.conversation.busy = false; state.conversation.active_run_id = null;
      }
      return send({ status: 'succeeded', run_id: 'model123' });
    }
    if (method === 'POST' && path.endsWith('/actions/action-1/execute')) {
      if (state.disconnectExecute) { state.conversation.actions[0].state = 'unknown'; await route.abort('failed'); return true; }
      state.conversation.actions[0] = { ...state.conversation.actions[0], state: 'completed',
        result: { run_status: 'succeeded', outcome: { status: 'blocked', reason: 'Independent approval is required.' } } };
      return send({ run_id: 'native123' }, 202);
    }
    if (method === 'GET' && path === '/api/runs/native123') return send({ status: 'succeeded', run_id: 'native123' });
    if (method === 'POST' && path.endsWith('/actions/action-1/dismiss')) {
      state.conversation.actions[0].state = 'dismissed'; return send({ conversation: state.conversation });
    }
    return false;
  };
  return state;
}

test('assistant navigation, saved message and explicit confirmation retain a blocked native result after reload', async ({ page, fixture }, testInfo) => {
  install(fixture);
  await page.goto('/#/assistant');
  await expect(page.getByRole('link', { name: 'Patching assistant', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', { name: 'Patching assistant', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Assistant configuration' })).toContainText('deterministic-ui-fixture');
  await page.getByRole('button', { name: 'New conversation', exact: true }).click();
  await expect(page).toHaveURL(/#\/assistant\/chat-1$/);
  await expect(page.getByRole('textbox', { name: 'Acting as', exact: true })).toHaveAttribute('readonly', '');
  const message = page.getByRole('textbox', { name: 'Message', exact: true });
  await message.fill('Prepare the approved source plan for execution.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByRole('log', { name: 'Conversation messages' })).toContainText('Review this proposal');
  await expect(page.getByRole('region', { name: 'Action status', exact: true })).toContainText('1 action proposal was prepared in this response');
  await expect(page.locator('.assistant-transcript img')).toHaveCount(0);
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/assistant/conversations', '/api/assistant/conversations/chat-1/messages']);
  await expect(page.getByRole('link', { name: 'Review plan and approvals' })).toHaveAttribute('href', '#/plans/source-plan');
  const confirm = page.getByRole('button', { name: 'Confirm and run', exact: true });
  await confirm.focus(); await page.keyboard.press('Enter');
  await expect(page.locator('.assistant-action')).toContainText('Native result: blocked');
  await expect(page.locator('.assistant-action')).toContainText('Operation finished');
  await expect(confirm).toHaveCount(0);
  expect(fixture.writes.at(-1)).toMatchObject({ path: '/api/assistant/conversations/chat-1/actions/action-1/execute', body: { digest } });
  await page.reload();
  await expect(page.locator('.assistant-action')).toContainText('Native result: blocked');
  await expect(page.getByRole('log')).toContainText('Prepare the approved source plan');
  await expect(page.getByRole('region', { name: 'Action status', exact: true })).toContainText('No operation was started by this response');
  expect(fixture.writes).toHaveLength(3);
  await page.screenshot({ path: testInfo.outputPath('assistant-fixture-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('assistant-fixture-mobile.png'), fullPage: true });
});

test('controller receipt distinguishes model-only preparation prose before and after reload without starting an action', async ({ page, fixture }) => {
  install(fixture, { proseOnly: true, finishImmediately: true });
  await page.goto('/#/assistant/chat-1');
  await page.getByRole('textbox', { name: 'Message', exact: true }).fill('Prepare a patch plan for review.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  const status = page.getByRole('region', { name: 'Action status', exact: true });
  await expect(status).toContainText('No action proposal was prepared. No operation was started by this response.');
  await expect(page.getByRole('heading', { name: 'Model response', exact: true })).toBeVisible();
  await expect(page.locator('.assistant-message-content').last()).toHaveText('The patch plan is prepared for review.');
  expect(await status.evaluate(node => Boolean(node.compareDocumentPosition(node.parentElement.querySelector('.assistant-message-content')) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toHaveCount(0);
  await expect(page.locator('.assistant-action')).toHaveCount(0);
  await page.reload();
  await expect(status).toContainText('No action proposal was prepared. No operation was started by this response.');
  expect(fixture.writes).toEqual([{ path: '/api/assistant/conversations/chat-1/messages', method: 'POST', body: { content: 'Prepare a patch plan for review.' } }]);
});

test('disabled local model explains configuration and never creates a conversation', async ({ page, fixture }) => {
  install(fixture, { configured: false, can_chat: false, reason: 'Configure the local model service before starting a conversation.' });
  await page.goto('/#/assistant');
  await expect(page.getByRole('region', { name: 'Assistant configuration' })).toContainText('Configure the local model service');
  await expect(page.getByRole('button', { name: 'New conversation', exact: true })).toBeDisabled();
  expect(fixture.writes).toEqual([]);
});

test('expired and unknown proposals cannot run, while dismissal remains explicit', async ({ page, fixture }) => {
  const state = install(fixture, { conversation: { id: 'chat-1', title: 'Saved work', messages: [], busy: false,
    actions: [proposal({ expires_at: '2000-01-01T00:00:00Z' }), proposal({ id: 'unknown-action', state: 'unknown', arguments: { plan_id: 'interrupted-plan' } })] } });
  await page.goto('/#/assistant/chat-1');
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toBeDisabled();
  await expect(page.locator('.assistant-actions')).toContainText('Outcome unknown');
  await page.getByRole('button', { name: 'Dismiss proposal', exact: true }).click();
  await expect(page.locator('.assistant-actions')).toContainText('dismissed');
  expect(state.conversation.actions[0].state).toBe('dismissed');
  expect(fixture.writes.map(row => row.path)).toEqual(['/api/assistant/conversations/chat-1/actions/action-1/dismiss']);
});

test('reload observes an existing model run without resending the message', async ({ page, fixture }) => {
  install(fixture, { finishImmediately: true, conversation: { id: 'chat-1', title: 'In progress', busy: true, active_run_id: 'model123',
    messages: [{ role: 'user', content: 'Explain readiness', created_at: now() }], actions: [] } });
  await page.goto('/#/assistant/chat-1');
  await expect(page.getByRole('log')).toContainText('Review this proposal');
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled();
  expect(fixture.writes).toEqual([]);
});

test('a lost confirmation response cannot be blindly resubmitted', async ({ page, fixture }) => {
  install(fixture, { disconnectExecute: true, conversation: { id: 'chat-1', title: 'Pending confirmation', busy: false, messages: [], actions: [proposal()] } });
  await page.goto('/#/assistant/chat-1');
  await page.getByRole('button', { name: 'Confirm and run', exact: true }).click();
  await expect(page.locator('.assistant-notice')).toContainText('inspect whether the request was accepted');
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Refresh conversation', exact: true }).click();
  await expect(page.locator('.assistant-action')).toContainText('Outcome unknown');
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toHaveCount(0);
  expect(fixture.writes).toHaveLength(1);
});
