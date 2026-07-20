# ADR 0001: Oracle Linux Bash Agent

- Status: accepted
- Date: 2026-07-13

## Context

The first release needs a small managed component on Oracle Database hosts. The
current product boundary is Oracle Linux only. Enterprise Linux teams already
operate Bash extensively, and Oracle patching ultimately invokes local Oracle
and operating-system tools.

The agent is also a high-risk execution boundary. Selecting Bash must not turn
the utility into remote shell access or allow untyped commands from a control
plane, operator, or AI component.

## Decision

The managed host agent and Oracle Linux operation adapters use Bash. Production
packages require Bash 4.4 or newer. Initial certification targets Oracle Linux
8 and 9 on x86-64; discovery may observe another Oracle Linux release, but no
future mutating operation may execute there until that release is explicitly
certified by policy.

The following constraints are mandatory:

1. Operations are selected through a literal `operation@version` registry.
2. No generic command, uploaded script, `eval`, `bash -c`, or `sh -c` operation
   is permitted.
3. Parameters will be validated against an exact contract before an operation
   starts. Unknown fields fail closed.
4. Every task uses an idempotency key, a local lock, immutable run evidence,
   and an atomically published completion result.
5. Discovery preserves source provenance and reports partial coverage rather
   than guessing or treating missing evidence as deletion.
6. Mutating patch tasks remain unregistered until read-only discovery,
   prechecks, failure injection, and reconciliation gates pass.
7. The control-plane language is a separate decision. It communicates only
   through versioned task/result contracts and cannot supply shell text.

## Consequences

- The first host package has a small runtime footprint and aligns with Oracle
  Linux operating practices.
- ShellCheck, fixture tests, Oracle Linux container tests, and a disposable
  Oracle lab become release gates.
- Durable distributed orchestration, API security, RBAC, and the system of
  record remain control-plane concerns; they are not implemented as ad-hoc Bash
  background processes.
- AIX, Solaris, Windows, RAC/GI, and Data Guard are outside this decision.

