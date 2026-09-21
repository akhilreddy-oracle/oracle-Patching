import assert from "node:assert/strict";
import { test, beforeEach } from "node:test";
import { fleetNextActions, validateFleetMetadata, renderFleet } from "../webapp/static/fleet.js";

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; this._text = ""; this.value = ""; }
  setAttribute(key, value) { this.attrs[key] = String(value); if (key === "value") this.value = String(value); }
  appendChild(node) { this.children.push(node); return node; }
  addEventListener(name, action) { this.events[name] = action; }
  async fire(name) { await this.events[name]?.({ preventDefault() {} }); }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(value) { assert.equal(value, ""); this.children = []; this._text = ""; }
  all(tag) { return this.children.flatMap((child) => [...(child.tag === tag ? [child] : []), ...child.all(tag)]); }
}

globalThis.document = { createElement: (tag) => new Element(tag) };
globalThis.localStorage = { getItem: () => null };
const fixture = { host_id: "source", host_label: "Source", environment: null, desired_patch_baseline: null,
  metadata_version: "a".repeat(64), configuration_missing: ["environment", "desired_patch_baseline"], evidence_status: "stale", baseline_status: "unknown" };
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
let mount, writes, rows, canManage, conflict;
beforeEach(() => {
  mount = new Element("main"); writes = []; rows = [fixture]; canManage = true; conflict = false;
  globalThis.fetch = async (url, options) => {
    if (options.method === "POST") {
      writes.push({ url, ...options, body: JSON.parse(options.body) });
      if (conflict) return response({ message: "Fleet configuration changed. Reload the fleet." }, 409);
      rows = [{ ...fixture, ...JSON.parse(options.body), configuration_missing: [] }];
      return response({});
    }
    assert.equal(url, "/api/fleet");
    return response({ databases: rows, can_manage_metadata: canManage });
  };
});

test("stale observations lead to discovery even when an older backup status says missing", () => {
  assert.deepEqual(fleetNextActions({ host_id: "source", evidence_status: "stale", backup_status: "missing" }),
    [{ label: "Refresh expired evidence", href: "#/hosts/source/discover" }]);
  assert.equal(fleetNextActions({ host_id: "source", evidence_status: "unknown" })[0].label, "Discover database");
  assert.equal(fleetNextActions({ host_id: "source", evidence_status: "fresh", backup_status: "missing" })[0].href, "#/hosts/source/recovery");
});

test("metadata validation accepts explicit clearing and rejects non-patch numbers", () => {
  assert.equal(validateFleetMetadata("", ""), null);
  assert.equal(validateFleetMetadata(" QA / Stage ", "39034528"), null);
  for (const input of ["0", "00123", "1e9", "12.3", "123; reboot", "1".repeat(21)]) assert.match(validateFleetMetadata("test", input), /positive patch ID/);
  assert.match(validateFleetMetadata("prod\nsecond", "12345"), /Environment/);
});

test("an administrator can save metadata with the observed version and blank clearing", async () => {
  await renderFleet(mount);
  assert.match(mount.textContent, /Not configured: environment, desired patch baseline/);
  assert.match(mount.textContent, /Refresh expired evidence/);
  const fields = mount.all("input");
  fields[0].value = " test "; fields[1].value = "";
  await mount.all("form")[0].fire("submit");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url, "/api/fleet/hosts/source/metadata");
  assert.deepEqual(writes[0].body, { environment: "test", desired_patch_baseline: null, expected_version: "a".repeat(64) });
  assert.match(mount.textContent, /Fleet settings saved for Source/);
  assert.match(mount.textContent, /unknown/);
});

test("a stale editor preserves values and offers explicit reload without retrying", async () => {
  conflict = true;
  await renderFleet(mount);
  const fields = mount.all("input");
  fields[0].value = "QA"; fields[1].value = "39034528";
  await mount.all("form")[0].fire("submit");
  assert.equal(writes.length, 1);
  assert.equal(fields[0].value, "QA");
  assert.match(mount.textContent, /Fleet configuration changed/);
  assert.equal(mount.all("button").find((button) => button.textContent === "Reload fleet").hidden, false);
});

test("read-only roles see missing setup and evidence navigation without edit controls", async () => {
  canManage = false;
  await renderFleet(mount);
  assert.equal(mount.all("form").length, 0);
  assert.match(mount.textContent, /An administrator can configure/);
  assert.equal(mount.all("a")[0].attrs.href, "#/hosts/source/discover");
  assert.equal(writes.length, 0);
});

test("invalid baseline is explained without sending a metadata mutation", async () => {
  await renderFleet(mount);
  mount.all("input")[1].value = "1e9";
  await mount.all("form")[0].fire("submit");
  assert.equal(writes.length, 0);
  assert.match(mount.textContent, /positive patch ID/);
});

test("cluster baseline uncertainty shows its supplied evidence limitation as plain text", async () => {
  const reason = "All-node baseline compliance is unavailable: this view contains the primary node's inventory. <img src=x>";
  rows = [{ ...fixture, evidence_status: "fresh", patch_baseline: "39034528", baseline_reason: reason }];
  await renderFleet(mount);
  const baseline = mount.all("td").find(cell => cell.attrs["data-label"] === "Patch baseline");
  assert.ok(baseline.textContent.includes(reason));
  assert.equal(baseline.all("span")[0].textContent, "unknown");
  assert.match(baseline.all("span")[0].className, /badge-warn/);
  assert.equal(mount.all("img").length, 0);
  assert.equal(writes.length, 0);
});
