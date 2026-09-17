# Second code review: assistant, transport and supporting services

Historical second-pass record. Runtime generations and confirmed plan target
bindings received further fixes in the [third review](THIRD_REVIEW_EXECUTION.md).

Reviewed on 2026-09-17. This was a code-first review of the final implementations
and their callers, followed by isolated reproductions. Passing fixtures were not
used as evidence that Oracle patching, restore, company identity, or remote
deployment works in a real environment. No Oracle connection, SSH action, model
inference, external notification delivery, or deployment was performed during
this second review.

## File coverage

Each file below was read end to end. Additional caller traces were limited to the
named integration points; this document does not claim a second complete review
of those other modules.

| File | Paths and boundaries reviewed |
| --- | --- |
| webapp/assistant.py | Private conversation ownership; persisted record validation; turn ownership; live query target selection; exact-run inventory answers; model/tool loop; proposal receipts; confirmation, deduplication and unknown outcomes. |
| webapp/assistant_tools.py | Role-filtered typed schemas; argument validation; bounded saved observations; native-free backup reads; target matching; proposal bindings; fixed routes and native run keys. |
| webapp/live_inventory.py | Exact configured run/configuration association; collection timestamps; XML inventory provenance; partial coverage; bounded receipts; integrity verification and direct formatting. |
| webapp/local_llm.py | Protected configuration; parser and endpoint policy; DNS/address pinning; bounded request/response transport; deadlines; credentials; strict function-call parsing and no fallback. |
| webapp/remote.py | SSH argument boundaries; checked exits; producer/consumer lifecycle; private transfer credentials; authenticated destination host-key pinning; forced commands; cleanup failures; bounded file reads. |
| webapp/agent_queue.py | Durable publication, node/plan ordering, generations, token fencing, expiry, native terminal verification, reconciliation, and HTTP-versus-pull transport admission. |
| webapp/agent_worker.py | Claim/ready/defer lifecycle, fixed executor selection, heartbeat loss, terminal evidence and diagnostic persistence. |
| webapp/agent_enroll.py | Registry safety, identifier/token binding, node enrollment, rotation/revocation, public views and serialized writes. |
| webapp/tools_sync.py | Single content snapshot for archive/hash, complete runtime packaging, installation ordering, host/process locks, active-executor exclusion and stamp caching. |
| webapp/itsm.py | Enforcement toggle, registry parsing/protection, ticket identity ambiguity and fail-closed approval lookup. |
| webapp/notifications.py | Local event/dead-letter persistence, configured webhook matching, credential redaction, destination handling, error isolation and append concurrency. |
| webapp/diagnostics.py | Recursive field redaction; authorization, URL, SQL login and private-key text handling; bounded rendered diagnostics. |
| webapp/fleet.py | Observation freshness, database/home matching, readiness dependency hashes, binary inventory provenance, backup age and RAC baseline scope. |
| webapp/fleet_metadata.py | Protected sidecar reads, typed metadata, optimistic revision checks, actor attribution and atomic writes without host connection changes. |
| scripts/model_acceptance.py | Synthetic-only scenarios; production client configuration; exact typed expectations; source/configuration drift detection; exclusive private receipts and honest classification. |

Integration traces included server agent/assistant/approval routes,
planctl publication and local-to-remote plan synchronization, pipeline procedure
input shape, native readiness/procedure document output, and the shared native
host execution lock.

## Confirmed defects and changes

1. **A reviewed chat plan could lose its exact patch/database scope.** The
   assistant route dropped those values and native asynchronous creation reread
   current evidence. The assistant now forwards the original approved
   creation binding plus the exact patch and database. Its binding delegates to
   the shared evidence helper. The root review added native verification while
   holding the host evidence lock through plan creation; host evidence writers
   use that same exclusion. A changed policy after review cannot silently become
   the action's new digest.

2. **Pull-agent execution and HTTP state transfer did not share admission.**
   Publishing queue tasks did not reserve the HTTP execution path. Importing
   controller task files could overwrite a pull worker's running claimant/lease,
   even when the native mutation lock prevented a second Oracle command.
   The root review owns the shared per-plan transport lock around publication
   and HTTP execution. The queue now supplies a locked fail-closed admission
   check: queued, claimed, running, reconciliation-required, malformed and
   unverified completed attempts prevent HTTP work. Completed production
   attempts must contain consistent, same-generation native terminal evidence.
   Queue record reads are bounded, nonblocking, no-follow and type checked.

3. **Tools synchronization could replace code beneath an active executor.**
   Installation now takes the native host mutation lock from the staged package
   before moving any installed runtime directory, holding it until exit.
   A held-lock reproduction preserves the old bin/lib/operations bytes and
   fingerprint and cleans staging. This is active-executor protection, not an
   atomic runtime-generation implementation; see the remaining limitations.

4. **Fleet readiness used a field shape the real wizard does not produce.**
   It looked for a flat database name while the submitted procedure stores
   target.database_unique_name. The view now uses the real nested target and
   verifies the validated procedure/input relationship, patch ID and all native
   readiness dependency hashes, including optional recovery/Data Guard evidence.
   Changed dependencies yield unknown readiness. Binary compliance also requires
   XML inventory provenance and a valid checksum. Configured or discovered multi-node hosts
   remain unknown for baseline compliance because this view currently holds
   primary-node observations; the API and UI explicitly explain that limitation.

5. **Ambiguous ITSM registries could approve a ticket by first match.**
   Conflicting duplicate IDs, whitespace aliases, malformed entries and duplicate
   JSON authority fields now reject approval. Reads also reject unsafe linked,
   nonregular, oversized or writable registry files without blocking on FIFOs.

6. **Diagnostics could persist or transmit credential-shaped data.**
   Agent failure stderr now passes through the shared redactor before entering
   queue records; the sync package includes that dependency. Notification
   payloads are redacted before persistence and webhook delivery. Dead letters
   retain the destination hostname/error class instead of an opaque webhook path
   or exception containing credentials. Event files are private and refuse
   symlink, hardlink and FIFO targets; append locking has a bounded wait.

7. **Deployment and runtime configuration validation could disagree.**
   The running local-model loader now delegates to the pure
   local_llm.validate_config(raw) parser. Deployment uses this same contract
   while keeping its stricter installation policy and file checks. The pure
   function performs no I/O or network access. Model scenarios, their expected
   responses, and the acceptance evaluator were not weakened.

## Isolated verification

The focused checks completed during this review include:

- assistant workflow: 28; assistant API: 18; live assistant API: 29.
- saved assistant inventory: 14; exact-run live inventory receipts: 10.
- local LLM configuration/transport: 14, including a loopback HTTP simulator.
- model assessment harness: 13 simulator tests, including rejection of fake
  preparation prose, substituted inspection tools and wrong refusal enums.
- remote transport: 13; agent queue/worker: 22; extracted runtime package: 9.
- fleet: 14 (including discovered clusters without configured nodes); fleet metadata: 7; fleet API: 7.
- integration shell checks: ITSM ambiguity, credential redaction, actual mocked
  webhook payloads, unsafe log targets and malformed configuration.

The review also ran targeted ShellCheck and diff whitespace checks. These are
focused results, not a replacement for the root agent's final combined release
gate on the final source.

## Remaining assumptions and limitations

- **Runtime generations are not immutable.** An executor can begin sourcing
  libraries before it reaches the native host lock in main; read-only collectors
  do not hold that mutation lock. The new installer exclusion protects active
  mutation owners but does not eliminate every startup/collector version race.
  Production-grade atomic updates require immutable versioned runtimes or an
  earlier shared bootstrap protocol across every entry point.
- **The pull queue remains a filesystem lab transport.** Enrollment is hashed
  token/node identity, not mTLS or a distributed queue. Publication outside the
  normal controller path and execution by another controller using a separate
  state root are outside the shared-lock guarantee. Private controller state
  and consistent lock/state configuration remain requirements.
- **RAC fleet baseline aggregation is not implemented.** The display keeps the
  primary node's dated inventory while reporting overall baseline compliance as
  unknown. This is not all-node compliance proof.
- **Generated language is not authority.** The controller-owned action receipt
  and native gates decide what was prepared/executed; plausible model prose can
  still be incorrect. The last authorized real model smoke run passed 5/6,
  failed the exact scope-refusal object, and executed no native actions. That
  receipt predates this second review's source changes and is not acceptance of
  the current source. Full model acceptance remains failed/pending.
- **Six synthetic protocol cases are not a model safety benchmark.** The
  evaluator still demands the same exact tools, arguments and refusal objects.
  It does not attest model weights, real restore capability or patch safety.
- **Remote support is intentionally constrained.** Direct transfer requires
  conventional Linux SSH host public-key files, IPv4 reachability, port22 and
  the expected authorized_keys layout. Cleanup uncertainty blocks automatic
  fallback. No real managed-host verification was repeated in this review.
- **Not every subprocess buffer is bounded.** The model HTTP client is bounded
  and relay producer stderr is spooled, but general remote command output and
  queue-worker communicate() still buffer output. Mutating workers deliberately
  do not kill an Oracle process merely because a queue heartbeat was lost;
  reconciliation remains necessary.
- **Integrations are limited implementations.** ITSM is a protected local
  registry lookup, not a live vendor ticket/window integration. Notifications
  are best effort, have no retry worker, and synchronous delivery can delay the
  caller by configured webhook timeouts. A log lock/delivery failure can drop
  notification evidence without changing the native operation result.
- **Administrative configuration remains trusted.** SSH configuration, runtime
  roots, external webhook destinations, model service behavior, credential
  files and protected state must be administered consistently. Redaction is
  pattern-based and cannot recognize arbitrary unlabeled secrets.

No production approval, complete restore, live patch cycle, company SSO
interoperability, or remote server deployment is established by this review.
