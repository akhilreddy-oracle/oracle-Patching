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
