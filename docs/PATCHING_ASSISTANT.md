# Patching assistant

The application has a local-model chat page at `#/assistant`. It can run live
patch inventory checks, inspect saved evidence and prepare typed backup,
readiness and patch actions.
Confirming a card submits that exact action through the same authenticated
command handlers, native guards and durable execution registry as the existing
workflow pages. Model output never becomes shell commands or SQL.

Configure an individual authenticated identity (company login or per-principal
credentials), then configure a local model using [LOCAL_LLM.md](LOCAL_LLM.md).
The shared development token cannot own conversations. A configured endpoint
does not establish that the model is installed, reachable or suitable: verify a
real response on the deployment server before enabling customer use.

## Example workflow

1. Ask, “Which databases have readiness blockers?” The assistant reads saved
   evidence. It reports observation times where available; this does not perform
   live discovery.
2. In an operator session, ask "What is the current patch version on targetdb?"
   This explicit observation request immediately starts native live discovery
   through the authenticated command handler. It can synchronize collector tools
   over SSH, update evidence and invalidate previous readiness results. Chat
   waits for that exact run and reports its collected inventory, times and run ID.
   Missing/failed evidence stays unknown; saved snapshots are never a fallback.
   A separate readiness refresh uses requirements and policy saved in the host
   wizard and retains its confirmation card.
3. Ask to prepare a backup request, providing its database, host, backup parent
   and explicit maintenance window. Create and analyze the request using cards.
   Use its native page for the independent approval and execution authorization.
4. Ask to execute the authorized backup and confirm its card. Native preparation
   may stop the database for a cold home backup. A completed backup can be
   validated and selected for this host, then readiness refreshed again.
5. Ask to create a patch plan with its exact patch, database and timezone-aware
   maintenance window. The patch/database must match saved requirements. Review
   and approve/authorize it in the native workflow with the required identities.
6. Ask to dispatch the authorized plan, then execute its remaining tasks. Each
   card is a distinct action. One confirmed execution runs the native stage
   sequence until completion, failure, a blocker or an unresolved outcome.
7. Review the native execution timeline and before/after evidence report. A chat
   action marked “Operation finished” means the native operation returned; its
   result can still report blocked readiness or a failed/paused patch plan.

Missing requirements, unconfigured hosts, insufficient permissions and unknown
execution outcomes remain actionable stops. Chat does not approve requests,
authorize plans, change maintenance windows, reset tasks, waive backup policy,
repair executables, force locks or perform rollback. Existing native pages
provide supported review and recovery operations.

## Confirmation and storage

Inventory questions use live discovery by default. Explicit saved, cached or
historical requests use saved evidence. A target must be one configured host in
the question or the most recent unambiguous user host selection; assistant prose
cannot select it. Missing or ambiguous targets prompt for a host. Viewer/requester
accounts receive operator sign-in guidance without dispatching discovery.

The live answer is formatted by the controller from an immutable receipt of
the actual node payloads returned to that native run. It verifies the run, host,
configuration, collection interval and XML inventory provenance before showing
installed binary patch IDs. Later cache writes and model prose cannot replace
that result. This reports binary inventory per Oracle home; it does not infer
an RU label from the base Oracle version or claim per-patch SQL status from an
aggregate failure count. Partial inventory is identified explicitly.

Live inventory facts are not embedded in application code. Host IDs and SSH
targets come from the deployment's inventory; database names, homes, versions
and installed patches come from each run's collector payload. Generated-data
regressions exercise previously unseen host names (including dots), different
inventories on consecutive runs, and failures after successful checks.

The current live-inventory shortcut recognizes English request patterns with
keyword/regex rules and formats verified facts deterministically. It is not a
general model-driven intent planner. Other chat requests use the local model's
typed tools and reviewed proposals. This distinction matters when evaluating
new phrasing: no claim is made that arbitrary prompts trigger live discovery.

Only this narrow live inventory request dispatches directly. Backup and patch
actions remain proposals requiring confirmation and all native authorization,
approval, readiness and maintenance-window gates. A restart or unknown launch
outcome never automatically replays an operation.
An unresolved native action also blocks confirmation of another proposal or a
new live inventory check in the same conversation until its outcome is
reconciled; dismissing unused proposals remains available.

Conversations are private to their authenticated owner and stored under
`OPU_WEBAPP_STATE_DIR/assistant` (development default: `webapp/var/assistant`).
Messages and structured results are redacted before persistence/model context;
do not paste credentials into chat. Raw credentials, SSH configuration, README
bodies and native logs are not supplied as model tool context.

Proposals expire after 15 minutes and bind exact arguments to saved target and
evidence digests. Confirmation rechecks current permissions and binding. Changed
state requires a new proposal. Confirmed plan dispatch/execution checks the
original binding again when the native worker starts, including every resolved
node's host configuration. Execution keeps that reviewed inventory snapshot for
all tasks in the run, so a later inventory edit cannot redirect a queued or
subsequent task. A durable launch marker prevents double clicks
and retries from repeating work; a lost launch result becomes unknown and
requires native inspection/reconciliation. Restart does not auto-confirm or
auto-resume proposals. Conversations and tool rounds have bounded limits.

Use [REMOTE_DEPLOYMENT.md](REMOTE_DEPLOYMENT.md) for a Linux controller with
versioned code, stable state, TLS and same-server inference. Existing sealed
plans with workstation paths are not automatically migrated or resumed.

## Validation scope

Client tests use a local HTTP simulator; workflow/API/browser tests use isolated
fixtures and a deterministic model simulator. They verify transport boundaries,
typed actions, authentication, confirmation, failure handling and native fixture
execution. They do not establish reasoning quality for an actual model, live
Oracle patch success or production approval. Select a model after checking the
server resources and evaluate real prompts plus controlled failure cases there.
