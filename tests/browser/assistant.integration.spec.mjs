import { test, expect } from '@playwright/test';

// Actual HTTP authentication, local-model protocol client, private conversations,
// typed proposals and native fixture executors. The model and Oracle binaries
// are deterministic simulators. No browser API response is intercepted.
const origin = 'http://127.0.0.1:18766';
const planId = 'assistant-native-patch';
const main = page => page.locator('#app');
const headers = { Authorization: 'Bearer integration-operator-token' };

async function signIn(page, role) {
  await page.getByLabel('API token', { exact: true }).fill(`integration-${role}-token`);
  await expect(page.locator('#rail-session')).toContainText(`Authenticated as ${role}. Roles: ${role}.`);
  await expect(page.getByRole('textbox', { name: 'Acting as', exact: true })).toHaveAttribute('readonly', '');
}

async function read(page, path) {
  const response = await page.request.get(`${origin}${path}`, { headers });
  expect(response.ok()).toBe(true);
  return response.json();
}

async function nativeAction(page, label, key) {
  const [response] = await Promise.all([
    page.waitForResponse(async response => {
      if (!response.url().startsWith(`${origin}/api/runs/`) || response.status() !== 200) return false;
      const run = await response.json();
      return run.key === key && ['succeeded', 'failed', 'unknown'].includes(run.status);
    }, { timeout: 45_000 }),
    main(page).getByRole('button', { name: label, exact: true }).click(),
  ]);
  expect((await response.json()).status).toBe('succeeded');
}

test('actual controller chat proposes without execution, preserves approval blockers, then runs approved native fixture tasks only on confirmation', async ({ page }, testInfo) => {
  test.setTimeout(300_000);
  const errors = [], writes = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname.startsWith('/api/') && request.method() !== 'GET') writes.push({ path: url.pathname, body: request.postDataJSON() });
  });
  await page.route(/^https?:\/\/(?!127\.0\.0\.1:18766(?:\/|$))/, route => {
    const url = new URL(route.request().url());
    if (!['fonts.googleapis.com', 'fonts.gstatic.com'].includes(url.hostname)) external.push(url.origin);
    return route.abort('blockedbyclient');
  });
  await page.goto('/#/plans/demo-new');
  await signIn(page, 'requester');
  await main(page).getByLabel('Plan ID', { exact: true }).fill(planId);
  await main(page).getByRole('button', { name: 'Build fixture and create plan', exact: true }).click();
  await expect(main(page).getByRole('heading', { name: planId, exact: true })).toBeVisible({ timeout: 45_000 });
  await signIn(page, 'operator');
  await page.getByRole('link', { name: 'Patching assistant', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Assistant configuration' })).toContainText('deterministic-controller-fixture');
  await page.getByRole('button', { name: 'New conversation', exact: true }).click();
  await expect(page).toHaveURL(/#\/assistant\/[a-f0-9]+$/);
  const conversationId = new URL(page.url()).hash.split('/').at(-1);
  const chatPath = `/api/assistant/conversations/${conversationId}`;
  await page.getByRole('textbox', { name: 'Message', exact: true }).fill(`Inspect ${planId} and propose executing its remaining tasks.`);
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByRole('log')).toContainText('Fixture model inspected the saved plan', { timeout: 30_000 });
  let conversation = (await read(page, chatPath)).conversation;
  expect(conversation.actions).toHaveLength(1);
  expect(conversation.actions[0]).toMatchObject({ tool: 'execute_plan', arguments: { plan_id: planId }, state: 'pending' });
  expect((await read(page, `/api/plans/${planId}`)).state).toBe('awaiting_approval');
  const beforeTasks = (await read(page, `/api/plans/${planId}/tasks`)).tasks;
  expect(beforeTasks.every(task => task.status === 'pending')).toBe(true);
  expect(writes.filter(row => row.path.endsWith('/execute'))).toHaveLength(0);

  // Confirmation is not native approval. The real executor returns without
  // running tasks because this plan still awaits its independent approver.
  await page.getByRole('button', { name: 'Confirm and run', exact: true }).click();
  await expect(page.locator('.assistant-action')).toContainText('Native result: awaiting_approval', { timeout: 30_000 });
  conversation = (await read(page, chatPath)).conversation;
  expect(conversation.actions[0]).toMatchObject({ state: 'completed', result: { outcome: { executed_count: 0, plan_state: 'awaiting_approval' } } });
  expect((await read(page, `/api/plans/${planId}/tasks`)).tasks).toEqual(beforeTasks);
  await page.reload();
  await expect(page.locator('.assistant-action')).toContainText('Native result: awaiting_approval');
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toHaveCount(0);

  await page.getByRole('link', { name: 'Review plan and approvals', exact: true }).click();
  await signIn(page, 'approver');
  await main(page).getByLabel('Ticket', { exact: true }).fill('SIMULATED-CHAT-NATIVE-APPROVAL');
  await nativeAction(page, 'Approve', `plan:${planId}:approve`);
  await expect(main(page).getByRole('heading', { name: 'Authorize', exact: true })).toBeVisible();
  await signIn(page, 'operator');
  await nativeAction(page, 'Authorize', `plan:${planId}:authorize`);
  await expect(main(page).getByRole('heading', { name: 'Dispatch', exact: true })).toBeVisible();
  await nativeAction(page, 'Dispatch', `plan:${planId}:dispatch`);
  await expect(main(page).getByRole('button', { name: 'Execute remaining tasks', exact: true })).toBeVisible();

  await page.goto(`/#/assistant/${conversationId}`);
  await expect(page.getByRole('log')).toContainText('Fixture model inspected the saved plan');
  await page.getByRole('textbox', { name: 'Message', exact: true }).fill(`Inspect ${planId} again and propose the current remaining tasks.`);
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toBeEnabled({ timeout: 30_000 });
  conversation = (await read(page, chatPath)).conversation;
  expect(conversation.actions).toHaveLength(2);
  expect(conversation.actions[1].state).toBe('pending');
  expect((await read(page, `/api/plans/${planId}/tasks`)).tasks.every(task => task.status === 'pending')).toBe(true);
  await page.getByRole('button', { name: 'Confirm and run', exact: true }).click();
  await expect.poll(async () => (await read(page, `/api/plans/${planId}`)).state, { timeout: 150_000 }).toBe('succeeded');
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeEnabled({ timeout: 30_000 });
  await page.reload();
  await expect(page.getByRole('button', { name: 'Confirm and run', exact: true })).toHaveCount(0);
  conversation = (await read(page, chatPath)).conversation;
  expect(conversation.actions[1].state).toBe('completed');
  const tasks = (await read(page, `/api/plans/${planId}/tasks`)).tasks;
  expect(tasks).toHaveLength(5);
  expect(tasks.every(task => task.status === 'succeeded')).toBe(true);
  expect(writes.filter(row => row.path.startsWith(chatPath) && row.path.endsWith('/execute'))).toHaveLength(2);
  for (const row of writes.filter(row => row.path.startsWith(chatPath))) {
    expect(row.body).not.toHaveProperty('actor'); expect(row.body).not.toHaveProperty('requester');
  }
  expect(errors).toEqual([]); expect(external).toEqual([]);
  await testInfo.attach('actual-chat-controller-native-fixture', { body: JSON.stringify({
    boundary: 'Actual HTTP/controller/local-model protocol/native tools; deterministic model and Oracle simulators. No live-lab proof.',
    conversation, tasks,
  }, null, 2), contentType: 'application/json' });
  await page.screenshot({ path: testInfo.outputPath('assistant-native-fixture-completed.png'), fullPage: true });
});
