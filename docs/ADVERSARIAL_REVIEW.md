# Adversarial correctness review — 18 September 2026

This review began from `adae38e`, after that revision's Mac and Linux fixture
gates had passed. It traced authority, target selection, durable ownership and
report provenance, then constructed counterexamples using disposable state.
The findings below are evidence that a passing test suite is not proof of
correctness. They are not claims about incidents on the user's databases.

| Boundary | Reproduced defect | Change and regression boundary |
| --- | --- | --- |
| Discovery versus backup target | A host could configure one SSH alias at host level and a different alias in its sole node. Discovery used the node alias, while recovery creation used the host alias. Matching cloned database names/homes did not establish that they were the same host. | Standalone recovery requires one consistent route. It binds the collector's node index and exact primary snapshot bytes to the request, retains private immutable copies, and checks the route before later actions and backup selection. Tests retain actual configuration loading and route admission; remote actions are recorders. |
| Before/after inventory | A native custody manifest sorted `apply-after-lspatches.log` before `apply-before-lspatches.log`; the latter overwrote the report's After inventory. Pre-datapatch health and explicit before JSON could similarly overwrite later facts. | Report parsing honors the observation side of each artifact and stage. Regressions feed actual custody-shaped evidence through the report builder with sorted names. |
| Empty rollback inventory | A successful inventory collection containing no interim patches was ignored, retaining an older nonempty inventory. | Recognized successful empty inventory becomes an empty list. Missing evidence remains unknown; an unrecognizable later inventory invalidates an older value. Completion still depends on verified native final-stage evidence. |
| Controller PID reuse | An old controller's numeric PID could belong to a new process. Manual reconciliation then treated that unrelated process as the still-active owner indefinitely. | New runs retain a process-incarnation lock and its filesystem identity. A real live owner blocks reconciliation; a released original lock permits the existing explicit operator inspection flow even if its PID is reused. Missing/replaced proof and legacy PID-only records remain conservative. |
| Retrying failed pull work | A verified failed task could not be retried through the controller while later tasks remained queued. The queue was also unable to progress past that failed task. | Per-plan admission coordinates retry with claims and publication. Only a verified failed attempt can produce its verified next generation; queued future tasks keep their reservation, and unresolved controller or agent work still blocks competing execution. |
| Worker crash before launch | A claimed job whose worker died before starting the executor became reconciliation-required, but its native task was still pending and could never satisfy terminal-only reconciliation. | The managed worker durably records launch admission immediately before creating a subprocess. Explicit operator reconciliation may release only an expired, verified-pending claim whose admission was never granted. The old claim token is invalidated atomically. Once launch is admitted, uncertainty remains blocked until verified native outcome. Legacy/manual claims do not gain this recovery shortcut. |
| Production admission marker | Both controller and native guards accepted a world-writable or hard-linked certification marker. Independent marker reads could also produce a contradictory status/checklist display. | Both entry points share one bounded, protected descriptor read and exact, duplicate-aware parsing. Status uses one observation. Regressions cover permissions, links, special files, oversize/invalid data and replacement or mutation during the read. This local switch does not itself verify a signed release or grant independent production approval. |

## Failures found by combined validation

The first combined Mac gate for `4f505fe` failed. It was not installed into the
running application. The native standalone suite stopped with `plan is not
running`; the browser suite passed 37 cases and failed two. Focused passes did
not override these failures. The [Linux gate for the same revision](https://github.com/akhilreddy-oracle/oracle-Patching/actions/runs/35372671281)
also failed the assistant workflow and the same two browser cases; its native
suites passed. This distinction is retained rather than attributing the Mac
failure to Linux or treating the native issue as proven from its deleted state.

The new incarnation metadata exposed a missed consumer: the assistant compared
the entire owner dictionary to the old PID/instance-only shape. Valid model turns
therefore failed before invoking the model. The assistant now uses the runner's
shared ownership predicate, which verifies the exact held incarnation and its
original protected lock inode without acquiring ownership while reading old
records. Existing conversation/key/status fences still reject late replies. An
existing assistant workflow test also reproduces this regression; the native
gate had stopped before reaching that suite. A real-run HTTP regression now
checks a message through persisted response, with only model output simulated.

The connected browser fixture had seeded only a primary snapshot, while the new
recovery route check correctly required indexed discovery. It now feeds its
simulated topology through the real discovery publisher and checks the route
capability before backup creation. The production guard was retained.

The deleted temporary state from the original standalone failure cannot establish
its cause. Separate controlled native reproductions did establish two timing
defects that produce the same result: the executor stopped renewing before
evidence sealing, and completion checked expiry after expensive verification
while holding the lock that prevented renewal. In both cases the task succeeded,
the plan paused with `task_completed_after_lease`, and the next task was refused.
The affected executors now share a heartbeat through evidence sealing, join it
before a synchronous lease handoff, and block success if renewal fails. Completion
judges lateness at plan-lock admission, keeps that lock through verification and
custody, and preserves the existing pause/reconciliation policy for already
expired admission. The matching release receipt remains the authority for
combined validation status; these corrections do not waive ownership or evidence
checks.

The follow-up gate for `50b4889` passed all 39 browser cases but exposed a
lock-safety fixture with a running task and no lease expiry. Earlier ownership
admission correctly rejected that task before the intended unsafe-lock check.
The fixture now supplies a valid lease; its hardlink, symlink, FIFO, inode and
lock-inheritance assertions are unchanged. That failed gate is retained as
failure evidence, rather than replaced by its successful browser scope.

The next Mac run (`6997c0e`) exposed an intermittent demo-builder defect: the
standalone fixture sampled its collection time and expiry base separately. A
second-boundary crossing produced a 3,601-second interval under a 3,600-second
policy, correctly rejected by native admission. Its failed fixture and logs were
retained; only the disposable validation process group was stopped. The builder
now derives both timestamps from one observation. A forced clock-crossing
regression covers standalone, RAC and Grid builders without relaxing freshness
validation or claiming the aborted run passed.

## Compatibility and operational limits

Older live recovery requests remain inspectable and existing execution outcomes
remain reconcilable. They cannot gain new backup authority without a recorded
discovery route: refresh discovery and create a new request. The application does
not attach new evidence retroactively to an old request. Refreshing unrelated
current evidence does not retarget a request that already has a valid saved
binding.

Controller incarnation locks are retained for the process lifetime and are not
deleted during normal operation. Do not remove them to clear an active or unknown
run. Missing or altered proof for the new protocol blocks reconciliation. Legacy
records cannot acquire historical proof of process identity after the fact:
PID-only records retain their conservative process check, while ownerless legacy
non-detached records retain the existing explicit operator confirmation and audit
note requirement. The filesystem queue remains a lab/shared-filesystem mechanism, not a
qualified distributed scheduler. A terminal task verification and a process
ownership observation answer different questions; neither substitutes for the
other.

The tests use local files, real process/lock boundaries, native fixture workflows
and simulated external transport. No SSH, live Oracle mutation, real model
inference or remote installation was performed for this review. Full validation
must be read from the exact-source release receipt and current draft PR checks;
the baseline receipt does not validate these changes.

The acceptance gaps in [ROBUSTNESS_REVIEW.md](ROBUSTNESS_REVIEW.md) remain:
supported live Oracle and restore/rollback drills, per-configuration qualification,
company identity-provider acceptance, final local-model acceptance, remote
deployment and independent production approval. This review does not claim that
every line or possible execution path has been proven correct.
