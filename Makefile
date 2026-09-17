.PHONY: test lint contracts check

# Every suite owns a disposable fixture/state directory, so make -j4 check
# can run independent suites concurrently without changing the test scope.
SHELL_TESTS := python_runtime execution_lock_files execution_lifecycle run jobs topology_snapshot opatch_platform_discovery \
	reconcile artifact_inspect procedure_validate opatch_compatibility compatibility_reconcile \
	artifact_stage webapp_stage_artifact opatch_upgrade recovery_prepare recovery_evidence \
	grid_recovery_evidence patch_plan single_instance_patch rac_database_patch grid_node_patch \
	grid_node_rollback grid_opatchauto_patch ojvm_patch out_of_place_patch readiness \
	dataguard dataguard_live dataguard_switchover patch_plan_dataguard discovery_phases \
	webapp_testmode_rac_grid live_multinode_preflight release_signing agent_queue_rbac agent_work_run \
	production_cert shell_fail_open_regressions database_rolling_patch grid_rolling_patch integrations
PYTHON_TESTS := runtime_package runtime_paths remote_deployment server_preflight local_llm model_acceptance assistant_workflow assistant_inventory assistant_api agent_queue_hardening artifact_safety webapp_control procedure_hints recoveryctl_live recovery_capability recovery_demo_fixture recovery_pipeline recovery_api lock_recovery lockctl extjob_provenance extjobctl company_auth company_auth_api company_auth_http fleet fleet_metadata fleet_api execution_console evidence_reports testmode_standalone
TEST_TARGETS := $(addprefix test-shell-,$(SHELL_TESTS)) $(addprefix test-python-,$(PYTHON_TESTS)) test-frontend test-release-validation
.PHONY: $(TEST_TARGETS)

test: $(TEST_TARGETS)

$(addprefix test-shell-,$(SHELL_TESTS)): test-shell-%:
	bash ./tests/$*.sh

$(addprefix test-python-,$(PYTHON_TESTS)): test-python-%:
	python3 -B tests/$*.py

test-frontend:
	node --test tests/frontend.mjs tests/release_ux.mjs tests/fleet_metadata.mjs tests/chat_frontend.mjs

test-release-validation:
	python3 -B tests/browser/release_validation_test.py

lint:
	./scripts/lint.sh

contracts:
	./scripts/check_contracts.sh

check: lint contracts test
