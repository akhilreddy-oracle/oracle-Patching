# Readiness Pipeline

This pipeline is deliberately separate from execution. A successful result is
only `ready_for_approval`; it never performs a patch operation.

## Evidence sequence

1. `opu-topology-discover` runs locally on each active node.
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

For RAC, step 5 runs once on each node and step 6 combines the local documents.
A result for one node never stands in for another node.

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
