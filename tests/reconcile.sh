#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-reconcile.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
make_snapshot() { jq -n --arg host "$1" --argjson patches "$2" --arg platform "${3:-226}" --arg digest "${4:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}" '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},host:{name:$host},cluster:{grid_home:"/grid",runtime:{status:"healthy",active_version:"19",upgrade_state:"NORMAL",active_patch_level:"1"},nodes:[{name:"node-a",status:"Active"},{name:"node-b",status:"Active"}]},oracle_homes:[{path:"/grid",owner:"grid",version:"19",opatch_version:"1",platform:{status:"collected",id:$platform,name:(if $platform == "226" then "Linux x86-64" else "Linux x86" end),source:"opatch_lsinventory_xml",source_sha256:$digest},patches:$patches}],databases:[{db_unique_name:"DB1",oracle_home:"/db"}],warnings:[]}' ; }
make_snapshot node-a.example '["100"]' >"$TMP/a.json"
make_snapshot node-b.example '["100"]' >"$TMP/b.json"
"$ROOT/bin/opu-snapshot-reconcile" --snapshot "$TMP/a.json" --snapshot "$TMP/b.json" --output "$TMP/result.json" >/dev/null
jq -e '.status == "consistent" and .expected_nodes == ["node-a","node-b"] and (.findings | length == 0)' "$TMP/result.json" >/dev/null
"$ROOT/bin/opu-snapshot-reconcile" --snapshot "$TMP/a.json" --snapshot "$TMP/b.json" >"$TMP/stdout-result.json"
jq -e '.status == "consistent"' "$TMP/stdout-result.json" >/dev/null
# Per-node OPatch XML digests differ (hostName/UId) but inventory must still reconcile.
make_snapshot node-b.example '["100"]' 226 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb >"$TMP/b-different-xml-digest.json"
"$ROOT/bin/opu-snapshot-reconcile" --snapshot "$TMP/a.json" --snapshot "$TMP/b-different-xml-digest.json" --output "$TMP/digest-result.json" >/dev/null
jq -e '.status == "consistent" and (.findings | length == 0)' "$TMP/digest-result.json" >/dev/null
make_snapshot node-b.example '["999"]' >"$TMP/bad.json"
if "$ROOT/bin/opu-snapshot-reconcile" --snapshot "$TMP/a.json" --snapshot "$TMP/bad.json" >/dev/null 2>&1; then echo 'inventory mismatch was accepted' >&2; exit 1; fi
make_snapshot node-b.example '["100"]' 46 >"$TMP/wrong-platform.json"
if "$ROOT/bin/opu-snapshot-reconcile" --snapshot "$TMP/a.json" --snapshot "$TMP/wrong-platform.json" >/dev/null 2>&1; then echo 'cross-node Oracle platform mismatch was accepted' >&2; exit 1; fi
jq -n '{schema_version:"1.0",collector:{name:"oracle.topology.discover",version:"1"},host:{name:"standalone.example"},cluster:{status:"unavailable",grid_home:null,runtime:{status:"unavailable"},nodes:[]},oracle_homes:[{path:"/u01/db",owner:"oracle",version:"19",opatch_version:"12.2.0.1.51",platform:{status:"collected",id:"226",name:"Linux x86-64",source:"opatch_lsinventory_xml",source_sha256:("b"*64)},patches:[]}],databases:[{db_unique_name:"ORCL",oracle_home:"/u01/db"}],warnings:["Clusterware home was not detected from PATH or active processes."]}' >"$TMP/standalone.json"
"$ROOT/bin/opu-snapshot-reconcile" --snapshot "$TMP/standalone.json" --output "$TMP/standalone-result.json" >/dev/null
jq -e '.status == "consistent" and .expected_nodes == ["standalone"] and .captured_nodes == ["standalone"] and (.findings | length == 1) and .findings[0].severity == "warning"' "$TMP/standalone-result.json" >/dev/null
printf '%s\n' 'snapshot reconcile test passed'
