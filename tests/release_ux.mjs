import assert from 'node:assert/strict';
import { test, beforeEach } from 'node:test';
import fs from 'node:fs';

// Small DOM double for the APIs this framework-free UI uses. No browser,
// managed hosts, external packages, or server process are required.
class Element {
  constructor(tag, text = '') {
    this.tagName = tag.toLowerCase(); this.children = []; this.attributes = {};
    this.style = {}; this.listeners = new Map(); this._text = text; this._value = undefined;
    this.className = ''; this.hidden = false;
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
      toggle: (name, enabled) => { if (enabled ?? !this.className.split(/\s+/).includes(name)) this.classList.add(name); else this.classList.remove(name); },
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  get isConnected() { return this === document.body || Boolean(this.parentElement?.isConnected); }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name === 'value' && this.tagName !== 'textarea') this._value = String(value);
    if (name === 'readonly') this.readOnly = true;
    if (name === 'checked') this.checked = true;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  get id() { return this.attributes.id; }
  get value() { return this._value ?? (this.tagName === 'select' ? this.children.find(child => child.tagName === 'option')?.value ?? '' : ''); }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value ?? ''); }
  set innerHTML(value) { assert.equal(value, '', 'The UI must render data as text'); this.replaceChildren(); }
  appendChild(child) { if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(item => item !== child); child.parentElement = this; this.children.push(child); return child; }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ''; children.forEach(child => this.appendChild(child)); }
  addEventListener(name, listener) { const handlers = this.listeners.get(name) || []; handlers.push(listener); this.listeners.set(name, handlers); }
  async fire(name) { for (const listener of this.listeners.get(name) || []) await listener({ target: this, preventDefault() {} }); }
  focus() { document.activeElement = this; }
  select() {}
  scrollIntoView() {}
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    return this.tagName === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(item => item.trim()); const found = [];
    const walk = node => { for (const child of node.children) { if (selectors.some(item => child.matches(item))) found.push(child); walk(child); } };
    walk(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

globalThis.document = {
  body: null, activeElement: null, createElement: tag => new Element(tag), createTextNode: text => new Element('#text', text),
  getElementById(id) { return this.body.querySelector(`#${id}`); },
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
};
document.body = new Element('body');
globalThis.window = new EventTarget();
window.setTimeout = setTimeout;
globalThis.location = { hash: '#/estate', replace(value) { this.hash = value; window.dispatchEvent(new Event('hashchange')); } };
const storage = new Map();
globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) };
const response = (data, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const mount = () => document.body.appendChild(new Element('main'));
const field = (container, title) => {
  const label = container.querySelectorAll('label').find(label => label.children[0]?.textContent === title);
  assert.ok(label, `Missing field ${title}`); return label.children[1];
};
const button = (container, text) => { const found = container.querySelectorAll('button').find(item => item.textContent === text); assert.ok(found, `Missing button ${text}`); return found; };

const { readinessBlockers, blockerCards } = await import('../webapp/static/readiness_blockers.js');
const { normalizeFleet, filterFleet, renderFleet } = await import('../webapp/static/fleet.js');
const { wizardContext, WIZARD_STAGES, maintenanceWindowError } = await import('../webapp/static/patch_wizard.js');
const { renderRecoveryStage } = await import('../webapp/static/stages/recovery.js');
const api = await import('../webapp/static/api.js');
const actor = await import('../webapp/static/actor.js');
beforeEach(() => {
  document.body.replaceChildren(); storage.clear();
  storage.set('opu-webapp-token', 'fixture-token'); api.setReadSignal(undefined); actor.setSessionIdentity(null); actor.setActor('fixture-operator');
});

const snapshot = {
  host: { name: 'source.example.test' }, collected_at: '2026-09-15T10:00:00Z',
  databases: [{ db_unique_name: 'ORCL', oracle_home: '/u01/db', runtime: { backup_age_minutes: 0, invalid_objects: 7, fra_space_limit_bytes: 1000, fra_space_used_bytes: 1000 } }],
  oracle_homes: [{ path: '/u01/db', patches: ['39034528'] }],
};
const policy = { schema_version: '1.0', maximum_snapshot_age_seconds: 1800, require_xml_inventory: true, recovery: { require_backup: true, max_backup_age_minutes: 1440, minimum_fra_free_bytes: 500, require_guaranteed_restore_point: false }, database: { maximum_invalid_objects: 0, require_primary_read_write: true } };
const steps = [{ step: 'discovery', evidence: snapshot }, { step: 'readiness-evaluate', input: policy }];

test('readiness findings retain zero values, affected database/home and actionable real routes', () => {
  const evidence = { patch_id: '39034528', gates: [
    { name: 'recovery_fra', status: 'blocker', detail: 'source database ORCL has insufficient FRA free capacity.' },
    { name: 'database_invalid_objects', status: 'blocker', detail: 'source database ORCL has excessive invalid-object evidence.' },
    { name: 'recovery_backup', status: 'blocker', detail: 'source database ORCL has no backup within policy age.' },
    { name: 'patch_not_installed', status: 'blocker', detail: 'source already has patch 39034528 in /u01/db.' },
  ] };
  const findings = readinessBlockers(evidence, steps, 'sourcedb');
  assert.equal(findings[0].actual, '0 bytes free'); assert.equal(findings[0].required, 'At least 500 bytes free');
  assert.equal(findings[0].database, 'ORCL'); assert.equal(findings[0].home, '/u01/db');
  assert.equal(findings[1].required, 'Invalid objects ≤ 0'); assert.equal(findings[2].actual, '0 minutes old');
  assert.equal(findings[2].action.href, '#/hosts/sourcedb/recovery');
  assert.match(findings[3].actual, /39034528/); assert.equal(findings[3].required, 'Patch 39034528 absent before apply');
  const card = blockerCards(evidence, steps, 'sourcedb'); assert.match(card.textContent, /Actual0 bytes freeRequiredAt least 500 bytes free/);
  assert.match(card.textContent, /cached discovery: 2026-09-15T10:00:00Z/);
});

test('missing measurements and another RAC node never borrow values or invent thresholds', () => {
  const evidence = { gates: [
    { name: 'recovery_fra', status: 'blocker', detail: 'other-node database ORCL has insufficient FRA free capacity.' },
    { name: 'database_invalid_objects', status: 'blocker', detail: 'source database ABSENT has missing evidence.' },
    { name: 'recovery_backup', status: 'blocker', detail: 'source database ORCL has no backup within policy age.' },
  ] };
  const findings = readinessBlockers(evidence, [{ step: 'discovery', evidence: snapshot }], 'sourcedb');
  assert.match(findings[0].actual, /^Unknown/); assert.match(findings[1].actual, /^Unknown/);
  assert.match(findings[2].required, /^Unknown/); assert.equal(findings[0].observedAt, null);
});

test('fleet prioritizes blockers, preserves unknowns and removes stale green compliance', () => {
  const rows = [
    { host_id: 'fresh', database: 'A', environment: 'test', oracle_version: '19.31', patch_baseline: '39034528', evidence_status: 'fresh', baseline_status: 'compliant', backup_status: 'fresh', readiness: 'ready_for_approval' },
    { host_id: 'stale', database: 'B', environment: 'prod', evidence_status: 'stale', baseline_status: 'compliant', backup_status: 'fresh', readiness: 'ready_for_approval' },
    { host_id: 'blocked', database: 'C', environment: 'prod', evidence_status: 'fresh', baseline_status: 'behind', backup_status: 'missing', readiness: 'blocked' },
    { host_id: 'unknown', database: 'D', evidence_status: 'invented_success', backup_status: 'invented_success' },
  ];
  const normalized = normalizeFleet(rows);
  assert.equal(normalized[1].baseline_status, 'unknown'); assert.equal(normalized[1].readiness, 'unknown');
  assert.equal(normalized[3].backup_status, 'unknown'); assert.equal(normalized[3].oracle_version, 'unknown');
  assert.deepEqual(filterFleet(rows).map((row) => row.host_id), ['blocked', 'stale', 'unknown', 'fresh']);
  assert.deepEqual(filterFleet(rows, { environment: 'prod', backup_status: 'missing' }).map((row) => row.host_id), ['blocked']);
});

test('fleet endpoint failure is explicit and safe text cannot become HTML', async () => {
  fetch = async () => response({ message: '<script>not evidence</script>' }, 503);
  const page = mount(); await renderFleet(page);
  assert.match(page.textContent, /Fleet compliance unavailable/); assert.match(page.textContent, /<script>not evidence<\/script>/);
  assert.equal(page.querySelectorAll('script').length, 0);
});

test('wizard places recovery before plan and rejects expired or ambiguous UTC windows', () => {
  assert.ok(WIZARD_STAGES.findIndex((stage) => stage.id === 'recovery') < WIZARD_STAGES.findIndex((stage) => stage.id === 'plan'));
  assert.match(maintenanceWindowError('2030-01-01T01:00:00', '2030-01-01T02:00:00Z'), /UTC/);
  assert.match(maintenanceWindowError('2030-01-01T02:00:00Z', '2030-01-01T01:00:00Z'), /after/);
  assert.match(maintenanceWindowError('2020-01-01T01:00:00Z', '2020-01-01T02:00:00Z'), /expired/);
  assert.match(maintenanceWindowError('2030-02-30T01:00:00Z', '2030-03-03T02:00:00Z'), /valid UTC calendar/);
  assert.match(maintenanceWindowError('2030-02-28T01:00:00Z', '2030-02-29T02:00:00Z'), /valid UTC calendar/);
  assert.match(maintenanceWindowError('2030-01-01T24:00:00Z', '2030-01-02T02:00:00Z'), /valid UTC calendar/);
  assert.equal(maintenanceWindowError('2032-02-29T01:00:00Z', '2032-02-29T02:00:00Z'), null);
  assert.equal(maintenanceWindowError('2030-01-01T01:00:00Z', '2030-01-01T02:00:00Z'), null);
});

test('wizard never presents a stale README binding as reviewed', () => {
  const artifact = { sha256: 'a'.repeat(64), patch_ids: ['39034528'], platforms: [{ id: '226' }], readme_files: [{ path: 'README.html', sha256: 'b'.repeat(64) }] };
  const procedure = { artifact_sha256: artifact.sha256, patch_id: '39034528', execution: { adapter: 'database_single_instance_opatch' }, target: { platform_id: '226', database_unique_name: 'ORCL' }, oracle_references: [{ kind: 'patch_readme', identifier: 'README.html', sha256: 'b'.repeat(64) }] };
  const source = [...steps, { step: 'artifact-inspect', evidence: { artifact } }, { step: 'procedure-validate', status: 'ready_for_planning', evidence: { procedure } }];
  assert.equal(wizardContext(source, 'source').bound, true);
  procedure.oracle_references[0].sha256 = 'c'.repeat(64);
  assert.equal(wizardContext(source, 'source').bound, false); assert.equal(wizardContext(source, 'source').readme, null);
});

test('wizard keeps the reviewed database when discovery lacks it and never borrows a database for Grid', () => {
  const artifact = { sha256: 'a'.repeat(64), patch_ids: ['39034528'], platforms: [{ id: '226' }], readme_files: [{ path: 'README.html', sha256: 'b'.repeat(64) }] };
  const procedure = { artifact_sha256: artifact.sha256, patch_id: '39034528', execution: { adapter: 'database_single_instance_opatch' }, target: { family: 'database', platform_id: '226', database_unique_name: 'SECOND' }, oracle_references: [{ kind: 'patch_readme', identifier: 'README.html', sha256: 'b'.repeat(64) }] };
  const source = [...steps, { step: 'artifact-inspect', evidence: { artifact } }, { step: 'procedure-validate', status: 'ready_for_planning', evidence: { procedure } }];
  const missing = wizardContext(source, 'source');
  assert.equal(missing.bound, true);
  assert.equal(missing.database, 'SECOND');
  assert.equal(missing.home, 'Unknown until target selection');
  procedure.target.database_unique_name = 'ORCL';
  assert.equal(wizardContext(source, 'source').home, '/u01/db');
  procedure.target = { family: 'grid', platform_id: '226' };
  procedure.execution.adapter = 'grid_rolling_opatch';
  const grid = wizardContext(source, 'source');
  assert.equal(grid.bound, true);
  assert.equal(grid.database, 'Not applicable (Grid Infrastructure)');
  assert.equal(grid.home, 'Unknown until target selection');
});

test('live recovery can start before a readiness decision using an explicit conservative policy', async () => {
  const calls = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { calls.push(JSON.parse(options.body)); return response({ run_id: 'prepare' }, 202); }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded' } : url === '/api/recovery?host_id=sourcedb' ? { requests: [], live_available: true, supported_adapter: 'standalone_primary_noarchivelog_spfile', target_capability_context: { host_id: 'sourcedb' }, target_capabilities: [{ database: 'ORCL', status: 'needs_native_analysis', can_create: true, requirements: [], blockers: [] }] } : { steps: [{ step: 'discovery', evidence: snapshot }] });
  };
  const page = mount(); await renderRecoveryStage(page, 'sourcedb');
  field(page, 'Backup parent directory').value = '/u02/backup';
  field(page, 'Window start (UTC)').value = '2030-01-01T01:00:00Z'; field(page, 'Window end (UTC)').value = '2030-01-01T02:00:00Z';
  await button(page, 'Create live recovery request').fire('click');
  assert.equal(calls.length, 1); assert.equal(calls[0].policy.recovery.require_backup, true);
  assert.equal(calls[0].policy.database.maximum_invalid_objects, 0); assert.equal(calls[0].policy.maximum_snapshot_age_seconds, 1800);
});

const { renderReadinessStage } = await import('../webapp/static/stages/readiness.js');
const { renderRecoveryDetail } = await import('../webapp/static/recovery_pages.js');

test('selected recovery policy needs an explicit copy and never rewrites the displayed saved gate thresholds', async () => {
  const selected = { ...policy, maximum_snapshot_age_seconds: 7200, database: { ...policy.database, maximum_invalid_objects: 10 } };
  const source = [
    { step: 'discovery', done: true, evidence: { ...snapshot, collected_at: new Date().toISOString() } },
    ...['reconcile', 'artifact-inspect', 'procedure-validate', 'compatibility-collect', 'compatibility-reconcile'].map((step) => ({ step, done: true })),
    { step: 'readiness-evaluate', done: true, status: 'blocked', input: policy, recovery_selection: { host_id: 'sourcedb', request_id: 'backup-verified', policy: selected }, evidence: { status: 'blocked', gates: [{ name: 'database_invalid_objects', status: 'blocker', detail: 'source database ORCL has excessive invalid-object evidence.' }] } },
  ];
  const posts = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { posts.push(JSON.parse(options.body)); return response({ run_id: 'evaluate' }, 202); }
    return response(url.startsWith('/api/runs/') ? { status: 'succeeded' } : { steps: source });
  };
  const page = mount(); await renderReadinessStage(page, 'sourcedb');
  assert.equal(field(page, 'Max invalid objects').value, '0'); assert.equal(posts.length, 0);
  await button(page, 'Use selected backup policy').fire('click');
  assert.equal(field(page, 'Max invalid objects').value, '10'); assert.equal(posts.length, 0);
  assert.match(page.querySelector('.readiness-finding').textContent, /RequiredInvalid objects ≤ 0/);
  await button(page, 'Evaluate readiness').fire('click');
  assert.equal(posts[0].policy.database.maximum_invalid_objects, 10);
});

test('reloaded unknown backup run exposes record reconciliation and suppresses duplicate execution', async () => {
  const request = { request_id: 'backup-1', host_id: 'sourcedb', mode: 'live', state: 'authorized', requester: 'requester', latest_run: { run_id: 'run-1', status: 'unknown', context: { request_id: 'backup-1' }, error: { message: 'SSH disconnected' } } };
  const posts = [];
  fetch = async (url, options) => {
    if (options.method === 'POST') { posts.push(url); request.latest_run = { run_id: 'run-1', status: 'failed' }; return response(request.latest_run); }
    return response(request);
  };
  const page = mount(); await renderRecoveryDetail(page, 'backup-1');
  assert.match(page.textContent, /SSH disconnected/);
  assert.equal(page.querySelectorAll('button').filter((entry) => entry.textContent === 'Execute').length, 0);
  await button(page, 'Inspect and reconcile').fire('click');
  assert.deepEqual(posts, ['/api/runs/run-1/reconcile']);
});
