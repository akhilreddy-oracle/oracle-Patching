# Lab Patch Artifact Onboarding

Use this checklist before a real Grid or Database patch is admitted to the
utility. This is an evidence intake step; it performs no patch operation.

1. Stage the *extracted* patch artifact at the same absolute path on every
   target node. Do not stage a mixed bundle directory.
2. Retain the patch README inside that extracted artifact.
3. Export the approved My Oracle Support procedure/reference used by the
   change and calculate its SHA-256. The utility does not accept a mutable URL
   as procedure evidence.
4. Inspect the artifact with `opu-artifact-inspect` and record its digest.
5. Start with the matching template in `contracts/procedure/examples/`; replace
   every `REPLACE_WITH_*` value using the inspected artifact and approved
   procedure evidence.
6. Validate the manifest with `opu-procedure-validate`.
7. Run compatibility collection on every active node, then reconcile it.

The `grid_rolling_opatch` template is valid only when the exact patch README
explicitly calls for the declared rolling OPatch/root script sequence. The
`database_rolling_opatch` template is valid only for RAC and only when the
README calls for rolling database-home OPatch and datapatch.
`database_single_instance_opatch` is the controlled downtime procedure for a
single-instance database: shutdown, binary OPatch apply, startup, then
datapatch. For `opatchauto`, out-of-place, FPP, OJVM, Data Guard, or any
different procedure, the utility blocks until a separate adapter has been
implemented and validated.
