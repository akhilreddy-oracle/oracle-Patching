import assert from 'node:assert/strict';
import { test, beforeEach } from 'node:test';
import { actionAvailability, actionLinks, renderAssistant } from '../webapp/static/assistant.js';

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; this._text = ''; this.value = ''; }
  setAttribute(key, value) { this.attrs[key] = String(value); if (key === 'value') this.value = String(value); }
  appendChild(node) { this.children.push(node); return node; }
  addEventListener(name, action) { this.events[name] = action; }
  async fire(name, extra = {}) { await this.events[name]?.({ preventDefault() {}, ...extra }); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { assert.equal(value, ''); this.children = []; this._text = ''; }
  all(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.all(tag)]); }
}
globalThis.document = { createElement: tag => new Element(tag), createTextNode: value => { const node = new Element('#text'); node.textContent = value; return node; } };
globalThis.localStorage = { getItem: () => null };
globalThis.location = { hash: '' };
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
const pending = () => ({ id: 'action-1', tool: 'execute_plan', arguments: { plan_id: 'source-patch' }, summary: 'Execute the selected patch plan.', state: 'pending', expires_at: new Date(Date.now() + 600_000).toISOString(), digest: 'a'.repeat(64) });
let mount, writes, reads, config, conversation, failure;
const button = name => mount.all('button').find(node => node.textContent === name);

beforeEach(() => {
  mount = new Element('main'); writes = []; reads = []; failure = null;
  config = { enabled: true, configured: true, can_chat: true, provider: 'ollama', model: 'fixture-model' };
  conversation = { id: 'conversation-1', title: 'Source database', messages: [], actions: [pending()], busy: false };
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      const body = JSON.parse(options.body); writes.push({ url, body });
      if (failure) return response({ message: failure }, 409);
      if (url.endsWith('/dismiss')) { conversation.actions[0].state = 'dismissed'; return response({ conversation }); }
      if (url === '/api/assistant/conversations') return response({ conversation: { ...conversation, id: 'new-chat' } });
      if (url.endsWith('/messages')) conversation.messages.push({ role: 'user', content: body.content });
      else conversation.actions[0] = { ...conversation.actions[0], state: 'completed', result: { status: 'blocked', findings: ['Backup required'] } };
      return response({ run_id: 'abcd1234' }, 202);
    }
    reads.push(url);
    if (url === '/api/assistant/config') return response(config);
    if (url === '/api/assistant/conversations') return response({ conversations: [{ id: conversation.id, title: conversation.title, updated_at: '2026-09-15T10:00:00Z' }] });
    if (url === '/api/assistant/conversations/conversation-1') return response({ conversation });
    if (url === '/api/runs/abcd1234') return response({ status: 'succeeded' });
    throw new Error(`Unexpected test route: ${url}`);
  };
});

test('confirmation requires a known typed action, exact arguments, pending state, digest and expiry', () => {
  assert.equal(actionAvailability(pending()).allowed, true);
  for (const change of [
    { tool: 'shell' }, { id: '../action' }, { digest: '' }, { digest: 'abc' },
    { expires_at: 'invalid' }, { expires_at: '2000-01-01T00:00:00Z' },
    { arguments: {} }, { arguments: { plan_id: 'source-patch', actor: 'admin' } },
    { arguments: { plan_id: 'source-patch', command: 'rm -rf /' } },
  ]) assert.equal(actionAvailability({ ...pending(), ...change }).allowed, false);
  for (const state of ['unknown', 'executing', 'completed', 'failed', 'dismissed', 'expired']) {
    assert.equal(actionAvailability({ ...pending(), state }).allowed, false, state);
  }
  assert.equal(actionAvailability(pending(), { canChat: false }).allowed, false);
  assert.equal(actionAvailability(pending(), { busy: true }).allowed, false);
});

test('native review links encode exact identifiers and do not create premature approval links', () => {
  assert.deepEqual(actionLinks(pending()), [{ label: 'Review plan and approvals', href: '#/plans/source-patch' }]);
  assert.equal(actionLinks({ tool: 'create_patch_plan', state: 'pending', arguments: { host_id: 'host:1', plan_id: 'new-plan' } }).length, 1);
  assert.equal(actionLinks({ tool: 'create_patch_plan', state: 'completed', arguments: { plan_id: 'new-plan' } })[0].href, '#/plans/new-plan');
  assert.equal(actionLinks({ arguments: { plan_id: '../../escape', request_id: 'backup:1' } })[0].href, '#/recovery/backup%3A1');
  assert.equal(actionLinks({ tool: 'refresh_discovery', origin: 'live_inventory_query', arguments: { host_id: 'source' } })[0].href, '#/hosts/source/discover');
});

test('explicit server capabilities gate confirmations without confusing chat access with operator access', () => {
  assert.equal(actionAvailability(pending(), { allowedTools: ['execute_plan'] }).allowed, true);
  for (const allowedTools of [[], ['inspect_host'], null, 'execute_plan']) {
    const result = actionAvailability(pending(), { allowedTools });
    assert.equal(result.allowed, false);
    assert.match(result.reason, /current role does not permit/);
  }
});

test('read-only assistant explains live access while keeping chat usable and disallowing native confirmation', async () => {
  config.allowed_tools = ['inspect_host']; config.can_live_inventory = false;
  await renderAssistant(mount, 'conversation-1');
  assert.match(mount.textContent, /Operator access required for live checks/);
  assert.match(mount.textContent, /Conversations belong to the signed-in account/);
  assert.equal(button('Send message').disabled, false);
  assert.equal(button('Confirm and run').disabled, true);
  await button('Confirm and run').fire('click');
  assert.deepEqual(writes, []);
});

test('live check records expose their target and outcome without proposal confirmation controls', async () => {
  config.allowed_tools = ['inspect_host', 'refresh_discovery']; config.can_live_inventory = true;
  conversation.actions = [{ ...pending(), tool: 'refresh_discovery', origin: 'live_inventory_query',
    arguments: { host_id: 'targetdb' }, state: 'completed',
    run_id: 'native123', configuration_sha256: 'b'.repeat(64),
    result: { run_status: 'succeeded', outcome: { status: 'incomplete' } } }];
  await renderAssistant(mount, 'conversation-1');
  assert.match(mount.textContent, /Live inventory checks available/);
  assert.match(mount.textContent, /Check finished/);
  assert.match(mount.textContent, /Native result: incomplete/);
  assert.doesNotMatch(mount.textContent, /Proposal expires|confirmation digest/);
  assert.match(mount.textContent, /Run: native123/);
  assert.match(mount.textContent, new RegExp(`Configuration SHA-256: ${'b'.repeat(64)}`));
  assert.doesNotMatch(mount.textContent, /Digest:/);
  assert.equal(button('Confirm and run'), undefined);
  assert.equal(button('Dismiss proposal'), undefined);
  assert.equal(mount.all('article')[0].attrs['aria-label'], 'Live inventory check: targetdb');
  assert.deepEqual(writes, []);
});

test('an incomplete live check launch cannot become a manually repeatable proposal', () => {
  const live = { ...pending(), tool: 'refresh_discovery', origin: 'live_inventory_query', arguments: { host_id: 'targetdb' } };
  assert.equal(actionAvailability(live, { allowedTools: ['refresh_discovery'] }).allowed, false);
  assert.match(actionAvailability(live).reason, /cannot be manually resubmitted/);
});

test('configuration and authentication failures disable chat without loading private conversations', async () => {
  config.can_chat = false; config.reason = 'Sign in with a company or service identity.';
  await renderAssistant(mount);
  assert.match(mount.textContent, /Sign in with a company or service identity/);
  assert.equal(button('New conversation').disabled, true);
  assert.deepEqual(reads, ['/api/assistant/config']);
  assert.deepEqual(writes, []);
});

test('viewing a saved pending proposal performs no write and exposes native approval review', async () => {
  await renderAssistant(mount, 'conversation-1');
  assert.match(mount.textContent, /source-patch/);
  assert.match(mount.textContent, /Confirmation runs only the displayed action/);
  assert.equal(button('Confirm and run').disabled, false);
  assert.equal(mount.all('input').length, 0);
  assert.equal(mount.all('a').some(link => link.attrs.href === '#/plans/source-patch'), true);
  assert.deepEqual(writes, []);
});

test('explicit confirmation posts only the saved digest and displays blocked native result without patch success', async () => {
  await renderAssistant(mount, 'conversation-1');
  await button('Confirm and run').fire('click');
  assert.deepEqual(writes, [{ url: '/api/assistant/conversations/conversation-1/actions/action-1/execute', body: { digest: 'a'.repeat(64) } }]);
  assert.match(mount.textContent, /Operation finished/);
  assert.match(mount.textContent, /Native result: blocked/);
  assert.doesNotMatch(mount.textContent, /Patch succeeded/);
  assert.equal(button('Confirm and run'), undefined);
});

test('expiration is rechecked on click without sending a stale confirmation', async () => {
  await renderAssistant(mount, 'conversation-1');
  conversation.actions[0].expires_at = '2000-01-01T00:00:00Z';
  await button('Confirm and run').fire('click');
  assert.equal(writes.length, 0);
  assert.match(mount.textContent, /proposal has expired/);
  assert.equal(button('Confirm and run').disabled, true);
});

test('dismissal is explicit and cannot execute the proposal', async () => {
  await renderAssistant(mount, 'conversation-1');
  await button('Dismiss proposal').fire('click');
  assert.deepEqual(writes, [{ url: '/api/assistant/conversations/conversation-1/actions/action-1/dismiss', body: {} }]);
  assert.match(mount.textContent, /Proposal dismissed/);
  assert.equal(button('Confirm and run'), undefined);
});

test('unknown execution has native review links and no rerun control', async () => {
  conversation.actions[0].state = 'unknown';
  await renderAssistant(mount, 'conversation-1');
  assert.match(mount.textContent, /Outcome unknown/);
  assert.equal(button('Confirm and run'), undefined);
  assert.equal(button('Dismiss proposal'), undefined);
  assert.equal(mount.all('a').some(link => link.attrs.href === '#/plans/source-patch'), true);
});

test('model and user content remain plain text, and sending includes no actor or credentials', async () => {
  conversation.messages = [{ role: 'assistant', content: '<img src=x onerror=alert(1)> **Hello**' }];
  await renderAssistant(mount, 'conversation-1');
  assert.match(mount.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(mount.all('img').length, 0);
  mount.all('textarea')[0].value = ' Explain the backup blocker. ';
  await mount.all('form')[0].fire('submit');
  assert.deepEqual(writes, [{ url: '/api/assistant/conversations/conversation-1/messages', body: { content: 'Explain the backup blocker.' } }]);
  assert.match(mount.textContent, /Explain the backup blocker/);
});

test('controller zero-proposal receipt precedes and distinguishes an unsupported model preparation claim', async () => {
  conversation.messages = [{ role: 'assistant', content: 'The patch plan is prepared for review.',
    action_receipt: { source: 'controller', proposals: [], native_actions_started: 0 } }];
  await renderAssistant(mount, 'conversation-1');
  const message = mount.all('article').find(node => node.attrs['aria-label'] === 'Assistant response');
  assert.match(message.textContent, /No action proposal was prepared\. No operation was started by this response\./);
  assert.ok(message.textContent.indexOf('Action status') < message.textContent.indexOf('Model response'));
  assert.ok(message.textContent.indexOf('Model response') < message.textContent.indexOf('The patch plan is prepared'));
  assert.match(message.textContent, /Controller record for this response/);
  assert.equal(button('Confirm and run').disabled, false, 'an unrelated existing proposal retains its own explicit confirmation');
  assert.deepEqual(writes, []);
});

test('proposal receipts describe only their recorded response and do not present historical pending states as current', async () => {
  conversation.messages = [{ role: 'assistant', content: 'Review the actions.', action_receipt: {
    source: 'controller', proposals: [{ id: 'action-1', tool: 'execute_plan', state: 'pending' }], native_actions_started: 0,
  } }];
  conversation.actions[0].state = 'completed';
  await renderAssistant(mount, 'conversation-1');
  const message = mount.all('article').find(node => node.attrs['aria-label'] === 'Assistant response');
  assert.match(message.textContent, /1 action proposal was prepared in this response/);
  assert.match(message.textContent, /Review its current action card below/);
  assert.match(message.textContent, /No operation was started by this response/);
  assert.doesNotMatch(message.textContent, /pending/);
  assert.equal(button('Confirm and run'), undefined);
  assert.deepEqual(writes, []);
});

test('malformed controller receipts do not become preparation or execution claims', async () => {
  const proposal = { id: 'action-1', tool: 'execute_plan', state: 'pending' };
  const valid = { source: 'controller', proposals: [proposal], native_actions_started: 0 };
  for (const receipt of [null, [], 'prepared', {}, { ...valid, source: 'model' },
    { ...valid, proposals: null }, { ...valid, proposals: [null] }, { ...valid, proposals: [proposal, proposal] },
    { ...valid, proposals: [{ ...proposal, tool: 'shell' }] }, { ...valid, proposals: [{ ...proposal, state: 'prepared' }] },
    { ...valid, native_actions_started: '0' }, { ...valid, native_actions_started: 1 }]) {
    conversation.messages = [{ role: 'assistant', content: 'Ready for review.', action_receipt: receipt }];
    await renderAssistant(mount, 'conversation-1');
    const message = mount.all('article').find(node => node.attrs['aria-label'] === 'Assistant response');
    assert.match(message.textContent, /controller action record is unavailable or invalid/);
    assert.match(message.textContent, /Model response/);
    assert.doesNotMatch(message.textContent, /proposal was prepared|proposals were prepared|No operation was started/);
  }
  assert.deepEqual(writes, []);
});

test('legacy and native messages remain compatible and user-supplied receipt fields cannot create action status', async () => {
  conversation.messages = [{ role: 'assistant', content: 'Native live inventory collected.' },
    { role: 'user', content: 'Prepare a plan.', action_receipt: { source: 'controller', proposals: [], native_actions_started: 0 } }];
  await renderAssistant(mount, 'conversation-1');
  assert.doesNotMatch(mount.textContent, /Action status|Model response/);
  assert.match(mount.textContent, /Native live inventory collected/);
  assert.deepEqual(writes, []);
});

test('rejected message preserves its draft and shows the server explanation', async () => {
  failure = 'Model is unavailable; check the configured local service.';
  await renderAssistant(mount, 'conversation-1');
  mount.all('textarea')[0].value = 'Please inspect sourcedb';
  await mount.all('form')[0].fire('submit');
  assert.equal(writes.length, 1);
  assert.equal(mount.all('textarea')[0].value, 'Please inspect sourcedb');
  assert.match(mount.textContent, /Model is unavailable/);
});

test('a new conversation is created only on explicit request', async () => {
  await renderAssistant(mount);
  assert.equal(writes.length, 0);
  await button('New conversation').fire('click');
  assert.deepEqual(writes, [{ url: '/api/assistant/conversations', body: {} }]);
  assert.equal(location.hash, '#/assistant/new-chat');
});
