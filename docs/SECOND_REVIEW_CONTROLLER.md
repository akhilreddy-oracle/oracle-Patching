# Second correctness review: controller and evidence

Historical second-pass record. The runtime startup/collector race documented
below is addressed by the subsequent [third review](THIRD_REVIEW_EXECUTION.md).

This pass followed the user's request to review code independently of passing
tests. It traced the data consumed by each decision, durable ownership during
failure, and the boundary between a requested operation and verified execution.
The regression cases below were added after finding defects in those paths;
they are examples of the corrected behavior, not proof of general correctness.

## Read coverage

Full implementation reads covered `webapp/server.py`, `auth.py`,
`pipeline_runner.py`, `pipeline_steps.py`, `planctl.py`, `recoveryctl.py`,
`evidence.py`, `execution_console.py`, `evidence_reports.py`, `host_config.py`,
`durable.py`, `runtime_paths.py`, `production.py`, `release_status.py`,
`localtools.py`, `adapters.py`, `procedure_hints.py`, `discovery_phases.py`,
`extjobctl.py` and `lockctl.py`.

Release validation, signing/verification, SBOM generation, lint and contract
entrypoints and the CI workflow were traced. Schema checks and existing tests
were inspected at the relevant contracts. This does not claim an independent
line-by-line review of every fixture, dependency, document or historical
blueprint generator. The other second-review reports record their own coverage.

## Defects found and corrected

| Decision or boundary | Defect in the prior implementation | Corrected behavior |
| --- | --- | --- |
| Discovery completeness | Missing entries in an existing node index could disappear, allowing a primary-only or partial node set to become authoritative. A JSON `null` index also looked absent. | Present indexes must be valid, complete, unique and regular files. Only an actually absent index permits the legacy single snapshot path. |
| Discovery publication | A failed index write could leave a primary snapshot published without a complete index. | Publish node snapshots and the index before the primary document. |
| Per-node compatibility | A missing RAC snapshot could be replaced with primary-node evidence before collection. | Validate all configured node snapshots before synchronization or SSH. The collector labels its result from that selected snapshot; correct SSH alias-to-host mapping remains a deployment requirement. |
| Reviewed procedure requirements | Readiness refresh replaced media/README hashes while retaining requirements reviewed against old content. | Preserve reviewed bindings. Changed or missing README/media requires renewed operator review. |
| Confirmed AI plan creation | The assistant checked evidence at confirmation, but the asynchronous native worker could create a plan from later evidence. | Pass the approved configuration/evidence digest through the native route; recheck patch, database and the digest in the worker. Hold a shared host evidence lock through sealing. Managed refreshes take that same lock. |
| Competing execution transports | HTTP import/execution could overlap queued pull-agent work and overwrite its state despite native task locks. | A stable plan transport lock excludes competing controller operations and queue publication. Published or unverified queue attempts reserve the plan. Unknown HTTP execution also blocks publication. |
| Persisted run recovery | Invalid stored statuses could lose ownership or appear failed, permitting fresh work without resolving an uncertain launch. | Invalid persisted states remain unresolved and block new admission. |
| Native command results | A successful process exit with scalar JSON or another task's output could be presented as successful execution. | Verify object shape, exact task identity and successful terminal status. Native terminal custody remains independently checked. |
| Authorization configuration | An invalid RBAC boolean could fall through to another authentication mode. | Reject invalid values rather than changing the authorization mode. |
| Recovery capability | Saved target guidance did not exclude CDBs despite a single-database implementation. | Require observed `CDB=NO`; native analysis independently probes it before downtime. |
| Host display contract | A valid host without an optional label could raise a controller error. | Use its configured ID as the display fallback. |

`tests/controller_evidence_integrity.py` reproduces corrupt/incomplete evidence,
changed confirmed inputs, filesystem lock contention, queue reservations and
unresolved ownership with isolated stores. Assertions at the SSH/native boundary
prove rejected cases do not reach it. Recovery admission and existing controller
tests exercise the integrated contracts separately.

## Boundaries that remain

- The host evidence lock coordinates application-managed writers. Independent
  filesystem writers and direct native CLI invocations are outside that lock;
  administrator-protected state and native document sealing remain necessary.
- The queue uses a filesystem store. These locks do not establish distributed
  consensus or correctness across independently copied controller stores.
- Remote tool synchronization now excludes an executor that already holds its
  host mutation lock. Immutable runtime generations are still needed to remove
  the startup/read-only collector version race entirely.
- Current database adapters reject multitenant targets. Root SQL registry success
  cannot prove that every PDB and seed was patched; all-container support needs a
  separate design and live acceptance.
- Native postconditions, fixture execution and source review cannot establish
  actual restore/open success, customer SSO behavior, production approval or the
  configured local model's protocol acceptance. Those remain separate evidence.

No new SSH connection, Oracle mutation, customer notification or model inference
was performed in this second review.
