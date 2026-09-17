# Second review: browser workflow, company login and deployment

Reviewed on 2026-09-17. This is a code-first review of the production files listed
below and their interface contracts. Tests were used to reproduce specific
failure paths and check the fixes, rather than as evidence that all behavior is
correct. The combined repository release gate is tracked separately; its result
is pending for this review snapshot.

## Coverage

Read end-to-end in this review:

| Area | Production files |
| --- | --- |
| Browser entry, navigation, identity and requests | `webapp/static/index.html`, `app.js`, `navigation.js`, `api.js`, `actor.js`, `shell.js`, `auth_recovery.js` |
| Browser rendering, layout and host selection | `webapp/static/dom.js`, `ux.js`, `style.css`, `estate.js`, `fleet.js`, `host_scope.js`, `workspace.js`, `discovery_phases.js` |
| Browser patch, backup, approval and execution workflows | `webapp/static/plans.js`, `recovery_pages.js`, `approvals.js`, `patch_wizard.js`, `plan_window.js`, `procedure_adapters.js`, `backup_policy.js`, `readiness_blockers.js`, `runs.js`, `run_reconciliation.js`, `execution_console.js`, `extjob_inspection.js`, `report_view.js` |
| Host workflow stages | `webapp/static/stages/discover.js`, `readiness.js`, `plan.js`, `execute.js`, `recovery.js` |
| Assistant and validation views | `webapp/static/assistant.js`, `validation.js` |
| Fresh Linux installation | `deploy/controller.py`, `deploy/server-preflight.sh` |
| Deployment templates | `deploy/templates/assistant-config.json.example`, `controller.env`, `deployment.json.example`, `nginx.conf`, `opu-ollama.service`, `oracle-patching.service`, `ssh_config.example` |
| Company identity | `webapp/company_auth.py` |

Additional contract tracing covered the server's authentication/session/logout
and workflow routes, the complete host-inventory validator, the model config
validator, principal parsing, the relevant readiness/procedure schemas, and
the tests changed below. Other reviewers own complete backend, native adapter,
transport, evidence and contract coverage. Third-party package implementations,
generated artifacts, local credential/state files and unrelated repository
lines are not claimed as reviewed by this slice.

## Reasoned invariants and confirmed fixes

| Invariant | Concrete failure found | Correction and regression |
| --- | --- | --- |
| A replaced page cannot act through the new page's read context. An accepted native operation continues independently. | Estate refresh captured the old signal only inside each run poll. After navigation, it could catch cancellation, perform fresh reads using the new global signal, and reset the current host navigation. | Estate now keeps its original signal throughout and stops follow-up reads after cancellation. Host navigation also checks cancellation after asynchronous JSON decoding. Deferred-response Node tests and a real Chromium navigation regression exercise the race. |
| A removed execution widget cannot initiate more native observations. | Re-rendering a plan could remove a follow-logs panel without aborting the route. Its late saved response could still submit an SSH log observation. | Native observation now also requires the panel to remain connected. A regression replaces the panel while the route stays active and verifies zero observation submissions. |
| Revocation denies operations without trapping a browser behind an unusable company cookie. | Company cookies take precedence over service credentials. Logout was behind normal authentication, so a revoked role or changed provider/client binding blocked logout as well as otherwise valid service-token access. | A dedicated logout path only clears that browser's session. Active sessions still require CSRF; revoked/expired cleanup requires the exact configured application origin. No action fields or native dispatch are accepted. Handler and real loopback TLS/OIDC tests verify rejection, deletion, and service-token recovery. |
| Login configuration is inspected and read as one bounded regular-file object. | The OIDC config open could block on a FIFO substituted after `lstat`; metadata was not checked again after reading. | Nonblocking, no-follow open, descriptor metadata checks and change detection precede parsing. A FIFO-swap regression verifies controlled rejection. |
| Valid browser origins match equivalent configured host casing/default ports, and malformed config fails predictably. | Uppercase callback hostnames or an explicit default HTTPS port rejected the browser's serialized Origin. Malformed ports/brackets and a list-valued client-auth method could escape the intended config-error contract. | Origin comparison normalizes hostname case/default ports; URL whitespace/control characters, invalid ports and malformed auth-method types are rejected. Focused login regressions cover these cases and a wrong nondefault port. |
| Installer admission agrees with the application's config semantics. | Deployment admitted duplicate host identities, string-valued `sudo`, nonboolean principal disable flags, invalid expiry values, and assistant fields/types/ranges that runtime startup rejects. | Host validation and the new pure model validator are shared with runtime; principal admission enforces matching types. The installer retains the stricter same-server Ollama policy and distinct active requester/approver/operator identities. Invalid configurations fail before installation writes. |
| Deployment reads protected objects and copies the bytes actually validated. | Input checks were followed by unchecked path reopen operations, and installation reread source inputs after mutation had begun. | Descriptor-bound bounded reads reject substituted files, symlinks, FIFOs and concurrent changes. Administrator input ancestors must also be root-owned and not group/world writable. Installation revalidates before mutation and copies that exact in-memory snapshot. Regression tests substitute inputs at admission and after installation starts. |
| A correctly hashed archive still needs its installation dependencies. | A bundle containing only the two entry scripts could pass admission and fail after installation had created paths because requirements/templates were absent. | Bundle admission requires the installer-used templates, requirement files and shared validation modules. A valid manifest/hash with missing dependencies is rejected before installation. This is not a claim of arbitrary package semantic equivalence. |

The procedure guide now states the native review's supported database scope:
**non-CDB databases only**. It does not advertise CDB/PDB patch completeness.
The native reviewer owns the corresponding runtime/readiness gates. The
out-of-place procedure method `switch_home` was also traced to a missing
readiness schema enum and handed to that reviewer for the contract correction.
The fleet baseline cell displays the backend's explicit all-node coverage
limitation as plain text; a primary-node inventory is not presented as verified
RAC-wide compliance.

The remaining interfaces were traced with these assumptions explicit:

- Browser permission hints and disabled controls are conveniences. The server
  must re-authenticate the principal and enforce the operation's role, approved
  inputs, plan state, maintenance window and native execution lock.
- Model prose is rendered as text. Controller receipts and durable native
  records determine whether a proposal exists or an operation completed. The
  browser does not convert model output into shell commands or approval.
- Saved report, readiness and fleet values retain their provenance/freshness
  labels. A successful transport/run alone cannot establish current database
  health or production acceptance.
- The installer operates on a reviewed fresh Linux host with root-controlled
  system directories and reviewed tooling matching the bundle. A separately
  retained archive digest binds transferred bytes; it is not a signature,
  security review or approval. Payload inspection rejects unsafe member paths,
  links, duplicates, excessive expansion and manifest/content mismatch.
- Installation creates a stopped controller and staged nginx configuration.
  It reloads systemd unit definitions without starting the application or
  contacting Oracle hosts. Existing paths/accounts stop admission; interrupted
  installation deliberately leaves partial paths for administrator inspection.
  There is no implemented upgrade, automatic rollback or sealed-state migration.
- The deployment service binds the application and model to loopback, uses
  separate unprivileged accounts, and retains canonical persistent state paths.
  Managed-host permissions and pinned SSH identities are separate deployment
  inputs, not provisioned by installing the controller.
- OIDC uses signed RS256 tokens, one-use browser-bound state, PKCE, exact issuer
  and audience/nonce checks, short-lived local sessions and current local group
  role mappings. Provider-group membership changes take effect on the next
  login/expiry; real-time directory revocation is not claimed.

## Focused verification

All new reproductions use disposable inputs, synthetic Oracle/API responses,
or isolated loopback identity-provider/browser fixtures. No managed Oracle
host, actual company tenant, remote installation or local model inference was
used by this review.

| Check | Result at this slice's completion |
| --- | --- |
| `tests/company_auth.py` | 13 passed |
| `tests/company_auth_api.py` | 9 passed |
| `tests/company_auth_http.py` | 6 passed, real local TLS/OIDC HTTP exchange and cookie jar |
| `tests/remote_deployment.py` | 16 passed, no root/systemd/SSH operations |
| Frontend target: `frontend.mjs`, `release_ux.mjs`, `fleet_metadata.mjs`, `chat_frontend.mjs` | 99 passed |
| Chromium: `workflow.spec.mjs` and `company_session.spec.mjs` | 14 passed, mocked APIs with external access denied |
| Shared model config validator | 11 offline local-LLM tests passed, reported by its owning reviewer |
| `tests/server_preflight.py`, `tests/runtime_package.py` | 4 and 9 passed |
| Complete repository gate | Pending coordinated source freeze and full run |

## Remaining acceptance and limits

- These checks do not establish live backup restoration, patch success,
  container-wide SQL correctness, supported rollback, or database/listener
  availability on any customer topology. Native adapter scope and real Oracle
  media/version acceptance remain separate requirements.
- A chosen company provider still needs tenant registration, real group claims,
  secret delivery, expiry/revocation exercises and HTTPS deployment acceptance.
  The local provider fixture proves the exercised protocol paths only.
- Remote Linux/systemd/nginx/SELinux/firewall behavior, storage permissions,
  controller-to-host SSH trust, and server/model capacity have not been verified
  on the proposed patching server. TLS preflight checks certificate/key pairing;
  hostname/SAN, expiry and the client trust chain still need deployment review.
- Packaging excludes named credential/state/cache locations; it does not scan
  arbitrary source files for embedded secrets. Build from a reviewed clean
  checkout and inspect the manifest before transferring it.
- A partial fresh install needs an administrator's inspection and recovery.
  Removing old state, automatically switching releases, or re-signing changed
  absolute paths would violate the current custody model and is not implemented.
- Reading CSS/HTML and running the focused browser paths does not constitute a
  complete screen-reader, cross-browser or every-viewport accessibility audit.
  The page still references external Google Fonts, with local font fallbacks;
  a fully isolated deployment may block those optional font requests.
- Model behavior and protocol acceptance are owned by the separate assistant
  review. No passing UI, deployment or identity test establishes model reliability
  or grants authority to patch without the native application controls.
