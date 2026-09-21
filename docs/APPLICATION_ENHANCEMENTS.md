# Application enhancements

The application now connects recovery preparation, patch planning, execution
observations and evidence review. Open the local controller at
`http://127.0.0.1:8765`. These capabilities do not themselves establish live lab
verification or production approval for a new release.

| Capability | Where to use it | Evidence and limits |
| --- | --- | --- |
| Backup through patch verification | Host workspace: Recovery, Readiness, Plan, Execute | Live preparation initially supports standalone PRIMARY, READ WRITE, NOARCHIVELOG databases with an SPFILE. Independent backup approval and authorization precede downtime; patch approval remains separate. RMAN restore validation checks backup readability and selection, not a restoration onto another database. |
| Actionable readiness blockers | Host workspace: Readiness | Cards identify the target, actual evidence, required value and a relevant next action. Missing measurements stay unknown. |
| Guided patch planning | Host workspace stage navigation | Target and patch selection, bound README requirements and UTC window review precede sealing. Technical procedure and policy fields are under Advanced settings. |
| Execution dashboard | Plan detail | Saved stage events, elapsed time, controller contact, observed native lease and bounded logs survive page reloads. An unknown outcome still requires reconciliation of the original launch. |
| Before and after reports | Plan detail | Verified task evidence supplies comparisons for inventory, SQL patch status, invalid objects, components and service health. JSON, printable HTML and CSV exports retain evidence gaps. Rollback requires the existing supported native eligibility and approval checks. |
| Fleet compliance | Hosts page | Filter saved observations by environment, Oracle version, baseline, backup, readiness and evidence freshness. Administrators can set or clear each host's environment and desired patch ID in the inline configuration form. Missing, stale or mismatched observations cannot establish compliance. |
| Company login and approvals | Session controls and Approvals | Optional OIDC configuration maps company groups to application roles. Sessions expire and writes require CSRF protection. Existing service credentials support expiry and disabling. The inbox links to the original independently approved requests. |
| Release validation | Release validation navigation and pull request CI | Exact-source fixture receipts include native failure drills and isolated Chromium tests. The page displays fixture testing, live lab verification and production approval separately. |

## Company identity provider

No provider or tenant is selected by default. Configure a standards-based OIDC
issuer later without changing application routes: issuer, client, callback URL,
group claim, group-to-role mapping and client authentication method are deployment
settings. Supported token endpoint methods are `client_secret_basic`,
`client_secret_post` and public-client `none` with PKCE. ID token verification
currently supports RS256. Provider-specific claim configuration and actual tenant
login must be validated when a provider is chosen.

See [company login](COMPANY_LOGIN.md),
[recovery preparation](RECOVERY_PREPARATION.md), and
[release validation](RELEASE_VALIDATION.md) for configuration and evidence details.
The [execution dashboard](EXECUTION_DASHBOARD.md) and
[evidence report](EVIDENCE_REPORTS.md) guides describe observation and export limits.

## Correcting configuration and blocked workflows

The host lifecycle rail reads host-scoped saved recovery summaries without
contacting managed servers. It labels the state as saved and shows its observation
time when recorded; legacy observations retain an unknown time. Opening Recovery
still performs the existing native status refresh. Saved navigation state never
authorizes backup selection or execution.

Readiness findings that refer to another section on the same page focus and scroll
to that section without discarding form drafts. Discovery and readiness updates
also refresh the workspace's saved target and lifecycle badges, so the header
does not keep showing a previous database or patch after evidence changes.

The Plan page reads one locked evidence generation and submits that generation's
confirmation with its displayed patch and database. Changes made by another
session require a new review before creation. This includes recovery policy and
Data Guard ordering. The older plan-creation route uses the same review and
target display. A blocked readiness result has no creation confirmation.

Artifact remediation does not search other machines when the page opens.
An operator explicitly selects **Probe managed hosts**, which explains the SSH
scope and sends a permission-checked POST with CSRF protection for company
sessions. Editing the path invalidates any old source selection or in-flight
result. Legacy GET callers require the same operator permission and CSRF proof.

Fleet metadata edits require the `admin` role when RBAC or company login is
enabled. The existing authenticated lab mode can also edit these fields. Values
are retained in `webapp/var/fleet-metadata.json`; host connection settings are
unchanged. Empty fields explicitly clear a classification or desired baseline.
Concurrent edits return a conflict and require a reload before saving. Editing
a baseline does not refresh discovery or establish patch readiness.

Recovery shows a host-specific comparison of observed and required properties.
Stale, absent or unsupported discovery blocks request creation. Eligible saved
discovery permits creating a request for native analysis only: SPFILE, capacity
and current database state still need native verification. RMAN restore validation
checks backup readability; it does not restore a separate test database.

Running and paused plans explain closed or unknown maintenance windows and
disable execution/retry controls. The controller independently enforces its
sealed window. A partially completed plan retains its successful tasks; the UI
does not propose repeating an apply to bypass an expired window. Evidence reports
include bounded, redacted failure logs only after their custody hashes verify.

## Validation and deployment

Install `webapp/requirements-sso.txt` in the controller environment before enabling
company login. Run the commands in the release-validation guide to create a
fixture evidence bundle for the current source. New source changes invalidate
that bundle until checks are rerun. CI starts on the configured GitHub events
after the workflow is committed and pushed.

Deploying the application does not resume a paused plan, repair an Oracle host,
create a backup or apply a patch. Those operations continue through their
explicit application controls and native admission checks.

## Acceptance still required

The eight implemented surfaces above are not eight accepted production
capabilities. The next acceptance work must demonstrate:

- A complete live lab backup, supported restore-and-open drill, approval,
  patch, database/listener verification and evidence report for the same release.
  RMAN validation and simulated Oracle commands do not establish this result.
- Interrupted execution reconciliation and supported rollback on each claimed
  topology. Existing unresolved runs retain their evidence and remain blocked.
- Reliable local-model behavior in the actual conversation loop. Chat cannot
  yet bootstrap artifact inspection, reviewed README requirements and policy on
  an unprepared host; those inputs must first be supplied through the wizard.
  See the separate [model assessment](MODEL_ACCEPTANCE.md).
- Login, logout, role mapping and approval separation against the company
  identity provider after it is selected, followed by remote deployment checks.
- Fixture evidence matching the exact installed source, followed separately by
  live lab acceptance and production signoff. An older passing receipt cannot
  validate new changes.
