# Native implementation review: second pass

Date: 2026-09-17. This is a code-first follow-up to
[ROBUSTNESS_REVIEW.md](ROBUSTNESS_REVIEW.md). Passing fixtures were not treated
as proof of correct Oracle behavior. No SSH connection, Oracle command, live
backup, database shutdown, patch application, or rollback was performed.

## Confirmed defects and changes

| Defect found by tracing production code | Correction and reproduction |
| --- | --- |
| RAC rollback required `.patch_storage/PATCH_ID`, but OPatch uses a patch identifier plus timestamp. TEST_MODE bypassed the check and hid this live-path rejection. | The actual rollback precheck now finds a regular timestamped directory matching that patch identifier. A `TEST_MODE=0` isolated stage reproduction rejected valid metadata before the change; it now accepts it while rejecting absent, wrong-patch and symlink metadata. |
| Grid stages collected current patch levels in a child process, then the supervisor emitted the old precheck binding as the current cluster state. | Both manual Grid and OPatchAuto persist the current task's complete runtime observation and load that observation for evidence. Missing observations remain `unknown`. The actual supervisors previously emitted baseline `111` after a child observed `222/333`; the regression now gets the latter. A retry uses the real attempt-path helper and cannot reuse a prior attempt's planted `999` observation, including when it fails before collection. |
| Compatibility logs used only the Oracle home's basename. Two distinct homes ending in `dbhome` overwrote one another's logs. | Log names include the full normalized home-path digest. The regression compares each emitted log digest to its preserved bytes for distinct homes with the same basename. |
| An unavailable OPatch version was passed through `jq select`, which eliminated the entire home check and could leave malformed multi-home JSON. A malformed version with a numeric prefix could also pass numeric comparison. | Unknown versions omit the optional `actual_version` field while retaining a blocked check and actionable findings. Version comparison requires the expected numeric dotted form first. The before-change reproduction failed JSON parsing; all check records are retained now. |
| The readiness producer emitted out-of-place `switch_home`, but its result schema rejected that method. | The schema includes the method. Runtime contract validation now invokes the actual evaluator with the out-of-place procedure and validates its resulting ready record. |
| Database final SQL checks inspect the current database/root registry. They do not prove that all PDBs and the seed received the SQL patch; a restart may leave PDBs closed and datapatch skips closed PDBs. Recovery preparation likewise did not capture or restore every PDB's open state. | Current database patch, rollback, OJVM, home-switch and recovery-preparation adapters explicitly admit **non-CDB targets only**. Database readiness requires `runtime.cdb == "NO"`; live health/upgrade probes require exactly one `CDB=NO` record. Recovery analysis and execution independently require live `CDB=NO` before downtime. `YES`, missing, null, conflicting or duplicate scope evidence blocks. No PDB is automatically opened. |
| OJVM and out-of-place datapatch wrappers forced a patch list and used `-noqi`, although their selected procedures specify `datapatch -verbose`. | Both now use ordinary `datapatch -verbose`, allowing Oracle to select SQL actions from current binary inventory. Existing binary-state and latest-action checks still require the intended APPLY/ROLLBACK result. Isolated command tests and executor fixtures reject additional bypass/forced-selection arguments. |

The controller's compatibility-node fallback was also traced: the native
collector labels results using the supplied snapshot's host identity. Sending
one primary snapshot to another node can mislabel results. The controller
review separately removes that fallback and requires per-node evidence.

## Oracle semantics used in this review

- Oracle documents `.patch_storage` rollback directories as a patch identifier
  with creation timestamp in its [OPatch guide](https://docs.oracle.com/cd/E16291_01/doc/em.112/e12255/oui7_opatch.htm).
- [Oracle's datapatch instructions](https://docs.oracle.com/en/database/oracle/zero-downtime-migration/19.2/zdmre/migrate-database1.html)
  explain that it processes the CDB and open PDBs; skipped PDBs require a later
  run. They describe ordinary `datapatch -verbose` invocation.
- The [19c multitenant monitoring guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/multi/viewing-information-about-cdbs-and-pdbs-with-sql-plus.html)
  distinguishes current-container DBA views, CDB container data, and
  `V$DATABASE.CDB` (`YES`/`NO`).
- [DBA_REGISTRY_SQLPATCH](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/DBA_REGISTRY_SQLPATCH.html)
  records individual apply/rollback attempts. An older successful row is not
  evidence that the most recent action succeeded.

The non-CDB restriction is a supported-scope decision, not an implementation
of multitenant patching. A future CDB adapter needs a sealed container manifest,
explicit PDB/seed handling, authorized open-state restoration, and successful
latest SQL patch evidence for every required container. These semantics need
Oracle lab verification before admitting CDBs.

## Coverage and limitations

The second pass inventoried all `bin/opu-*`, `lib/opu/*`, and the discovery
operations, approximately 17,500 source lines. It **did not freshly re-read
every line of that inventory**. The earlier review's coverage is recorded
separately; it is not relabeled as a second independent complete review.

Fresh complete reads covered `opu-opatch-compatibility-collect`,
`opu-readiness-evaluate`, `lib/opu/execution.sh`, `lib/opu/oracle_inventory.sh`,
`lib/opu/dataguard.sh`, and `lib/opu/recovery_evidence.sh`.

Fresh targeted control-flow reads covered:

- All five database implementations: standalone, OJVM, out-of-place, RAC apply
  and RAC rollback. Traced admission/health probes, mutation-stage callers,
  datapatch arguments, latest SQL action queries, and affected rollback paths.
- Both Grid implementations: runtime collection, precheck binding, child
  execution, attempt isolation, evidence emission, and the stages that collect
  new runtime values. The small rollback wrappers share these implementations.
- Recovery preparation's request binding, analysis, live identity probe and
  the pre-downtime execution checks; topology discovery's CDB field emission;
  the shared non-CDB admission primitive; associated producer/consumer schemas.

Artifact staging/inspection, complete plan construction, the remaining
collectors and legacy commands, agent transport, discovery operations and the
larger Python native helpers were not each fully re-read in this second pass.
That remains a review coverage limit, even when regression suites pass.

## Validation evidence at source freeze

Completed focused checks:

- `tests/native_second_review.py`: five tests, with subcases for every affected
  database adapter, both Grid supervisors, failed/no-observation retries,
  timestamped metadata and compatibility evidence preservation.
- `tests/runtime_contracts.py`: actual generated discovery/artifact/readiness
  (including out-of-place)/execution/custody records validate.
- Native readiness, compatibility, recovery preparation, OJVM apply/rollback,
  and out-of-place switch/switchback fixture suites passed.
- Changed shell sources and fixture scripts passed ShellCheck; whitespace
  validation passed.

Standalone, RAC and Grid end-to-end fixtures and controller TEST_MODE tests
were still running when this document was frozen. The coordinated final
release-validation bundle is the authoritative result for the final source;
this document does not assert their completion ahead of that evidence.

All tests use temporary fixtures or local command doubles. They establish
specific fail-closed behavior and catch the defects above; they do not prove
real Oracle SQL syntax execution, Linux service restoration, patch README
applicability, real CRS/OPatch output on every release, successful live
rollback, or production approval.
