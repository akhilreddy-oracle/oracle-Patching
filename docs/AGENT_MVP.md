# Oracle Linux Bash Agent MVP

## Implemented boundary

The current agent is a local, read-only execution kernel. It proves the
operation registry, evidence envelope, durable completion, idempotency, and
discovery contract before network leasing or patch execution is introduced.

Registered operations:

| Operation | Version | Mutability | Current sources |
| --- | --- | --- | --- |
| `host.discover` | 1 | read-only | `/etc/os-release`, hostname, machine ID, kernel |
| `oracle_homes.discover` | 1 | read-only | `/etc/oratab`, `/etc/oraInst.loc`, central inventory XML |

The central-inventory parser intentionally reports `partial` coverage. Its
current conservative parser records same-line `HOME` entries without claiming
full XML correctness. An XML-aware, fixture-certified collector must replace
that limitation before the discovery accuracy gate closes.

## State layout

The development and production default is `/var/lib/oracle-patching-agent`:

```text
locks/<idempotency-key>.lock
runs/<task-id>/events.jsonl
runs/<task-id>/stdout.log
runs/<task-id>/stderr.log
runs/<task-id>/payload.json
runs/<task-id>/request.sha256
runs/<task-id>/result.json
task-bindings/<task-id>
results/<idempotency-key>/complete
results/<idempotency-key>/completion.sha256
results/<idempotency-key>/exit_code
results/<idempotency-key>/request.sha256
results/<idempotency-key>/result.json
results/<idempotency-key>/result.sha256
```

A completion is published as a same-filesystem directory rename. A repeated
delivery returns the original result. An existing run without a completion is
classified as an unknown outcome and is not silently rerun.

## Local verification

```bash
make check
```

The tests use filesystem fixtures and never require or invoke Oracle software.
The next verification layer will run the same suite inside Oracle Linux 8 and 9
containers with ShellCheck installed.

## Explicitly not implemented

- enrollment, mTLS, heartbeat, or remote task leasing
- signed JSON task envelopes and parameter schemas
- runtime-process, OPatch, SQL, listener, or service discovery
- timeouts, output-size enforcement, redaction rules, or privilege brokerage
- patch staging, stop/start, OPatch apply, datapatch, rollback, or recovery

These omissions are gates, not implicit behavior. No unregistered action can be
requested through the current CLI.

## Next slice

1. Add a signed/fixture task-envelope contract with exact-field validation.
2. Replace central-inventory parsing with an XML-aware collector.
3. Add runtime process discovery and source reconciliation.
4. Add read-only OPatch version and `lsinventory` fixture adapters.
5. Add forced-interruption, restart-reconciliation, and stale-lock journal tests.
