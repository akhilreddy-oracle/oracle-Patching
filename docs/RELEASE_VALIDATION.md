# Release validation

Pull requests run `make -k -j4 check` and the Chromium browser workflow in
`.github/workflows/release-validation.yml`. The job uses disposable fixtures,
read-only repository permissions and no database credentials. The workflow is
prepared in the repository; a passing local run does not prove GitHub has run it.

Make continues independent suites after a failure and retains a failing exit
code; targets that depend on a failed prerequisite are skipped. This does not
continue inside a failed test script, after a Makefile parse error, or past a
timeout or interruption. The collector proceeds to the browser scope after a
normally completed check command, even when checks fail. CI first requires the
validation-reader tests to pass before it invokes that collector.

## Three distinct evidence levels

| Level | Required evidence | What a passing fixture job establishes |
| --- | --- | --- |
| Fixture-tested | Passing command receipts and hashed logs for the exact source tree, with the tested scopes listed | The listed contract, unit, fixture or browser checks passed |
| Live-lab-verified | A separately retained lab run for the same runtime digest, Oracle version/platform, topology, patch and policy; native backup/restore-validation, task, database and listener evidence; reviewer identity | Nothing; this level remains unverified |
| Production-approved | Independent change approval, the supported production certification process, exact release/configuration binding, expiry and an accepted recovery procedure | Nothing; this level remains unverified |

Historical sourcedb runs apply to their recorded runtime and configuration. They
are not carried forward automatically to new code. A marker file, a passed browser
test or an old certificate cannot grant current production approval.

`python3 scripts/release_validation.py` runs the requested checks and writes a new
bundle containing `manifest.json`, `status.json` and command logs. The manifest
binds file contents and modes before and after execution, the exact commands,
exit codes and log hashes. Source drift, missing evidence, changed logs or failed
commands prevent a passing status. Existing bundle directories are never
overwritten.

The manifest is a local audit record, not a cryptographically signed release
attestation. Its reader must never be used to authorize patch execution or grant
production certification. `validation_status(root, bundle)` is a read-only helper
for displaying the distinction in the application. Live and production statuses
remain unverified here until a separate reviewed evidence integration exists.

## Run locally

Use Node 22 or later, Python 3.10 or later, Bash, jq, ShellCheck and `xmllint`
(`libxml2-utils` on Ubuntu). Install the contract, API and company-login test
dependencies in the project environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt -r webapp/requirements-sso.txt -r webapp/requirements-api.txt
.venv/bin/python -m pip check
source .venv/bin/activate
npm ci --ignore-scripts
npx playwright install chromium
python3 -B tests/browser/release_validation_test.py
python3 -B scripts/release_validation.py --suite all --output release-validation
```

Ensure `python3` used by Make can import the API and company-login dependencies,
for example by activating `.venv` before running the checks. On Linux, install
browser system dependencies with `npx playwright install --with-deps chromium`.
The workflow installs these on its disposable runner. For a restricted local
cache, set `npm_config_cache` and `PLAYWRIGHT_BROWSERS_PATH` to writable test
locations; no global installation is necessary.

`tests/asgi_api.py` uses HTTPX `AsyncClient` with `ASGITransport`, together with
direct ASGI messages for malformed framing and delayed-body tests. It does not
use Starlette's `TestClient`. Starlette 1.6.0 warns that its legacy HTTPX
`TestClient` compatibility is deprecated in favor of HTTPX2; that warning does
not require a test-client migration here. The real browser integration harness
also exercises Uvicorn startup and the application's lifespan hooks, which an
in-process HTTPX transport does not start automatically.

For one scope, use `--suite check` or `--suite browser` and a new output directory.
A browser-only bundle explicitly reports scope `browser`; it is not evidence that
the native executor tests passed. Inspect a bundle without running anything:

```sh
python3 scripts/release_validation.py --status --output release-validation
```

## Browser isolation and coverage

`npm run test:browser` runs two Chromium projects. Neither accepts a configurable
live application URL, reuses port 8765, or uses existing host configuration or
credentials. Browser traffic to external destinations is blocked, as are service
workers.

The `fixture-chromium` project starts `tests/browser/fixture_server.mjs` on
**127.0.0.1:18765**. This static-file server imports no controller and rejects
every unmocked API request. These focused UI scenarios intercept API responses;
external font requests receive empty local responses. They cover:

- Target and patch selection, README binding, Advanced settings and maintenance
  window rejection before a plan is submitted.
- Missing/stale/unknown fleet evidence and actionable backup links.
- Backup analysis and independent approval gates, validated backup selection and
  explicit transfer of its policy into readiness.
- Disconnect/reload recovery using the same run identity, with duplicate
  execution controls suppressed.
- Approval inbox review and independent-approver messaging.
- Persistent execution timeline, distinct controller/native heartbeat evidence,
  bounded native logs, unknown outcomes and reports that hide unverified facts.

The `backend-chromium` project starts `tests/browser/integration_server.py` on
**127.0.0.1:18766**. The harness copies source into a temporary directory, excluding
saved state and deployment configuration. It creates one explicitly simulated
managed host and separate synthetic requester, approver and operator credentials. Browser API
responses are **not mocked**: the harness invokes `server.main()` and requests
use Uvicorn, FastAPI's ASGI transport, existing controller authentication,
the asynchronous run controller, native recovery/patch shell tools and evidence
verification. Oracle binaries are TEST_MODE shims. `connected_fixture.py` permits
only the single fixture alias, native executable allowlist and paths inside the
disposable tree at the transport boundary. It executes the controller's actual
detached launch scripts locally, preserving PID/exit-status checks and native
sealed evidence import. A deterministic model protocol simulator listens at
**127.0.0.1:18767** for the chat acceptance test; only that exact loopback model
destination is permitted for outbound DNS/connections. Other remote targets and
real SSH/network commands are blocked. All generated state and backups remain
in the disposable tree.

The following workflows run through visible application controls:

- Recovery fixture creation, analysis, self-approval rejection, approval by a
  separately authenticated approver, operator authorization and execution; completed
  backup evidence and status remain visible after reload.
- Standalone patch fixture creation, independent approval/authorization, dispatch,
  all five executor stages, status after reload, and an exported report whose
  verified facts show SQL `APPLY/SUCCESS`, listener ready and zero invalid objects.
  Report completion is verified, while rollback approval remains false.
- Connected controller acceptance with simulated host transport: create a managed
  recovery request, analyze, independently approve/authorize and execute it;
  select the completed backup through the native evidence collector, explicitly
  load its policy, evaluate readiness, create an ordinary host patch plan, execute
  all five stages and export its verified report. Recovery and patching share
  one fixture database, listener state and Oracle home. No completed recovery or
  ready-for-approval document is seeded at this seam. The test checks that the
  plan seals the same recovery digest that readiness evaluated, backup remains
  required, status survives reload and report completion is verified. Initial
  topology observations are simulated; native snapshot reconciliation, artifact
  inspection, procedure validation and OPatch compatibility checks derive the
  prerequisite documents.
- Chat inspection and proposal using the real local-model HTTP client and a
  deterministic model simulator. A proposal launches no native work. Confirming
  an unapproved plan leaves it awaiting approval; independent approval,
  authorization and dispatch then permit an explicitly confirmed proposal to
  run all five fixture stages. Saved results survive reload. Separate UI cases
  cover expired/unknown proposals, lost responses, typed confirmation and layout.

A separate integration scenario checks that anonymous and invalid credentials
cannot load the workflow, and that a real HTTP request from an operator principal
is denied when it attempts requester-only creation.

Run just these integration scenarios with `npm run test:browser:integration`.
They establish actual browser-to-controller integration, including the connected
backup selection/readiness/patch seam with a **simulated managed-host transport**.
The separate demo routes still use independent fixtures. The connected test uses
the actual managed-host branch (whose API mode is `live`), but this is a test of
controller admission and evidence custody, **not live-lab proof**. It does not
prove real SSH transport, real Oracle/RMAN behavior, an actual restore to another
database, actual model reasoning quality, a company identity provider, or
production certification.

Successful scenarios save screenshots, and exports are retained under
`test-results/`. Failures save screenshots and Playwright traces. The HTML report
is in `playwright-report/`; CI uploads these together with the command receipts.

The existing `make check` gate includes failure drills in
`tests/execution_lifecycle.sh`, `tests/execution_lock_files.sh`,
`tests/recovery_prepare.sh`, `tests/lock_recovery.py`, `tests/lockctl.py`,
`tests/shell_fail_open_regressions.sh` and the standalone/RAC/Grid executor suites.
These test interruption, duplicate dispatch, lock ownership, incomplete evidence,
service restoration and refusal to report false success. They remain fixture
checks, including files whose historical names contain `live` or `production`.

The configuration follows the primary Playwright guidance for
[CI](https://playwright.dev/docs/ci),
[a managed test web server](https://playwright.dev/docs/test-webserver) and
[API mocks](https://playwright.dev/docs/mock). Playwright is pinned in
`package-lock.json`; dependency updates should arrive through reviewed pull
requests and rerun the same validation workflow.
