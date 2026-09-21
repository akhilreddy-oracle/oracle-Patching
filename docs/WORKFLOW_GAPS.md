# Workflow gap review — 21 September 2026

This review traces handoffs between saved evidence, browser controls, assistant
proposals and native execution. It found additional defects after earlier checks
passed. It is not a claim that every line or every Oracle configuration is correct.

## Reproduced defects addressed

| Handoff | Reproduced problem | Changed behavior |
| --- | --- | --- |
| Readiness → plan | `ready_for_approval` still produced a usable preview when its expiry was missing, invalid or past. Browser controls also presented it as current. | Preview requires a valid future expiry. The readiness card and rail require refresh, and Create checks expiry again at submission. |
| Backup selection → readiness | Replacing backup A with B cleared native saved selection, but a failed or interrupted validation left A displayed with a Continue link. | Starting replacement validation removes that handoff. Only a confirmed selection restores it; failed or unknown results remain unconfirmed. |
| Out-of-place execution → report | File ordering let retained-original-home inventory overwrite patched-clone inventory. Switchback had the same ambiguity. | Each stage uses its native subject inventory artifact. Missing subject evidence stays unknown. Clone identity is checked before accepting report observations or completion. |
| Chat schedule → native command | Offset timestamps passed tool validation and appeared in cards, but native creation requires UTC. Unsupported fractional instants could also be proposed. | Normalize the same instant to whole-second UTC before review and hashing. Unsupported precision/range is rejected. Legacy noncanonical cards expire instead of silently rewriting confirmed arguments. |
| Execute remaining → later tasks | The ordinary UI path could reload host routes between tasks; the assistant path already retained its reviewed routes. | One inventory snapshot is retained for the whole native operation, and each task's preflight and route selection share it. |
| Interrupted launch → terminal result | A valid later retry's terminal result could satisfy reconciliation of an older launch. | Verify plan, task, definition digest and retry number against the original launch. Mismatches remain unresolved, including legacy launches without a verifiable attempt binding. |

Reproductions use isolated state, synthetic host names, simulated Oracle commands
and controlled failures. No managed-host SSH, live backup, patch, rollback or
database restart was used for this review.

The expiry display is evaluated when rendered and again when Create is clicked;
it is not a continuously ticking lease display. Native sealing remains the
authority. Ordinary UI execution pins routes when its worker starts; this is not
a new click-time route approval contract. Existing assistant admission still
rechecks its reviewed binding.

## Remaining capability and acceptance gaps

| Priority | Gap | Evidence needed to close it |
| --- | --- | --- |
| High | Reliable model-selected workflows | The latest retained six-case local model protocol assessment failed with 2/6 passing. Evaluate the final configured model against representative conversations, wrong targets, missing inputs, tool errors and untrusted evidence. This review's deterministic tests do not improve that model result. |
| High | Complete live recovery and patch acceptance | Retain one approved application-driven backup → validation → approval → patch → database/listener verification cycle for each claimed configuration, including disconnect/reconciliation and supported rollback. A simulated cycle is not live evidence. |
| High | Actual restored database | RMAN validation establishes backup readability/selection, not a restored and opened database. An actual restore drill remains separate work. |
| High | Oracle procedure and executable provenance | Patch-specific README requirements and rollback conditions still need verified inputs. The previously unresolved extjob provenance needs trusted Oracle media or an approved reference before any repair. |
| Medium | Broader recovery/topology support | The initial recovery preparation adapter supports standalone primary non-CDB, NOARCHIVELOG, SPFILE databases. This does not establish CDB/PDB, RAC or Data Guard backup preparation support. |
| Medium | Fleet-wide RAC compliance | Current summaries cannot establish all-node compliance from a primary-node observation. Implement and verify aggregation before claiming that coverage. |
| Medium | End-to-end AI preparation | Chat can diagnose setup and prepare bounded actions. Initial media/README/procedure preparation and independent approvals still use the native wizard; autonomous end-to-end planning is unfinished. |
| Deployment | Company identity and remote server | Provider selection/tenant acceptance and remote installation remain deferred work. The Mac runtime is not proof of remote capacity, TLS, host trust or company login. |
| Architecture | Distributed execution | The controller uses one worker and filesystem state. The lab pull queue is not a qualified distributed control plane. |
| Architecture | Complete route binding on native UI actions | Unlike confirmed assistant actions, ordinary UI execution does not yet bind the click-time configuration through queue admission. Detached reconciliation compares recorded host identity/alias/root but does not seal the `sudo` setting. The operation snapshot fix does not establish those additional guarantees. |
| Release | Exact-runtime acceptance | Fixture receipts, live-lab verification and production approval remain separate. A previous revision's CI pass cannot certify these changes or replace a matching Mac receipt. |

Relevant regression suites are `assistant_workflow.py`, `assistant_api.py`,
`webapp_control.py`, `frontend.mjs`, `evidence_reports.py`, `plan_transport.py`
and the Chromium workflow/integration suites. Consult the current PR checks for
their exact revision, outcomes and logs rather than using this document as a
release certificate.
