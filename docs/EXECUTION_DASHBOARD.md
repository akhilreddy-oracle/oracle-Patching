# Execution dashboard

Every plan detail and execution stage includes a saved task timeline, controller
events, elapsed time, and a bounded native-log view. Opening the view reads local
plan and run records. It does not refresh discovery or sync deployment files.

Run events and the last observation persist in `webapp/var/runs/<run>/run.json`.
After a controller restart, existing ownership rules still mark interrupted work
as **unknown**. Viewing or refreshing diagnostics never releases that ownership
or changes a task result. Use the existing **Inspect and reconcile** action to
verify an interrupted execution; the log view cannot declare it completed.

Two separate indicators explain freshness:

- **Controller contact** records when the controller most recently polled SSH.
  It reports connectivity, not Oracle progress.
- **Native worker heartbeat** reports the task lease renewal observed on the
  managed host, accounting for the host/controller clock offset. It is an
  observation of a lease, not verified terminal evidence or a percent complete.

**Refresh native logs** performs a typed read-only inspection of an existing
persisted launch. **Follow native logs while running** is optional and off by
default; when enabled it requests observations at most every 15 seconds while
the controller run is queued/running. Saved state refreshes locally every five
seconds during execution and every 15 seconds otherwise. A failed native read
turns following off. Navigating away stops browser polling; execution continues
independently on the managed host.

The observer validates the plan/task, host identity, launch path and attempt
generation. It can read only fixed wrapper files and allowlisted standalone
worker logs. Native logs require an exact task definition and retry generation
persisted before launch. Historical launches without that binding expose only
their own wrapper output and an unknown worker heartbeat. Observations of an
older attempt cannot attach logs from a later retry.

Each remote log tail is limited to 64 KiB and the displayed/persisted text is
limited to 16,000 characters after credential redaction. A partial first line
is dropped when tailing, preserving credential-key boundaries. Symbolic links,
nonregular files and hard links are rejected. The reader accepts no arbitrary
command, file path or SQL. Wrapper PID existence is explicitly unverified and
does not prove the worker is running.

Routes:

- `GET /api/plans/<id>/execution`: local saved snapshot.
- `POST /api/plans/<id>/execution-observe` with `{ "run_id": "..." }`:
  operator-authenticated managed read-only operation under `plan:<id>:observe`.
- `pipeline_runner.list_runs(key_prefix=None)`: newest-first persisted and
  in-memory records, including unknown outcomes after ownership loss.

Fixture validation: `python3 -B tests/execution_console.py`. The fixtures run
the fixed reader against temporary local files and mock SSH, sync and launch
boundaries. They never contact an Oracle host.
