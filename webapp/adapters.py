"""Package-owned executor registry shared by dispatch and agent runners."""
EXECUTOR_PATHS = {
    "database_single_instance_opatch": "bin/opu-database-single-instance-patch",
    "database_single_instance_opatch_rollback": "bin/opu-database-single-instance-rollback",
    "database_rolling_opatch": "bin/opu-database-rac-node-patch",
    "database_rac_opatch_rollback": "bin/opu-database-rac-node-rollback",
    "grid_rolling_opatch": "bin/opu-grid-node-patch",
    "grid_rolling_opatch_rollback": "bin/opu-grid-node-rollback",
    "grid_rolling_opatchauto": "bin/opu-grid-opatchauto-patch",
    "grid_rolling_opatchauto_rollback": "bin/opu-grid-opatchauto-rollback",
    "database_ojvm_opatch": "bin/opu-database-ojvm-patch",
    "database_ojvm_opatch_rollback": "bin/opu-database-ojvm-rollback",
    "database_out_of_place_switch": "bin/opu-database-out-of-place-patch",
    "database_out_of_place_switchback": "bin/opu-database-out-of-place-switchback",
}
