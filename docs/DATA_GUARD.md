# Data Guard (S11) — observe, evaluate, standby-first order

This repository now includes a **lab-capable Data Guard control slice**:

| Tool | Role |
| --- | --- |
| `bin/opu-dataguard-observe` | Read-only broker/member/lag observation (`TEST_MODE` fixture or live SQL) |
| `bin/opu-dataguard-evaluate` | Fail-closed lag/broker gates → `ready_for_standby_first` or `blocked` |
| `bin/opu-dataguard-plan-order` | Emits standby-first patching order from sealed evaluate+observe digests |

## Readiness integration

`opu-readiness-evaluate` accepts optional `--dataguard EVALUATION_JSON`:

- Standby roles without a ready evaluation remain **blocked** (`dataguard_unsupported`).
- Standby or primary with `status=ready_for_standby_first` receives a
  `dataguard_standby_first` pass and may proceed to approval planning.

## Not yet in this slice

- Automatic switchover / reinstate execution
- A dedicated DG patch procedure adapter that replaces Database/Grid adapters
- Broker enable/disable mutation

Those remain follow-on EXE-12 work after standby-first ordering is proven in lab.
