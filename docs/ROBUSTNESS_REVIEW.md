# Robustness review — 17 September 2026

This review covers the application and native execution code, confirmed fixes
and regression evidence. The first-pass Linux fixture gate passed on `63f032f`
([retained run](https://github.com/akhilreddy-oracle/oracle-Patching/actions/runs/35209437448)).
A second code-first review found additional correctness defects despite that
passing gate. Its coverage and remaining limits are recorded in the
[controller](SECOND_REVIEW_CONTROLLER.md), [native](SECOND_REVIEW_NATIVE.md),
[assistant/transport](SECOND_REVIEW_ASSISTANT_TRANSPORT.md) and
[browser/deployment](SECOND_REVIEW_UI_DEPLOY.md) reports. Consult the retained
exact-source release receipt and draft PR checks for combined validation;
the earlier receipt does not validate changed source.
This document is an audit record, not a production certificate.

The subsequent [third correctness review](THIRD_REVIEW_EXECUTION.md) reproduced
additional delayed-request authorization, confirmed-target and pre-outage state
defects after the second pass's full tests passed. It fixes those boundaries and
replaces in-place remote tool updates with retained, verified code generations.
The older second-pass runtime-race limitation is superseded by that implementation;
the separate Oracle/model/environment acceptance limits below remain.

The second pass preserves reviewed README/media bindings, rejects incomplete
node evidence, binds confirmed AI plan creation through native sealing, excludes
competing HTTP/pull-agent transports, and corrects stale Grid runtime reporting.
It also fixes fleet readiness bindings, revoked company-session cleanup,
configuration read/admission races and diagnostic credential exposure. Current
database adapters now explicitly require **non-CDB** targets. RAC-wide baseline
compliance remains unknown until an all-node aggregation is implemented.

The final cross-check also reproduced SQL admission defects: recovery discarded
earlier result rows, while database adapters selected the last duplicate keyed
value and some registry probes omitted stderr. Recovery now requires one complete
row; the adapters reject ambiguous required values and SQL diagnostics. Live
discovery controls also use a server-derived permission so a requester/viewer
session receives guidance before attempting an operation it cannot execute.

No new live Oracle operation, SSH connection, remote installation or company
identity-provider login was performed for this audit. Two authorized assessments
called the configured local model endpoint using synthetic protocol cases and
executed zero native actions; their limitations and failed acceptance result are
recorded below.

## Review coverage

The review combined full implementation reads, cross-module contract checks,
targeted reproductions and regression tests. It does not claim that every line
of the repository was manually reviewed. Vendored dependencies, virtual
environments, generated output, credentials and saved customer evidence were
outside the manual source review.

| Area | Reviewed scope | Important fixes and checks |
| --- | --- | --- |
| Browser application | All 35 files under `webapp/static`, including navigation, authentication, assistant, fleet, wizard, recovery, execution, reports and shared UI helpers; API integration and browser/frontend regressions | Cancelled requests cannot clear a newer identity or redraw an abandoned execution view. Submitted request IDs remain stable when fields change during a save. Invalid calendar dates are rejected. Failed discovery cannot show a successful refresh. Active/unknown recovery runs block conflicting analysis. Readiness inputs have accessible labels. |
| Controller and authorization | HTTP routes, authentication/company-session boundaries, production configuration, native command-result parsing, host configuration and evidence invalidation | Live discovery requires execution permission; company sessions retain CSRF protection. Credentials and protected configuration files reject unsafe filesystem objects. Invalid production flags fail closed. Failed refresh invalidates derived readiness. Invalid native JSON/status combinations cannot become successful evidence. Duplicate host IDs and invalid connection boolean values are rejected. |
| Assistant and transport | `assistant.py`, `assistant_tools.py`, `live_inventory.py`, `local_llm.py`, model-assessment harness; plan/recovery/lock/provenance control and SSH transfer modules | Current-inventory requests select exact configured targets and use native discovery. Model-backed responses carry a controller-owned action receipt, displayed before labeled model text; unsupported preparation prose cannot establish an actual proposal or execution. Actions retain approved host configuration and recheck authorization. Corrupt conversations and unresolved launches block further work. Remote launch setup failures stop execution. Transfers preserve private state, pinned connection settings and stable controller lock files. |
| Plan and recovery controls | Entire native plan controller, backup-preparation controller, artifact-staging command and compatibility reconciler; lock-recovery/provenance wrappers and Python implementations; recovery-capacity calculation | Stable inode locks replace the reclaimable directory/PID race. Rollback separates every completed source worker from approval and authorization. Safe retry names match real dispatched stages. Backup and lock recovery recheck their window before downtime. Authority-file swaps cannot block on a FIFO. Artifact reports are published atomically. Compatibility blockers return the documented blocked status. |
| Oracle adapters and evidence | Standalone, OJVM, out-of-place, RAC apply/rollback, Grid manual/OPatchAuto and legacy rolling adapters; topology/home discovery; recovery, compatibility, procedure, readiness, OPatch and inventory utilities; common execution/job/queue libraries | Invalid XML and unsuccessful native collection cannot supply authoritative inventory. Service starts do not inherit execution locks. SQL validation checks the latest action. Native failures remain failures, including RAC rollback. Remote arguments are quoted. OPatch upgrade holds host and request locks, binds PMON to the requested home, records service state before downtime and supports interrupted quiesce/restoration recovery. Backup checksums, partial-copy restoration, job transitions and out-of-place oratab metadata have regression coverage. Compatibility collection uses unique private log directories and rejects unsafe or reused outputs to retain prior evidence. RAC rollback shared functions were compared with the fully reviewed RAC apply implementation; differing blocks were read directly. |
| Data Guard | Observe/evaluate/order/orchestration, role-change gates/executors and shared helpers | Standby targets retain their observed roles; downstream consumers verify input seals. Live role-change execution rejects simulation gates, requires recent bound observations and positive broker success plus healthy expected-role postchecks; failed postcheck diagnostics remain available for reconciliation. Readiness binds the exact database/home/role, sealed evaluation and observation time. Plan admission and later authorization recheck the exact earliest evidence expiry, and transport carries both evaluation and order. Targeted safety and integration regressions passed. Data Guard remains a separately scoped native workflow; this audit does not establish company-approval integration or live qualification. |
| Release and deployment | Release-validation source/receipt semantics, deployment/runtime contracts and configuration checks | A receipt belongs to exact source bytes and relevant file metadata. A CI artifact is not automatically interchangeable with a different local snapshot. Fixture testing, live-lab evidence and production approval remain separate states. |

Reports and discovery presentation received focused changed-block reviews in
addition to the controller review. The historical `scripts/build_blueprint.py`
document generator was also read; it is outside the live application workflow.
Contracts, test definitions and deployment assets are also exercised by the release gates; this is distinct from claiming
an independent line-by-line review of every test or documentation file.

## Status of the eight enhancements

All eight areas have application implementation and fixture coverage. The
remaining acceptance work is specific to the deployed environment and supported
Oracle procedure; it must not be represented as already completed.

| Enhancement | Implemented scope | Still required for acceptance |
| --- | --- | --- |
| 1. Backup → validation → approval → patch → health verification | Host Recovery/Readiness/Plan/Execute pages connect sealed backup requests, separate approval/authorization, native preparation, backup selection, patch tasks and database/listener checks. Initial preparation supports standalone non-CDB PRIMARY, READ WRITE, NOARCHIVELOG databases using an SPFILE. | Run and retain a complete approved live cycle for each supported configuration. RMAN restore validation proves backup readability/selection; it does **not** restore and open another database. An actual restore drill remains a separate acceptance task. |
| 2. Actionable readiness blockers | Cards identify the affected target, observed evidence, required condition and relevant next action. Missing measurements remain unknown. Refresh actions rebuild evidence and derived readiness. | Verify representative real backup, inventory, FRA, compatibility and stale-evidence failures against the deployed hosts, including successful remediation through the UI. |
| 3. Guided patch wizard | Database/patch selection, bound README procedure requirements, maintenance-window review, plan inspection and Advanced settings are implemented. Calendar validation and request-identity races have regressions. | Review the real patch README, artifact/platform/OPatch requirements and supported rollback procedure; complete usability acceptance with the intended operators and host configurations. |
| 4. Live execution dashboard | Persistent task timeline, elapsed time, controller contact, observed worker lease, bounded native logs and reconciliation instructions are implemented. Navigating away stops browser polling without cancelling native work. | Validate real network loss, controller restart, slow native stages and reconnection. Connectivity and a lease observation are not proof that a task succeeded. Unknown outcomes must reconcile the existing launch. |
| 5. Before-and-after reports | Custody-verified binary inventory, SQL patch status, invalid objects, components and service observations; JSON, printable HTML and CSV exports. Supported native final stages can establish report completion while missing metrics remain unknown. | Compare exports against live native evidence for the accepted adapter. Rollback still requires separate native lineage, README, recovery, window and approval checks; a report does not authorize it. |
| 6. Fleet compliance | Environment/version/baseline/backup/readiness/freshness filters, priority ordering and administrator-managed environment/baseline metadata are implemented. Unknown or stale evidence cannot establish compliance. | Set each host's intended environment and desired baseline, refresh native evidence, and validate expected classifications across the real fleet. Fleet summaries intentionally display timestamped saved observations. |
| 7. Company login and approvals | Provider-neutral OIDC code flow with PKCE, verified RS256 tokens, group-to-role mapping, expiring sessions, CSRF protection and approval inbox. Existing service credentials support expiry and disabling. | Select the provider and tenant, register the client/callback, configure real groups and secrets, deploy TLS and test tenant login/logout/expiry/revocation and separation of duties. No company provider is selected or accepted by this audit. |
| 8. Automated release validation | Pull-request workflow, native/controller/frontend checks, isolated Chromium tests, failure drills and exact-source receipts. The UI separates fixture, live-lab and production evidence. | Retain matching fixture receipts and collect independently reviewed live-lab evidence and production approval for the exact runtime/configuration. |

The local assistant is an additional capability. Explicit current patch-inventory
questions use a role-authorized native check and its run-specific receipt; saved
evidence is identified as saved. Patch and backup mutations still use typed
actions, confirmation and native approval/readiness controls. Controller-owned
action receipts identify proposals actually prepared in each model response;
current action cards retain subsequent execution states. Model prose cannot
replace those records.

The configured Ollama `qwen3:4b-instruct` endpoint initially passed **0 of 6**
strict synthetic protocol cases. After clarifying the production tool-before-claim
policy and adding literal protocol examples, it passed **5 of 6** with unchanged
expected outcomes. **Model acceptance still failed.** The remaining scope-refusal
case returned no tool call but did not match the required refusal object. The assessment receipt omits the raw final response, so this exact-object
failure alone does not establish unsafe semantic behavior. Both assessments executed zero native actions. These six
instructed cases do not establish general reasoning safety, Oracle workflow
acceptance or model weight provenance. Representative operator evaluation and a
passing assessment of the final configured model remain outstanding.

Local diagnostic receipts: `webapp/var/mac-local/model-assessment-post-audit-20260917.json`
and `webapp/var/mac-local/model-assessment-protocol-clarification-20260917.json`.
They retain source/configuration bindings for their own assessment runs and do not
certify later source changes.

Remote deployment scripts and same-server model configuration are present.
Installation, service startup, TLS, protected credentials, host trust and model
capacity on the intended patching server still require deployment acceptance.
This review did not move or launch the application on that server.

## First-pass validation evidence

The full Linux run executes `make -j4 check` and `npm run test:browser`. These
historical first-pass rows do not validate second-pass edits. Their counts must not be added
together because some suites overlap. The Mac runtime record is independent of
the Linux artifact because source file metadata and runtime differ.

| Check | First-pass result | Evidence boundary |
| --- | --- | --- |
| Frontend Node suites | 95 tests passed | DOM fixtures and application behavior; no Oracle operations. |
| Chromium browser cases | Full Linux browser scope passed | Isolated browser-to-controller/native fixtures, including managed backup selection/readiness/patching and response receipts. No live Oracle or SSH. |
| `native_controller_edges.py` | 4 tests passed | Real local lock contention/unsafe paths, retry stage/outcome matrix and UTC calendar validation. |
| `lock_recovery.py`; `extjob_provenance.py` tests | 40 and 15 passed | Temporary files, process fixtures and mocked Oracle commands, including FIFO substitution and an expiring window. |
| Backup, artifact and controller shell suites | `recovery_prepare`, `artifact_stage`, `patch_plan`, `shell_fail_open_regressions`, `compatibility_reconcile` passed | Complete fixture workflows and rejection paths. Recovery preparation also ran 10 capacity tests. |
| Standalone apply/rollback suite | Passed | Normal and backup-waived apply/rollback, multiple source workers, safe retries and failure/evidence-tampering drills. |
| Assistant suites | Workflow 27, model assessment harness 13, API 18, live API 29, inventory 14, native live-inventory integration 10, offline model-client class 10 passed | Exact-target, authorization, transport and unresolved-run fixtures; no new real model or SSH call. |
| Assistant receipt UI | 19 chat frontend tests and 12 targeted Chromium cases passed | Controller status precedes model prose; zero, positive, malformed and historical receipts, reload, role restrictions and explicit confirmation. Browser cases use API fixtures and overlap earlier targeted coverage. |
| Real model protocol assessment | **Failed: 5 of 6**, improved from 0 of 6 after protocol clarification | Same configured local endpoint; zero native actions. Scope-refusal exact-object compliance remains unresolved; this is not a safety or production certificate. |
| Transport and diagnostics | Plan transport 10, remote transport 13, lock control 19, provenance control 19, execution console 13 passed | Simulated host transport and temporary evidence. |
| Other native adapter checks | A 14-suite targeted batch, OJVM/out-of-place reruns, final Grid rollback/OPatchAuto pair and OPatch-upgrade lifecycle checks passed | Fixture adapters and failure drills, not live Oracle/media qualification. |
| Data Guard and readiness | 8 Data Guard safety tests, `readiness` and `patch_plan_dataguard` passed | Seals, role/target binding, broker proof, retained failure logs, simulated-gate rejection, stale/future evidence, exact expiry and order/evaluation transport. |
| Contract validation | 22 schemas, 3 procedure examples, generated runtime payloads and negative probes passed | Includes the optional strict Data Guard readiness binding contract. |
| Combined Linux release | **Passed** on `63f032f` | Full check and browser scopes in [run 35209437448](https://github.com/akhilreddy-oracle/oracle-Patching/actions/runs/35209437448); unsigned fixture evidence only. |
| Installed Mac release | Read the independently verified local receipt | `release-validation/manifest.json` and `status.json`, displayed by `/api/validation`. Source drift or incomplete/failed logs prevent a passing display; Linux artifacts are never substituted. |

`git diff --check` passed after the completed fixes. The complete gate includes
all newly registered regression suites and the Data Guard readiness/plan
integration. Check the installed Mac receipt without running work:

```sh
.venv/bin/python3 -B scripts/release_validation.py --status --output release-validation
```

The private audit log and model receipts live under `webapp/var/mac-local/`;
they are runtime evidence and are not committed or exposed as credentials.

## Remaining evidence, not inferred completion

- Retain and revalidate exact-source fixture receipts when source or runtime changes.
- Run an approved live backup-to-patch-to-health cycle and required restore drill.
- Qualify each intended Oracle version/platform/topology/patch/rollback combination.
- Resolve any live extjob provenance issue from trusted Oracle media or an approved
  reference; an old backup comparison is not permission to repair privileged bytes.
- Select and validate the company identity provider and deployment configuration.
- Resolve the remaining model protocol acceptance failure, assess the final
  configured model and complete the remote installation.
- Obtain production approval independently; fixture success cannot grant it.

Supporting scope and procedures: [application enhancements](APPLICATION_ENHANCEMENTS.md),
[release validation](RELEASE_VALIDATION.md), [recovery preparation](RECOVERY_PREPARATION.md),
[company login](COMPANY_LOGIN.md), [model assessment](MODEL_ACCEPTANCE.md),
[remote deployment](REMOTE_DEPLOYMENT.md), and [Data Guard](DATA_GUARD.md).
