import { test, expect } from './fixtures.mjs';

// UI contract fixtures: no SSH, Oracle process or real model is used here.
const now = () => new Date().toISOString();
const answer = 'Live inventory check completed for source. Collected at 2026-09-17T08:30:00Z. Oracle Home: /fixture/oracle/dbhome_1. Binary patch IDs: 29517242, 39034528.';
const liveAction = (state = 'executing') => ({ id: 'live-1', tool: 'refresh_discovery', origin: 'live_inventory_query',
  arguments: { host_id: 'source' }, state, digest: 'a'.repeat(64), expires_at: new Date(Date.now() + 600_000).toISOString(), run_id: 'live123' });

function install(fixture, { operator = true, resumed = false, outcome = 'succeeded', existingProposal = false } = {}) {
  const state = { polls: 0, terminal: false, conversation: { id: 'live-chat', title: 'Current source inventory',
    messages: resumed ? [{ role: 'user', content: 'Check the current patch inventory on source', created_at: now() }] : [],
    actions: resumed ? [liveAction()] : [], busy: resumed, active_run_id: resumed ? 'live123' : null } };
  if (existingProposal) state.conversation.actions = [{ ...liveAction('pending'), origin: undefined, tool: 'execute_plan', arguments: { plan_id: 'source-plan' } }];
  fixture.session = { rbac_enabled: true, actor: operator ? 'fixture-operator' : 'fixture-viewer', roles: [operator ? 'operator' : 'viewer'] };
  fixture.custom = async ({ url, method, request, send: respond }) => {
    const send = async (...args) => { await respond(...args); return true; };
    const path = url.pathname;
    if (method === 'GET' && path === '/api/assistant/config') return send({ enabled: true, configured: true, can_chat: true,
      provider: 'ollama', model: 'deterministic-live-ui-fixture', can_live_inventory: operator,
      allowed_tools: operator ? ['inspect_host', 'refresh_discovery', 'execute_plan'] : ['inspect_host'] });
    if (method === 'GET' && path === '/api/assistant/conversations') return send({ conversations: [{ id: 'live-chat', title: state.conversation.title, updated_at: now() }] });
    if (method === 'GET' && path === '/api/assistant/conversations/live-chat') {
      if (state.terminal && state.conversation.busy) {
        state.conversation.busy = false; state.conversation.active_run_id = null;
        const action = state.conversation.actions[0];
        action.state = outcome === 'succeeded' ? 'completed' : outcome;
        action.result = { run_id: 'live123', run_status: outcome, outcome: { status: outcome === 'succeeded' ? 'complete' : outcome } };
        state.conversation.messages.push({ role: 'assistant', content: outcome === 'succeeded' ? answer
          : 'Live inventory could not be verified. Existing saved observations are not a current result. Inspect the native discovery outcome before starting another check.', created_at: now() });
      }
      return send({ conversation: state.conversation });
    }
    if (method === 'POST' && path === '/api/assistant/conversations/live-chat/messages') {
      state.conversation.messages.push({ role: 'user', content: request.postDataJSON().content, created_at: now() });
      state.conversation.actions = [liveAction()]; state.conversation.busy = true; state.conversation.active_run_id = 'live123';
      return send({ run_id: 'live123' }, 202);
    }
    if (method === 'GET' && path === '/api/runs/live123') {
      if (state.polls++ === 0) return send({ run_id: 'live123', status: 'running' });
      state.terminal = true;
      return send({ run_id: 'live123', status: outcome, ...(outcome === 'failed' ? { error: { message: 'Fixture discovery failed' } } : {}) });
    }
    return false;
  };
  return state;
}

for (const width of [1280, 390]) {
  test(`current inventory question displays its live result without a second confirmation at ${width}px`, async ({ page, fixture }) => {
    install(fixture);
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/#/assistant/live-chat');
    await expect(page.getByRole('region', { name: 'Assistant configuration' })).toContainText('Live inventory checks available');
    await page.getByRole('textbox', { name: 'Message', exact: true }).fill('Check the current patch inventory on source');
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    const card = page.getByRole('article', { name: 'Live inventory check: source', exact: true });
    await expect(card).toBeVisible();
    await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toHaveCount(0);
    await expect(page.getByRole('log')).toContainText(answer);
    await expect(card).toContainText('Check finished');
    await expect(card).not.toContainText('Proposal expires');
    expect(fixture.writes).toEqual([{ path: '/api/assistant/conversations/live-chat/messages', method: 'POST', body: { content: 'Check the current patch inventory on source' } }]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.reload();
    await expect(page.getByRole('log')).toContainText(answer);
    await expect(page.getByRole('article', { name: 'Assistant response', exact: true })).toHaveCount(1);
    expect(fixture.writes).toHaveLength(1);
  });
}

test('viewer can chat but sees operator guidance and cannot confirm a retained native proposal', async ({ page, fixture }) => {
  install(fixture, { operator: false, existingProposal: true });
  await page.goto('/#/assistant/live-chat');
  await expect(page.getByRole('region', { name: 'Assistant configuration' })).toContainText('Operator access required for live checks');
  await expect(page.getByRole('region', { name: 'Assistant configuration' })).toContainText('Conversations belong to the signed-in account');
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toBeDisabled();
  await expect(page.locator('.assistant-action')).toContainText('Your current role does not permit this action');
  expect(fixture.writes).toEqual([]);
});

test('returning to a running live check observes its existing run and appends one answer', async ({ page, fixture }) => {
  install(fixture, { resumed: true });
  await page.goto('/#/assistant/live-chat');
  await expect(page.getByRole('log')).toContainText(answer);
  await page.getByRole('button', { name: 'Refresh conversation', exact: true }).click();
  await expect(page.getByRole('article', { name: 'Assistant response', exact: true })).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled();
  expect(fixture.writes).toEqual([]);
});

for (const outcome of ['failed', 'unknown']) {
  test(`${outcome} live check does not present cached patches as current or offer a replay button`, async ({ page, fixture }) => {
    install(fixture, { resumed: true, outcome });
    await page.goto('/#/assistant/live-chat');
    await expect(page.getByRole('log')).toContainText('Live inventory could not be verified');
    await expect(page.getByRole('log')).not.toContainText('29517242');
    await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Dismiss proposal', exact: true })).toHaveCount(0);
    await expect(page.getByRole('article', { name: 'Live inventory check: source', exact: true })).toContainText(outcome);
    expect(fixture.writes).toEqual([]);
  });
}
