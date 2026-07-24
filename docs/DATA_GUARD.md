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
