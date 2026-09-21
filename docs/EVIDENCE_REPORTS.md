# Before and after evidence reports

Plan pages and execution stages provide an evidence comparison and JSON,
printable HTML, and CSV downloads. Reports work for completed, paused and
incomplete historical plans. Building a report performs local read-only
verification; it does not contact a host, refresh evidence or run recovery.

The baseline uses snapshots/reconciliation only when their bytes still match
the SHA-256 references sealed into the plan. The report then reads task status
through the native plan tool, which verifies terminal seals and custody. Each
consumed artifact is checked again against its custody hash. A changed file
invalidates that task's contribution rather than leaving partially verified
values in the comparison.

The comparison covers binary patch inventory, target SQL patch latest
action/status, invalid-object count, database component status, listener READY
registration, and database role/open/instance state. The report interprets
specific custodied native artifacts. Generic stdout, error messages and failed
task output never establish successful health observations. Failed-stage
evidence metadata remains available for diagnosis.

**Verified** means saved evidence matched its sealed/custodied reference. It
does not mean current live health. Missing or unverifiable fields remain
**Unknown**. A paused plan reports its latest verified completed stages with
final validation incomplete. Completion requires a succeeded plan and a
verified succeeded final validation whose local evidence remains intact.
Completion recognizes the native standalone, RAC, Grid, OPatchAuto, OJVM and
out-of-place apply/rollback final stages. Adapter prechecks populate the
before column. Completion alone does not supply missing comparison metrics.

New standalone apply/rollback executions retain bounded, hash-bound precheck
and final inventory, dictionary, SQL patch and listener observations. This adds
read-only dictionary/registry probes to precheck and preserves observations
already produced by other stages. Existing plans are not rewritten: historical
baseline SQL/components or listener values may remain unknown.

Rollback is never approved by a comparison report. A completed apply source
only changes the display to **requires_native_validation**. The existing native
rollback workflow must separately verify lineage, README procedure, recovery
evidence, maintenance window and distinct approval/operator identities.

The JSON report includes comparison values, exact evidence source hashes,
compact native task metadata, gaps and a report SHA-256. It does not export
executable artifacts or an unredacted raw-log bundle. HTML escapes all dynamic
content and includes print styling; CSV neutralizes spreadsheet formula
prefixes. Credential-like values in exported diagnostic strings are redacted,
while authoritative SHA-256 identifiers are preserved.

Backend contracts:

- `evidence_reports.build(plan_id) -> dict`
- `evidence_reports.export(report, format) -> (text, content_type, filename)`
- `GET /api/plans/<id>/report` returns the JSON report;
  `?format=json|html|csv` downloads the selected representation.

Fixture validation: `python3 -B tests/evidence_reports.py` and
`bash tests/single_instance_patch.sh`. The suites use temporary evidence and
fake Oracle commands, including apply and rollback artifact-custody checks.
## Saved failure diagnostics

Failed task records can include `failure_diagnostics` and `next_action` in the
JSON report and the printable HTML export. The application displays these with
the recorded task and timestamp. Only stdout/stderr files whose hashes match the
verified custody manifest are included, with credential redaction and bounded
text. A missing or altered diagnostic remains an evidence gap.

Diagnostic text from a failed task never supplies a successful health observation
or grants retry, repair or rollback authorization. A reported extjob ownership
failure directs the operator to verify binary provenance and the README's
requirements through an administrator-supported procedure. It does not perform
a permission change. The comparison CSV continues to contain comparison rows;
use JSON or HTML for the saved failure details.
