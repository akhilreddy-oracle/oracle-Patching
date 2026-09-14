# Data Guard (S11) — observe, evaluate, gates, orchestrate

Lab-capable Data Guard control slice:

| Tool | Role |
| --- | --- |
| `bin/opu-dataguard-observe` | Read-only broker/member/lag observation (`TEST_MODE` fixture or live SQL) |
| `bin/opu-dataguard-evaluate` | Fail-closed lag/broker gates → `ready_for_standby_first` or `blocked` |
| `bin/opu-dataguard-plan-order` | Emits standby-first patching order from sealed evaluate+observe digests |
| `bin/opu-dataguard-switchover-gate` | Fail-closed switchover readiness (`ready_for_switchover`) — does not switch over |
| `bin/opu-dataguard-reinstate-gate` | Fail-closed reinstate readiness (`ready_for_reinstate`) — does not reinstate |
| `bin/opu-dataguard-switchover` | Switchover executor bound to a sealed `ready_for_switchover` gate (`TEST_MODE` simulated; live via `dgmgrl`, fail-closed) |
| `bin/opu-dataguard-reinstate` | Reinstate executor bound to a sealed `ready_for_reinstate` gate (`TEST_MODE` simulated; live via `dgmgrl`, fail-closed) |
| `bin/opu-dataguard-orchestrate` | Seals an ordered orchestration step list from the above evidence |

## Readiness integration

`opu-readiness-evaluate` accepts optional `--dataguard EVALUATION_JSON`:

- Standby roles without a ready evaluation remain **blocked** (`dataguard_unsupported`).
- Standby or primary with `status=ready_for_standby_first` receives a
  `dataguard_standby_first` pass and may proceed to approval planning.

## Orchestration automation

`opu-dataguard-orchestrate` produces a sealed step plan: patch standbys →
optional switchover gate → switchover → patch primary → optional reinstate.
When a sealed switchover or reinstate gate is supplied, the corresponding
step is delegated to `opu-dataguard-switchover` / `opu-dataguard-reinstate`
(`operator_executed:false`, `mode:test_mode_or_live`); without gate evidence
the step stays `operator_executed:true`. Existing Database/Grid adapters
remain the patch mutation executors; this tool does not replace them.

## Switchover / reinstate executors

`opu-dataguard-switchover execute` and `opu-dataguard-reinstate execute` are
fail-closed mutation authorities. Both verify the supplied gate's
`record_sha256` against its canonical content, require the gate status
(`ready_for_switchover` / `ready_for_reinstate`), require the gate's
`evidence.observe_sha256` to match the supplied observe file, require the
target `db_unique_name` to appear in the observe members, and call the
production certification gate (`OPU_PRODUCTION_MODE=1` without a valid
certification marker refuses with exit 77). In
`OPU_DATAGUARD_TEST_MODE=1` the broker command is simulated and the sealed
evidence carries `simulated:true`. In live mode `dgmgrl` must exist on
`PATH` or at `$ORACLE_HOME/bin/dgmgrl` (missing → exit 69); the broker
command output is captured to a log whose path and digest are sealed into
the evidence, and a nonzero exit or `ORA-`/`error` output yields
`status:failed` (exit 2).

A sealed `opu-dataguard-plan-order` result can now be bound into an immutable
apply plan: `opu-patch-plan create --dataguard-order ORDER_JSON` verifies the
order's record digest, requires `strategy=standby_first` with the primary
never listed before a standby, and requires the readiness result to carry a
passing `dataguard_standby_first` gate. The order digest and strategy are
sealed into `plan.json` (`.dataguard`), and `dispatch` re-verifies the sealed
order file fail-closed before materializing tasks. Because each plan targets
one database, this is a metadata seal plus verification — cross-database
standby-then-primary sequencing remains an operator responsibility guided by
the sealed order; broker `SWITCHOVER`/`REINSTATE` execution is available
through the gate-bound executors below (proven in `TEST_MODE`, fail-closed
live). The webapp passes a `dataguard_order` evidence document
through to `create` whenever one is cached for the host.

## Not yet live-automated

- `SWITCHOVER` / `REINSTATE` execution now exists in `TEST_MODE` via
  `opu-dataguard-switchover` and `opu-dataguard-reinstate`; the live `dgmgrl`
  path exists but requires `dgmgrl` plus production certification and remains
  unproven against a real broker configuration
- A dedicated DG patch procedure adapter that replaces Database/Grid adapters
- Broker enable/disable mutation

Real-broker validation of the live executor path and the dedicated DG adapter
remain EXE-12 follow-on work.

## Live observation and lag freshness

The live collector now queries the local database role and `DB_UNIQUE_NAME`
from `V$DATABASE`. It seals this native database identity separately from the
instance's `ORACLE_SID`; new live patch orders cannot substitute a SID for an
unknown database name. The existing `primary` object is retained for contract
compatibility and describes the locally observed database, including when its
role is standby. [Oracle 19c V$DATABASE reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/V-DATABASE.html).

On a physical standby's applying instance, the collector reads the `transport
lag` and `apply lag` metric rows from `V$DATAGUARD_STATS`. It retains `VALUE`,
`UNIT`, `TIME_COMPUTED`, `DATUM_TIME`, and the originating database identity.
Intervals are converted using days, hours, minutes and seconds; fractional
seconds round upward. Missing values stay unavailable (`-1`), and malformed
rows, duplicate metrics or unsupported formats fail collection. Both sample
clocks are compared with database-local time, avoiding an assumed controller
timezone. Supported clock formats are `MM/DD/YYYY HH24:MI:SS` and
`YYYY-MM-DD HH24:MI:SS`; unexpected formats block collection.
[Oracle 19c V$DATAGUARD_STATS reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/V-DATAGUARD_STATS.html).

Physical apply state comes from the local `MRP0` process in
`V$DATAGUARD_PROCESS`. `APPLYING_LOG` or `WAIT_FOR_LOG` can pass the process
check only alongside acceptable lag and freshness; missing apply-process
evidence blocks readiness. Logical or snapshot standby collection does not
assert physical apply readiness. [Oracle 19c V$DATAGUARD_PROCESS reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/V-DATAGUARD_PROCESS.html).

A primary scan reports standby destination health from
`V$ARCHIVE_DEST_STATUS`. That view supplies no elapsed transport/apply lag,
and `V$DATAGUARD_STATS` returns no primary rows. Consequently the primary
scan explicitly reports unknown lag and cannot pass lag-based readiness.
Passing primary-wide orchestration still requires an integration that binds
fresh standby observations or verified broker lag samples to every member;
this collector does not fabricate that evidence. [Oracle 19c V$ARCHIVE_DEST_STATUS reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/V-ARCHIVE_DEST_STATUS.html).

The evaluator verifies the observation checksum and collection age. Its
optional policy fields `dataguard.maximum_observation_age_seconds` (default
300) and `dataguard.maximum_sample_age_seconds` (default 60) must be positive
integers. For live collector version `2`, both computation and received-data
ages, plus time elapsed since collection, must remain within the sample age
limit. A stale zero-lag sample therefore blocks readiness. Legacy version `1`
fixtures remain supported with an explicit warning that native sample clocks
are unavailable. [Oracle's explanation of DATUM_TIME and lost transport](https://docs.oracle.com/en/database/oracle/oracle-database/19/haovw/redo-transport-troubleshooting-and-tuning.html).

SQL*Plus uses SQL/OS failure exit directives; tagged output parsing also
rejects diagnostics that return exit zero. Broker availability requires a
successful single-command `SHOW CONFIGURATION` result without Oracle/broker
errors, rather than merely finding a binary. [Oracle 19c broker command reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/dgbkr/oracle-data-guard-broker-commands.html).

Run `bash tests/dataguard_live.sh` for the real collector command path with
temporary fake Oracle binaries. It covers native identity, primary/standby
query separation, long/fractional lag, stale and future clocks, missing
metrics, missing apply process, SQL and broker errors, malformed output, and
checksum/replay checks. This proves local control flow and parsing; Oracle
19c physical-standby, RAC applying-instance, locale, and real-broker lab
validation remain required before production certification.
