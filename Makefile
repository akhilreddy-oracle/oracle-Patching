.PHONY: test lint contracts check

test:
	./tests/run.sh
	./tests/jobs.sh
	./tests/topology_snapshot.sh
	./tests/opatch_platform_discovery.sh
	./tests/reconcile.sh
	./tests/artifact_inspect.sh
	./tests/procedure_validate.sh
	./tests/opatch_compatibility.sh
	./tests/compatibility_reconcile.sh
	./tests/opatch_upgrade.sh
	./tests/recovery_prepare.sh
	./tests/recovery_evidence.sh
	./tests/grid_recovery_evidence.sh
	./tests/patch_plan.sh
	./tests/single_instance_patch.sh
	./tests/rac_database_patch.sh
	./tests/grid_node_patch.sh
	./tests/grid_node_rollback.sh
	./tests/grid_opatchauto_patch.sh
	./tests/ojvm_patch.sh
	./tests/out_of_place_patch.sh
	./tests/readiness.sh
	./tests/dataguard.sh
	./tests/dataguard_switchover.sh
	./tests/patch_plan_dataguard.sh
	./tests/webapp_testmode_rac_grid.sh
	./tests/live_multinode_preflight.sh
	./tests/release_signing.sh
	./tests/agent_queue_rbac.sh
	./tests/agent_work_run.sh
	./tests/production_cert.sh
	./tests/database_rolling_patch.sh
	./tests/grid_rolling_patch.sh
	./tests/integrations.sh

lint:
	./scripts/lint.sh

contracts:
	./scripts/check_contracts.sh

check: lint contracts test
