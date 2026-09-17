# Third correctness review — 18 September 2026

This pass began from `586965e`, after complete Mac and Linux fixture gates had
passed. Independent code tracing and deliberately delayed operations reproduced
three additional defects and closed the previously documented runtime-update
race. Those findings demonstrate why passing tests are not a correctness claim.

| Boundary | Reproduced behavior before the fix | Corrected behavior and regression evidence |
| --- | --- | --- |
| HTTP authority after a delayed request body | A principal could authenticate, withhold its body, then submit recovery or patch execution after its token was disabled/rotated. A company operator downgraded to viewer could also submit. | After reading the body, the handler freshly checks authentication, unchanged identity, current role and CSRF before dispatch. Regressions change those credentials during the read and assert that no native work is submitted. |
| Assistant confirmation through queued execution | Changing a node's SSH alias after confirmation redirected an accepted execute-plan action to an unreviewed target. Native transport was replaced by a recorder in the reproduction. | Confirmation binds every resolved node configuration. The handler checks that binding at admission and worker startup. The worker uses a private copy of the reviewed host map for every task. Barrier tests cover queued changes, changes between tasks, nodes outside the display host and concurrent-run isolation. |
| Database state between precheck and outage | After a valid standalone precheck, changing the database role from PRIMARY to PHYSICAL STANDBY still allowed shutdown and binary apply. The later health check detected failure only after mutation. | Standalone apply probes current identity, role and non-CDB scope immediately before shutdown. RAC apply/rollback recheck supported live health conditions before drain and again before stop. Drift cases assert no shutdown, stop or binary mutation. Later stages that deliberately operate on stopped instances retain their stage-specific behavior. |
| Code updates during native startup or collection | The prior installer could replace libraries after a worker started but before it acquired the host mutation lock. Read-only collectors did not take that lock. | Complete code generations are verified and published under `<deployment>/.opu-runtimes/<full-sha256>`. Each managed launch uses the exact returned path. A paused collector continues reading its original libraries while a new launch uses the new generation. Existing generation contents and legacy code are never replaced. |

## Runtime and state integration

Discovery, compatibility, recovery collection, artifact staging (zip, direct and
relay), plan execution, rollback-plan creation, recovery preparation, lock
recovery and provenance inspection select generation paths from typed sync
receipts. A receipt must match the configured deployment and exact SSH alias.
There is no fallback to a mutable current pointer or a guessed fingerprint.

The installer checks the complete native package contract, archive member types,
content fingerprint, executable modes and required Python imports before
publication. Existing generations are verified and retained; altered, incomplete,
symbolic or unexpected contents are rejected. Published files/directories are
read-only and the application never rewrites them. This is an application and
filesystem ownership guarantee, not protection against a privileged host
administrator deliberately replacing code.

Mutable state remains at its original paths. Native plan and launch records stay
under the stable deployment root; recovery/native execution state keeps its
existing absolute locations. Queue and enrollment defaults also use the stable
root. Native child commands use their own code generation. New detached launches
record their generation; older launch records without those additional fields
remain reconcilable through their original state paths.

Local installer tests also found and corrected two defects during implementation:
acceptance of a fingerprinted but incomplete package, and macOS directory-rename
permission requirements. Staging validates the full dependency set and moves
through a private name before sealing and atomically publishing the generation.

## Validation boundaries

The regressions use disposable files, processes and Oracle command fixtures.
Auth tests retain the actual request handler; assistant barrier tests retain
the actual worker and host resolver; native drift tests use real executors;
runtime concurrency tests run the real installer and paused shell processes.
External Oracle and SSH boundaries remain simulated. Read final exact-source
release receipts and draft PR checks for combined validation rather than using
older receipts to certify these edits.

Legacy scheduled/direct CLI commands keep their original code path until an
administrator explicitly changes them to a verified generation. Generations are
retained without automatic garbage collection so interrupted launches do not lose
their code. This review does not perform a remote deployment or upgrade an
existing host's manually scheduled tools.

CDB/PDB support, all-node RAC fleet compliance, final local-model acceptance,
tenant SSO acceptance, and live backup/restore/patch acceptance remain outstanding.
The supported non-CDB gate, native approvals and production certification controls
remain in place. This pass does not claim an exhaustive proof of correctness.
