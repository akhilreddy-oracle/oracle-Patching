# Read-only extjob inspection

After a standalone binary apply and datapatch succeed, the application can
inspect the final-validation prerequisite without executing another patch task.
Exactly two states are admitted: a running plan whose final validation is
pending, or a paused plan whose final validation has a verified failed result.
All four preceding standalone tasks must have succeeded with verified custody;
unknown, running, missing or extra tasks block inspection. This operation never
installs an executable or changes permissions.

A controller administrator registers a comparison reference in
`webapp/var/extjob-references/<plan_id>.json`. The directory and regular,
single-link file must be owned by the controller or root and must not permit
group or world writes. Use mode `0700` for the directory and `0600` for the file.
The JSON has exactly four fields:

```json
{
  "plan_sha256": "<sealed plan SHA-256>",
  "database": "<sealed database unique name>",
  "archive_path": "/u02/opu-backup/<database>/<backup>/oracle-home/<home>.tar.gz",
  "archive_sha256": "<independently retained archive SHA-256>"
}
```

An authenticated operator calls `POST /api/plans/<plan_id>/extjob-inspect`
with only `{"actor":"<sealed execution authorizer>"}`. The response supplies
a run ID for `GET /api/runs/<run_id>`. The inspection reserves the plan's
execution slot, so it cannot overlap another application execution for that
plan. The API does not accept archive paths, digests, commands, or repairs.
An expired maintenance window does not prevent this read-only inspection.
It still prevents execution/retry, and inspection cannot change the window or
mark a failed task successful. Any unresolved application execution must be
reconciled before inspection can reserve the plan's execution slot.

The plan detail and host Execute stage expose **Inspect extjob (read-only)**
when this exact task chain is eligible. Use the sealed execution authorizer as
the actor. **Load saved inspection** reads the retained result without starting
work. Results whose authority no longer matches current plan/task evidence are
labelled historical; the comparison is never presented as approval for repair.

The native collector binds its report to the plan, authorizer, root-protected
apply evidence, verified datapatch evidence, target, and registered archive
reference. Its authority records the plan state, final task ID/status and failed
result digest when applicable. Both the controller and native collector recheck
this context after collection, rejecting task or evidence changes. It hashes the current
regular, single-link `bin/extjob` through a checked file descriptor. It checks
the archive's root protection and whole-file checksum before streaming the
unique regular `<home>/bin/extjob` member; it never extracts archive contents.

`inspected` means the read-only comparison completed. `blocked` retains the
reason and, when available, the current executable's digest and metadata even
if archive custody could not be verified. These are inspection outcomes;
neither marks the final patch task successful. Both reports explicitly set
`mutation_authorized` to `false`.

A matching historical checksum or a tar member labelled root-owned does not
establish publisher authenticity or authorize root execution. A permission
repair requires separately verified content provenance and appropriate
authorization. The final validator independently checks the sealed README's
required extjob owner and mode and fails if that evidence is missing or changed.

## Expired-plan continuation

No validation-continuation operation is provided by this inspection feature.
A future continuation needs a separate request and approval bound to the
original plan seal, successful apply/datapatch custody, failed final-task result,
verified correction evidence, current target and a new maintenance window.
It must authorize only remaining validation, preserve the original immutable
window and task history, reject unresolved executions, and never repeat binary
apply or datapatch implicitly. Passing such a continuation must produce linked
new evidence rather than rewrite the original failed attempt.
