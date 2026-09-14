# Codebase review remediation — 14 September 2026

This change addresses the concrete defects from the repository review. Existing
uncommitted work was retained. Verification uses isolated local fixtures and
mocked HTTP/SSH or Oracle executables; it does not certify a live Oracle patch.

| Review finding | Implemented correction | Regression coverage |
| --- | --- | --- |
| Recovery demo path traversal and destructive replacement | Validate identifiers and resolved containment before filesystem work; reject existing fixture paths | `tests/webapp_control.py` |
| Shared-token actor impersonation and missing role checks | Per-principal token digests in RBAC mode, authenticated actor binding, central route authorization, fail-closed principal configuration, typed and bounded request bodies | `tests/webapp_control.py` |
| Corrupt or missing tasks produce plan success | Sealed expected-task manifest and strict task/custody validation; incomplete scans cannot mark success | `tests/execution_lifecycle.sh` |
| Duplicate claims and overlapping plans | Atomic durable queue writes under process locks; claim-token fencing; target reservations and host execution exclusion | `tests/agent_queue_hardening.py`, `tests/execution_lifecycle.sh` |
| Unknown outcomes retried; safe retries rejected | Require verified retry eligibility; immutable attempt directories and attempt-bound evidence | `tests/execution_lifecycle.sh`, `tests/single_instance_patch.sh` |
| Nested artifact links escape content hashes | Preflight all archive members, reject links/special files/traversal, validate trees during inspection and execution; serialize staging | `tests/artifact_safety.py`, `tests/artifact_stage.sh`, `tests/execution_lock_files.sh` |
| Invalid live Data Guard SQL and fabricated lag | Role-specific documented Oracle view columns, native identity and freshness checks, explicit unknown primary lag | `tests/dataguard_live.sh`, `docs/DATA_GUARD.md` |
| Missing invalid-object count passes readiness | Require a nonnegative integer before policy comparison | `tests/readiness.sh` |
| Restart loses active run ownership | Persist run ownership and remote launch context; retain unknown state until reconciliation | `tests/webapp_control.py` |
| Enrollment token omitted or used on another node | Forward credentials; enforce enrolled node and authenticated API actor | `tests/webapp_control.py`, `tests/agent_queue_hardening.py` |
| Worker lease expires and adapter map drifts | Renew throughout executor work, retain unknown after ownership loss; one twelve-adapter Python registry | `tests/agent_queue_hardening.py` |
| Incomplete release and host synchronization | Include `operations/` and required Python agent modules; content/mode fingerprints invalidate on source changes; isolated extraction smoke tests | `tests/runtime_package.py`, `tests/release_signing.sh` |
| UI status, identity, host scope, and failure rendering | Typed states, explicit host identity, six procedure adapters, conditional Grid fields, central API errors, preserved Execute failures and cancellable stale reads | `tests/frontend.mjs` |
| CLI input interpolated into Python source | Quoted heredoc with values carried as environment data | `tests/agent_queue_hardening.py` |
| Incomplete quality gates | Mandatory contract environment and ShellCheck, Python/JavaScript syntax checks, frontend/backend regressions and generated-runtime schema validation | `make check`, `tests/runtime_contracts.py` |

## Operational compatibility

RBAC deployments must provision a separate random bearer token for every
principal and store its SHA-256 digest as `token_sha256` beside `actor` and
`roles`. Shared lab tokens cannot authenticate as RBAC principals. Production
mode requires RBAC; explicit lab mode remains available for isolated fixtures.
See the webapp documentation for the configuration format.

Agent claims return `claim_token` and `claim_generation`. Workers must return the
claim token when renewing or completing work. Claim tokens are stored as digests.
Tasks are ordered across a plan's nodes, coordinator tasks are routed to the
coordinator host, and verified tasks that have not started can be deferred
without consuming an attempt. Real completion is checked against sealed terminal
evidence, and reconciliation does not block unrelated worker heartbeats.
Expired jobs stay `reconciliation_required`; they are never redelivered blindly.
The queue reconciliation API requires an independently verified terminal task
outcome for the same attempt. Interrupted controller runs similarly require
reconciliation before their execution key can be reused.

Paused plans and unknown outcomes retain target reservations. Existing unfinished
plans also block overlapping dispatch during migration. Preserve their evidence
and resolve the actual outcome before retry or rollback. Legacy rolling mutation
helpers now require the managed executor workflow for live execution; analysis
and test fixtures remain available.

Unknown/recovery-required outcomes have no automatic recovery or force-unlock
transition. Recovery tools can support manual Oracle recovery, but a paused plan
needs explicitly reviewed controller-state reconciliation before fresh dispatch.
Creating rollback plans still requires a succeeded source apply; deleting only
the reservation registry does not bypass the historical paused-plan check.

The Python agent runtime and synchronization installer require Python 3.9 or newer.
They use a compatible `python3` when available, otherwise an installed versioned
interpreter (`python3.14` through `python3.9`). This supports Oracle Linux 8 hosts
whose default `python3` remains 3.6, without changing system alternatives. The
installer and Python-backed CLI helpers share the same selection logic. Missing
interpreters produce an actionable error; failed installs remove their upload
and report the failed step and exit code.

Safe staging also requires Python 3.9 or newer. Archive payloads must contain only the expected
patch directory, ordinary files and directories. When creating portable archives
on macOS, set `COPYFILE_DISABLE=1` to omit AppleDouble sidecars. Extracted runtime
packages require Python, Bash and the documented host utilities; the host sync
installer also uses `flock` to serialize installations.

## Remaining validation

Run the standalone Oracle 19c apply/rollback and interruption drills in an
isolated Oracle lab, then validate RAC/Grid and Data Guard separately. The live
Data Guard primary view does not supply elapsed standby lag: absent verified
standby/broker lag is intentionally a blocker. Enterprise identity/transport,
database-backed high-availability state, operational audit retention and pilot
approval remain deployment work, not outcomes established by these fixture tests.

## Verification result

`make -j4 check` completed with exit code 0 against the frozen implementation:
41 shell suites, four Python suites, and the frontend suite (46 suites total).
The gate also passed Bash/ShellCheck, Python and JavaScript syntax validation,
22 JSON schemas, three procedure examples, and generated runtime payload checks
with negative evidence/shape/timestamp probes.

The full run includes standalone/RAC apply and rollback, manual Grid and
OPatchAuto, OJVM, out-of-place switching, and webapp RAC/Grid end-to-end fixtures.
The complete local log is `/private/tmp/opu-fixes-check.log`. Tests used simulated
Oracle executables and isolated state; no live Oracle or managed-host mutations
were performed.

## Live discovery follow-up (2026-09-14)

Live refresh exposed an installer regression: all four machines had Python 3.6
as `python3`, alongside a supported versioned interpreter. Shared interpreter
selection now covers both installation and Python-backed CLI commands. The
eight runtime-package tests pass, including actual installation with an older
default Python, preservation of host state, and cleanup on rejected runtimes.
Interpreter selection, execution lock, agent worker and artifact safety tests
also pass, along with the repository lint gate. The full suite above was not
rerun for this focused follow-up.

After restarting the local application, tool fingerprints and runtime imports
were verified on `oracle-test-rac`, `oracle-test-rac2`, `target`, and `source`.
All three live discovery runs succeeded: `73274e8166f2` (both RAC nodes),
`ca8b6f860f66` (targetdb), and `c1bc70021ddf` (sourcedb). Their refreshed database
states are OPEN. This verification synchronized utility files and collected
read-only Oracle evidence; it did not apply or roll back Oracle patches.
