.PHONY: test lint check

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
	./tests/readiness.sh

lint:
	./scripts/lint.sh

check: lint test
