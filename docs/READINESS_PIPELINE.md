# Readiness Pipeline

This pipeline is deliberately separate from execution. A successful result is
only `ready_for_approval`; it never performs a patch operation.

## Evidence sequence

1. `opu-topology-discover` runs locally on each active node (the webapp SSHes
   every `hosts.json` node alias for a cluster). It stores one sealed
   `snapshot_<node>` document per node plus a primary `snapshot` view for
   estate/UI discovery phases operators can audit before later gates:
   - **A Host identity & OS** — hostname/OS/kernel (always required)
   - **B Oracle homes & OPatch** — home paths, owners, versions, OPatch tooling
   - **C Cluster / RAC / CRS** — membership + Grid runtime (required for
     RAC/Grid; `not_applicable` on single-instance when Clusterware is absent)
   - **D Databases & instances** — DB→home mapping + runtime (required for
     database-family; `not_applicable` on Grid-only hosts)
   - **E Patch inventory & platform** — OPatch XML platform ID/name/digest and
     installed patches (**required before reconcile / compatibility / readiness**)
2. `opu-snapshot-reconcile` rejects differing cluster membership, Grid state,
   Oracle-home inventory, or database-to-home mappings.
3. `opu-artifact-inspect` produces a deterministic artifact manifest. The
   staging directory must contain exactly one patch identity and exactly one
   authoritative `etc/config/inventory.xml` platform entry. Its numeric ARU
   platform ID, platform name, and source-document SHA-256 are sealed in the
   manifest. Filenames are not platform evidence: ARU `226` is
   `Linux x86-64`, while ARU `46` is the different `Linux x86` platform.
4. `opu-procedure-validate` binds the artifact digest and platform ID to the
   patch README and approved Oracle procedure metadata. The selected target
   home must carry independently discovered platform evidence for the same ID.
5. `opu-opatch-compatibility-collect` runs two distinct fixed Oracle
   prerequisites as the discovered owner for every local target home:
   `CheckPatchApplicableOnCurrentPlatform` and
   `CheckConflictAgainstOHWithDetail`. It seals the native return code,
   command evidence path, and SHA-256 for each result; success from one check
   cannot substitute for the other.
6. `opu-compatibility-reconcile` requires one passing evidence record for
   every expected `node:home` pair. It rejects duplicate, incomplete, failed,
   or procedure-mismatched local results.
7. `opu-readiness-evaluate` requires all per-node evidence and policy gates to
   agree before returning `ready_for_approval`. Its result includes
   `evaluated_at`, the earliest `valid_until`, and one path/digest/host/time
   record for every supplied snapshot.

(Discovery phases above are a UI/API view of the primary-node snapshot; reconcile
and readiness consume every per-node `oracle.topology.discover` document.)

For RAC, step 2 requires one snapshot per active node, step 5 runs once on each
node, and step 6 combines the local documents. A result for one node never
stands in for another node.

The reconciled `snapshot_evidence` set must exactly equal the snapshots passed
to readiness evaluation. `opu-patch-plan` independently re-hashes those files
and recomputes their policy expiry at create, approve, authorize, and dispatch.
An old readiness JSON remains useful audit evidence, but it cannot authorize a
new or delayed apply after the earliest bound snapshot expires.

The readiness target and immutable plan also carry the same numeric platform
ID as the artifact, procedure, reconciled target home, and every compatibility
record. Any absent, multiple, nonnumeric, or disagreeing platform value blocks
planning. This binding prevents a compatibility result produced for one
platform from authorizing another platform's artifact or home.

## Procedure editor and README hints

The web editor restores a saved procedure only when its artifact, patch,
platform, and README digests match the current inspection. Edited values and
the last failed submission are shown as unvalidated drafts. Submitting another
validation clears the previous procedure result and dependent compatibility
and readiness results, including when that new validation fails.

Autofill preserves entered values and fills only unambiguous missing fields.
Select an inspected README to retrieve its minimum OPatch requirement through
`GET /api/hosts/{id}/procedure-hints?readme_identifier=README.html`. This read-only
endpoint fetches at most 2 MiB plus one overflow-detection byte, verifies the
README's inspected SHA-256, and recognizes explicit minimum-version statements.
It returns the source sentence with the hint. A changed README requires fresh
artifact inspection; missing or ambiguous wording requires manual review.
Rollback conditions remain an explicit entry from the README. Hints neither
validate a procedure nor authorize execution.

## Compatibility collector inputs

The collector accepts a local topology snapshot, exact staged artifact,
artifact manifest, and procedure-validation result. It does not accept a Grid
home, database home, node, or OS user argument. It derives homes and owners
from the signed discovery evidence and verifies:

- the staged artifact path, ID, and digest match the validated procedure;
- the artifact's authoritative ARU platform ID matches the procedure and the
  platform evidence discovered for every selected Oracle home;
- the local `OPatch` version meets the procedure minimum; and
- Oracle's platform-applicability prerequisite succeeds for every target home:
  `opatch prereq CheckPatchApplicableOnCurrentPlatform -ph
  <verified-artifact>`; and
- the patch README-documented Oracle conflict prerequisite succeeds for every
  target home. For modern 19c RU metadata this is
  `opatch prereq CheckConflictAgainstOHWithDetail -ph <verified-artifact>`;
  it is not interchangeable with `-phBaseDir`.

Each prerequisite retains its own native exit code and content-addressed log.
Any unavailable `OPatch`, owner, version, platform evidence, command error, or
failed prerequisite is recorded as `blocked`. The collector is read-only.

A blocked result must be explainable without a host login. Every failed
prerequisite therefore also carries `detail` — OPatch's own "The details are:"
text (or the failure line) taken from the sealed log — and the document lists
one human-readable entry in `findings` per blocked condition (incomplete media,
unreadable metadata, OPatch version, platform mismatch, or a failed check).
Metadata-only stage directories (no `files/` payload) are diagnosed before
OPatch runs; otherwise OPatch reports them as the opaque "one-level down" /
"No patch location specified" errors.

## Fixing blocked media from the control plane

`stage-artifact` is a remediation action of the webapp pipeline (not part of
the evidence chain). It stages complete media at the same absolute path on
every node of a host via `bin/opu-artifact-stage`, either by replicating from
another managed host that already holds complete media, or by unpacking a
patch zip already on the node. `GET /api/hosts/<id>/artifact-sources` probes
where complete media exists. The tool is fail-closed: it extracts into a
private work directory, requires `etc/config/{inventory,actions}.xml` plus a
non-empty `files/` payload, requires the inventory patch ID to equal the
directory name, sets ownership to the Oracle Home owner, moves a metadata-only
stage aside (never deletes it), and refuses to overwrite complete media
without `replace`.

Replication prefers the hosts' own network: a throwaway ed25519 key is
authorised on the destination for the duration of one transfer, restricted to
`from=<source IPs>` with a forced `command=` pinned to the exact staging
invocation and `no-pty,no-port-forwarding,no-agent-forwarding`, then removed.
When the destination is unreachable from the source the stream is relayed
through the control plane instead. Staging clears every artifact-bound
evidence document (artifact, procedure, compatibility, reconciliation,
readiness) so the chain must be re-run from Artifact inspection.

Before any remote tool runs, the webapp compares a content fingerprint of
`bin/` + `lib/` with a stamp on the host and pushes the current tree when they
differ, so a host can never run a stale executor or collector.

Passing readiness is not a one-time permission to mutate. The execution
adapter re-runs both applicability and conflict checks immediately before the
first service outage and again at the binary-apply boundary. A failure before
shutdown ends the task as `no_mutation`; it must not stop the database or
listener.

## Policy example

```json
{
  "schema_version": "1.0",
  "maximum_snapshot_age_seconds": 1800,
  "require_xml_inventory": true,
  "recovery": {
    "require_backup": true,
    "max_backup_age_minutes": 1440,
    "minimum_fra_free_bytes": 107374182400,
    "require_guaranteed_restore_point": false
  },
  "database": {
    "require_primary_read_write": true,
    "maximum_invalid_objects": 0
  }
}
```

Actual values are change-policy decisions. The utility will not substitute
defaults for a missing policy document.

## Required filesystem recovery

Existing policies keep their FRA requirement. An explicit standalone policy can
set `recovery.storage_mode` to `filesystem`, keep `require_backup: true`, and
set a nonnegative `minimum_filesystem_free_bytes` reserve. This is not a backup
waiver. The selected recovery set must contain the validated database base,
controlfile, SPFILE, Oracle Home, Central Inventory and oraInst.loc from managed
preparation. Missing, stale, altered, incorrectly targeted, or incomplete
evidence blocks readiness. Measured filesystem capacity must satisfy the
reserve; capacity evidence must also be current.

The optional preparation `capacity_basis: "rman_unused_blocks"` uses measured
non-free datafile bytes only when Oracle's documented unused-block eligibility
conditions are proven. It does not assume a compression ratio. Ineligible or
unproven files retain their full allocation in the budget. The default basis
remains `allocated`; preparation retains its overhead and filesystem reserve.

`POST /api/hosts/{id}/pipeline/recovery-collect` selects a completed live
managed recovery request by `request_id`. It runs the native recovery collector
against the current app snapshot, mirrors that snapshot at the exact same
absolute path on the target, and retains the native result without rewriting
its binding. This validation may take many minutes. A changed snapshot during
collection rejects the result. Fixture and other-host recovery requests cannot
be selected.

A successful selection is remembered, and Refresh all evidence runs a new
recovery collection before readiness evaluation. Rediscovery invalidates the
previous recovery result. Patch planning independently repeats the same
selected-backup freshness, digest and filesystem checks. Backup age alone
cannot satisfy filesystem readiness.

The live recovery creation API must be explicitly enabled before its UI controls
activate; the response from `/api/recovery` advertises `live_available: true`
only when that route is installed. Until then, the form remains disabled.
Approval and authorization remain separate native tool operations; no actors
or approvals are generated by the recovery selector.
