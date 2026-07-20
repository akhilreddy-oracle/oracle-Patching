#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-grid-recovery.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

grid_root="$TMP/grid"
inventory_root="$TMP/oraInventory"
backup_root="$TMP/backup/grid/run-001"
ora_inst="$TMP/oraInst.loc"
mkdir -p "$grid_root/bin" "$inventory_root/ContentsXML" "$backup_root"

cat >"$grid_root/bin/crsctl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  'check crs')
    printf '%s\n' 'CRS-4638: Oracle High Availability Services is online'
    ;;
  'query crs activeversion -f')
    printf '%s\n' \
      'Oracle Clusterware active version on the cluster is [19.0.0.0.0].' \
      'The cluster upgrade state is [NORMAL].' \
      'The cluster active patch level is [2715623437].'
    ;;
  'query css votedisk')
    printf '%s\n' 'Located 1 voting disk(s).'
    ;;
  *)
    exit 64
    ;;
esac
EOF
cat >"$grid_root/bin/ocrcheck" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' 'Status of Oracle Cluster Registry is OK'
EOF
cat >"$grid_root/bin/ocrdump" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  -stdout) [ "$2" = -backupfile ] && [ -f "$3" ] ;;
  -local) [ "$2" = -stdout ] && [ "$3" = -backupfile ] && [ -f "$4" ] ;;
  *) exit 64 ;;
esac
printf '%s\n' 'SYSTEM.version: 19'
EOF
chmod 750 "$grid_root/bin/crsctl" "$grid_root/bin/ocrcheck" "$grid_root/bin/ocrdump"

printf 'inventory\n' >"$inventory_root/ContentsXML/inventory.xml"
printf 'inventory_loc=%s\ninst_group=%s\n' "$inventory_root" "$(id -gn)" >"$ora_inst"
printf 'ocr-backup\n' >"$backup_root/ocr.backup"
printf 'olr-node1\n' >"$backup_root/node1.olr"
printf 'olr-node2\n' >"$backup_root/node2.olr"
cp "$ora_inst" "$backup_root/oraInst.loc"
tar -czf "$backup_root/grid-home.tar.gz" -C "$TMP" grid
tar -czf "$backup_root/oraInventory.tar.gz" -C "$TMP" oraInventory
sha256sum \
  "$backup_root/grid-home.tar.gz" \
  "$backup_root/oraInventory.tar.gz" \
  "$backup_root/oraInst.loc" \
  "$backup_root/ocr.backup" \
  "$backup_root/node1.olr" \
  "$backup_root/node2.olr" >"$backup_root/SHA256SUMS"

owner=$(id -un)
jq -n --arg grid_root "$grid_root" --arg owner "$owner" '
  {
    schema_version:"1.0",
    collector:{name:"oracle.topology.discover",version:"1"},
    cluster:{
      status:"detected",
      grid_home:$grid_root,
      runtime:{status:"healthy",upgrade_state:"NORMAL"},
      nodes:[{name:"node1",status:"Active"},{name:"node2",status:"Active"}]
    },
    oracle_homes:[{path:$grid_root,owner:$owner}]
  }
' >"$TMP/snapshot.json"

checksum_sha=$(sha256sum "$backup_root/SHA256SUMS" | awk '{print $1}')
grid_sha=$(sha256sum "$backup_root/grid-home.tar.gz" | awk '{print $1}')
inventory_sha=$(sha256sum "$backup_root/oraInventory.tar.gz" | awk '{print $1}')
ora_inst_sha=$(sha256sum "$backup_root/oraInst.loc" | awk '{print $1}')
ocr_sha=$(sha256sum "$backup_root/ocr.backup" | awk '{print $1}')
olr1_sha=$(sha256sum "$backup_root/node1.olr" | awk '{print $1}')
olr2_sha=$(sha256sum "$backup_root/node2.olr" | awk '{print $1}')
jq -cn \
  --arg grid_root "$grid_root" --arg owner "$owner" \
  --arg inventory_root "$inventory_root" --arg ora_inst "$ora_inst" \
  --arg backup_root "$backup_root" --arg checksum_sha "$checksum_sha" \
  --arg grid_sha "$grid_sha" --arg inventory_sha "$inventory_sha" \
  --arg ora_inst_sha "$ora_inst_sha" --arg ocr_sha "$ocr_sha" \
  --arg olr1_sha "$olr1_sha" --arg olr2_sha "$olr2_sha" '
  {
    schema_version:"1.0",
    collector:{name:"oracle.grid.recovery.bundle",version:"1"},
    status:"prepared",
    target:{grid_home:$grid_root,owner:$owner,central_inventory:$inventory_root,oraInst_loc:$ora_inst},
    backup:{
      root:$backup_root,
      checksum_manifest:{path:($backup_root+"/SHA256SUMS"),sha256:$checksum_sha},
      grid_home_archive:{path:($backup_root+"/grid-home.tar.gz"),sha256:$grid_sha,source_path:$grid_root},
      central_inventory_archive:{path:($backup_root+"/oraInventory.tar.gz"),sha256:$inventory_sha,source_path:$inventory_root},
      oraInst_loc:{path:($backup_root+"/oraInst.loc"),sha256:$ora_inst_sha,source_path:$ora_inst},
      ocr_backup:{path:($backup_root+"/ocr.backup"),sha256:$ocr_sha},
      olr_backups:[
        {node:"node1",path:($backup_root+"/node1.olr"),sha256:$olr1_sha,source_path:"/var/backups/node1.olr"},
        {node:"node2",path:($backup_root+"/node2.olr"),sha256:$olr2_sha,source_path:"/var/backups/node2.olr"}
      ]
    }
  }
' >"$TMP/bundle.tmp"
bundle_record=$(jq -cS . "$TMP/bundle.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$bundle_record" '.record_sha256=$record' "$TMP/bundle.tmp" >"$TMP/bundle.json"

tool() {
  OPU_GRID_RECOVERY_TEST_ALLOW_NONROOT=1 \
    OPU_GRID_RECOVERY_ORAINST_LOC="$ora_inst" \
    "$ROOT/bin/opu-grid-recovery-evidence-collect" "$@"
}

tool \
  --snapshot "$TMP/snapshot.json" \
  --bundle "$TMP/bundle.json" \
  --output "$TMP/evidence.json" >"$TMP/result.json"

jq -e --arg grid_root "$grid_root" --arg inventory_root "$inventory_root" '
  .status == "passed" and
  .collector.name == "oracle.grid.recovery.evidence" and
  .target.grid_home == $grid_root and
  .target.nodes == ["node1","node2"] and
  .target.central_inventory == $inventory_root and
  (.backup | keys | sort) == (["central_inventory_archive","checksum_manifest","grid_home_archive","ocr_backup","olr_backups","oraInst_loc"] | sort) and
  (.backup.olr_backups | length) == 2 and
  (.verification.olr_backup_logs | length) == 2 and
  (.verification | length) == 9 and
  (.record_sha256 | test("^[a-f0-9]{64}$"))
' "$TMP/result.json" >/dev/null
expected=$(jq -r '.record_sha256' "$TMP/result.json")
actual=$(jq -cS 'del(.record_sha256)' "$TMP/result.json" | tr -d '\n' | sha256sum | awk '{print $1}')
[ "$expected" = "$actual" ]

jq '.cluster.nodes[1].status="Inactive"' "$TMP/snapshot.json" >"$TMP/inactive-snapshot.json"
if tool \
  --snapshot "$TMP/inactive-snapshot.json" \
  --bundle "$TMP/bundle.json" \
  --output "$TMP/inactive-evidence.json" >/dev/null 2>&1; then
  echo 'Grid recovery evidence accepted an inactive cluster node' >&2
  exit 1
fi

cp "$backup_root/SHA256SUMS" "$TMP/SHA256SUMS.original"
printf 'unrelated\n' >"$backup_root/unrelated-file"
sha256sum "$backup_root/unrelated-file" >>"$backup_root/SHA256SUMS"
extra_manifest_sha=$(sha256sum "$backup_root/SHA256SUMS" | awk '{print $1}')
jq --arg digest "$extra_manifest_sha" \
  '.backup.checksum_manifest.sha256=$digest | del(.record_sha256)' \
  "$TMP/bundle.json" >"$TMP/extra-bundle.tmp"
extra_record=$(jq -cS . "$TMP/extra-bundle.tmp" | tr -d '\n' | sha256sum | awk '{print $1}')
jq --arg record "$extra_record" '.record_sha256=$record' \
  "$TMP/extra-bundle.tmp" >"$TMP/extra-bundle.json"
if tool \
  --snapshot "$TMP/snapshot.json" \
  --bundle "$TMP/extra-bundle.json" \
  --output "$TMP/extra-evidence.json" >/dev/null 2>&1; then
  echo 'Grid recovery evidence accepted an unsealed extra checksum entry' >&2
  exit 1
fi
mv "$TMP/SHA256SUMS.original" "$backup_root/SHA256SUMS"

printf 'tampered\n' >>"$backup_root/ocr.backup"
if tool \
  --snapshot "$TMP/snapshot.json" \
  --bundle "$TMP/bundle.json" \
  --output "$TMP/tampered-evidence.json" >/dev/null 2>&1; then
  echo 'Grid recovery evidence accepted a tampered OCR backup' >&2
  exit 1
fi

printf '%s\n' 'Grid recovery evidence test passed'
