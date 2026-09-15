# Patching assistant

The application has a local-model chat page at `#/assistant`. It can inspect
saved estate evidence and prepare typed backup, readiness and patch actions.
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
2. Ask to refresh a specific host. Review the host and effect on the action card,
   then confirm. Discovery can synchronize tools over SSH and update saved
   evidence. A readiness refresh uses the requirements and policy already saved
   in the host wizard; the model cannot change or waive them.
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

Conversations are private to their authenticated owner and stored under
`OPU_WEBAPP_STATE_DIR/assistant` (development default: `webapp/var/assistant`).
Messages and structured results are redacted before persistence/model context;
do not paste credentials into chat. Raw credentials, SSH configuration, README
bodies and native logs are not supplied as model tool context.

Proposals expire after 15 minutes and bind exact arguments to saved target and
evidence digests. Confirmation rechecks current permissions and binding. Changed
state requires a new proposal. A durable launch marker prevents double clicks
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
