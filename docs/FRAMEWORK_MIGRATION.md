# Framework adoption and engineering acceptance

Decision date: 18 September 2026. Status: first migration stage implemented;
subsequent stages remain proposals. Passing checks must be established by an
exact-source validation receipt, not by this document.

## Decision and scope

Adopt FastAPI, Pydantic and Uvicorn incrementally around the existing application
services. Retain the native Oracle executors and their safety contracts. This
reduces manual HTTP plumbing while allowing each migrated route to be reviewed
against its previous authorization, evidence and execution behavior. A framework
does not prove application correctness or certify a supported Oracle workflow.

The implemented first stage is:

| Component | Current implementation |
| --- | --- |
| Startup | `python -B webapp/server.py` validates configuration and starts one Uvicorn worker on `127.0.0.1`. Existing port and optional TLS certificate/key environment settings remain supported. Forwarded-header trust and access logging are disabled. |
| HTTP controller | `server.Controller` owns existing authentication, roles and native action dispatch without constructing a socket or HTTP listener. `server.Handler` remains as a compatibility adapter for existing HTTP fixtures. |
| ASGI transport | `api_transport.py` adapts requests and responses to a private controller per request. It bounds and validates the request body, preserves multiple cookie headers, and sends blocking controller work to a thread pool. Native background run ownership remains in `pipeline_runner.py`. |
| Typed routes | `api.py` and `api_models.py` define response contracts for `GET /api/health`, `/api/session`, `/api/auth/whoami`, `/api/validation` and `/api/approvals`. `application_views.py` supplies their framework-independent read services. |
| API schema | Authenticated readers can retrieve `/api/openapi.json`. It documents only the migrated route group. The public interactive documentation pages are disabled; remaining routes are not yet represented in OpenAPI. Health remains a public liveness probe. |
| Other routes | Existing GET/POST routes pass through the ASGI bridge to their controller and domain services. Mutation endpoints have not all acquired individual Pydantic request/response models. |
| Dependencies | `webapp/requirements-api.txt` contains the reviewed direct API and test-client pins. The Mac launcher checks these dependencies before starting services; CI and the fresh Linux installer install the file and run `pip check`. Native managed-host tools do not depend on FastAPI. |

The ASGI server is an HTTP runtime. It does not replace the workflow engine.
`pipeline_runner.py` still uses background threads, process-local registries,
filesystem locks and persisted run records. Live controller execution still
launches pinned native workers over SSH. The available pull queue is a lab/shared
filesystem mechanism, not a qualified distributed control plane. Multiple API
workers and multiple controllers are not supported by this migration.

## Invariants across every migration stage

- Authenticated identity and current permissions are enforced by the controller
  and checked again at the existing execution boundaries. Framework dependencies
  cannot replace independent approval or authorization.
- Native plans/tasks, required backup/readiness evidence, maintenance windows,
  host locks and leases remain authoritative. Schema validation cannot waive a
  blocked native operation.
- A reviewed assistant action stays bound to its exact target, configuration and
  evidence. The model can propose typed actions; human confirmation and native
  gates govern their submission. The model cannot grant itself permissions or
  execute arbitrary shell commands or SQL.
- Ambiguous launch or completion outcomes remain unresolved until native
  evidence establishes a result. A timeout, disconnect, process restart or
  framework retry must not automatically repeat a destructive operation.
- Workers use their recorded immutable code generation and stable state paths.
  Runtime publication must not change an already-started worker's code.
- Current-inventory answers require the application's verified live discovery
  receipt. Saved evidence is labeled with its observation time; it cannot become
  a live result because a model or HTTP request completed successfully.
- Source-bound fixture results, live lab evidence and production approval remain
  separate. None of the framework's schema or health endpoints can confer the
  latter two levels.

## Security requirements and evidence checklist

The [OWASP Application Security Verification Standard](https://owasp.org/projects/asvs)
provides a basis for evaluating application security controls. The following is
a project checklist inspired by that approach, not a complete ASVS mapping or
certification. No ASVS requirement identifiers or achieved assurance level are
claimed. Listed tests are evidence locations; their presence is not proof that a
particular source revision passed them.

| Area | Project requirement | Evidence to review |
| --- | --- | --- |
| Authentication and sessions | Reject absent, expired, revoked and changed identity; preserve company-session CSRF checks through delayed requests and action submission. | `tests/asgi_api.py`, `tests/company_auth_api.py`, `tests/company_auth_http.py`, `tests/webapp_control.py`; separate acceptance for the selected identity-provider tenant. |
| Authorization | Deny unauthorized routes and cross-identity actions; retain requester/approver/operator separation and fresh permission checks. | Controller route policy, native plan/recovery gates, `tests/assistant_api.py`, `tests/patch_plan.sh`, `tests/recovery_api.py`. |
| Input and output handling | Bound body size/time, reject ambiguous headers and JSON, prevent nonfinite numeric input, and validate migrated response contracts without exposing submitted secrets in errors. | `webapp/api_transport.py`, `webapp/api_models.py`, `tests/asgi_api.py`; review each mutation route as it receives its own model. |
| Execution integrity | Bind approvals to target/evidence, enforce exclusive execution, and reconcile unknown outcomes before retry. | `tests/plan_transport.py`, `tests/controller_evidence_integrity.py`, `tests/native_controller_edges.py`, native failure drills and retained task/launch records. |
| Secret and evidence handling | Protect credentials and sessions; redact diagnostics; preserve evidence provenance and reject altered or stale receipts. | Authentication/storage code review, diagnostic and evidence tests, `tests/controller_evidence_integrity.py`; deployment permission and log inspection. |
| Transport and deployment | Bind the controller to loopback, terminate remote TLS as configured, disable untrusted forwarded identity, and preserve OIDC callback secrecy. | `tests/asgi_api.py`, `tests/remote_deployment.py`, deployment templates; actual TLS/proxy/service acceptance on the destination server. |
| Dependencies and runtime updates | Review direct pins, verify package resolution, require complete deployment/native bundles, and retain old worker generations. | Requirements files, `pip check`, `tests/remote_deployment.py`, `tests/runtime_package.py`; approved dependency sources and deployment manifest. |
| Audit and recovery | Record who requested, approved and executed work; retain blocked/failed/unknown results and evidence; demonstrate recovery for each claimed supported workflow. | Run timelines, native audit/task records, recovery tests, real restore and rollback drill evidence for the claimed Oracle configuration. |

## Definition of done for a supported workflow

1. **Define the supported case.** Record the Oracle release/platform, topology,
   patch family, adapter, deployment mode and policy. Unsupported cases must
   produce an explicit blocker, including current non-CDB boundaries where
   applicable.
2. **Review the complete execution path.** Trace UI/chat input through identity,
   policy, persisted state, native execution and result publication. Review
   failure and concurrency paths independently of the implementation's happy
   path tests. Address concrete findings before acceptance.
3. **Prove fixture behavior at meaningful boundaries.** Exercise actual
   controller and native code where feasible. Test denied roles, stale or
   changed evidence, duplicate submissions, target drift, malformed input,
   disconnects, interrupted launches and unknown-result reconciliation. A test
   that merely repeats the implementation is insufficient evidence.
4. **Retain exact-source validation.** Run the applicable contract, API, native
   and browser checks, including relevant failure drills. Preserve commands,
   exit codes and hashed logs in a source-bound receipt. Source changes require
   fresh evidence for the affected checks and the release gate.
5. **Perform separate live lab acceptance.** Use the same code generation and
   recorded configuration to demonstrate the intended backup, restore
   validation, approval, patch and final database/listener checks. Demonstrate
   actual restore/open and supported rollback through separately planned drills;
   an RMAN validation result alone is not proof of a successful restore.
6. **Accept deployment and AI behavior separately.** Verify the destination
   service/proxy/TLS, configured identity provider, credential expiry, restart
   and reconciliation behavior. For chat, record evaluations for the exact local
   model/configuration, including missing evidence, denied permissions and
   unsupported requests. A deterministic model simulator does not qualify the
   real model.
7. **Record the decision and remaining limits.** Independent production change
   approval and the supported certification process must identify the exact
   release/configuration and accepted recovery procedure. Keep unverified
   capabilities visibly unverified. Do not promote old lab results to a newer
   revision or describe green fixture checks as complete correctness.

See [release validation](RELEASE_VALIDATION.md), the
[third correctness review](THIRD_REVIEW_EXECUTION.md) and
[remote deployment](REMOTE_DEPLOYMENT.md) for existing evidence boundaries.

## Later phases — not implemented by this change

| Phase | Proposed work | Acceptance before adoption |
| --- | --- | --- |
| More typed routes | Extract mutation services and migrate route groups into explicit Pydantic request/response contracts. Expand protected OpenAPI coverage. | Preserve existing error, identity, confirmation and native gate semantics; test real HTTP and browser paths for each group. |
| Durable orchestration | Evaluate a durable workflow engine such as Temporal if scheduling, restart recovery or multi-controller requirements justify it. | Specify workflow ownership, idempotency, reconciliation and state migration; prove that engine retries cannot repeat Oracle mutations. No Temporal runtime is installed by this stage. |
| AI orchestration | Consider LangGraph only if conversation branching and checkpointing become difficult to maintain with the current typed tool flow. | Preserve controller authority and target binding; evaluate prompt injection, model failures and replay behavior. No LangGraph runtime is installed by this stage. |
| Remote acceptance | Qualify the actual destination installation, identity provider, local model and supported live Oracle workflows. | Retain configuration-specific acceptance and recovery evidence. Packaging and fixture success alone do not complete deployment or production approval. |

These phases are independent decisions. Adding another framework is not a
prerequisite for completing the current native workflow acceptance work.
