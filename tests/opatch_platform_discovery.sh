#!/usr/bin/env bash
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/opu-opatch-platform.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

. "$ROOT/lib/opu/oracle_inventory.sh"

cat >"$TMP/current.xml" <<'XML'
<InventoryInstance xmlns="urn:oracle:test">
  <oracleHome><path>/u01/app/oracle/product/19c/dbhome_1</path></oracleHome>
  <osPlatform id="226">
    <UId>machine-platform</UId>
    <hostName>sourcedb</hostName>
    <version>Linux x86-64</version>
  </osPlatform>
  <patches>
    <patch>
      <osPlatforms>
        <osPlatform id="0"><version>Generic Platform 1</version></osPlatform>
      </osPlatforms>
    </patch>
  </patches>
</InventoryInstance>
XML

actual=$(opu_extract_opatch_platform "$TMP/current.xml")
[ "$actual" = $'226\tLinux x86-64' ] || {
  echo "current OPatch InventoryInstance platform was not extracted: $actual" >&2
  exit 1
}

cat >"$TMP/legacy.xml" <<'XML'
<INVENTORY>
  <ARU_PLATFORM_INFO>
    <ARU_ID>226</ARU_ID>
    <ARU_ID_DESCRIPTION>Linux x86-64</ARU_ID_DESCRIPTION>
  </ARU_PLATFORM_INFO>
</INVENTORY>
XML

actual=$(opu_extract_opatch_platform "$TMP/legacy.xml")
[ "$actual" = $'226\tLinux x86-64' ] || {
  echo "legacy ARU platform was not extracted: $actual" >&2
  exit 1
}

cat >"$TMP/property.xml" <<'XML'
<INVENTORY>
  <PROPERTY name="ARU_ID" value="226"/>
  <PROPERTY name="ARU_ID_DESCRIPTION" value="Linux x86-64"/>
</INVENTORY>
XML

actual=$(opu_extract_opatch_platform "$TMP/property.xml")
[ "$actual" = $'226\tLinux x86-64' ] || {
  echo "property-based ARU platform was not extracted: $actual" >&2
  exit 1
}

cat >"$TMP/nested-generic-only.xml" <<'XML'
<InventoryInstance>
  <patches>
    <patch>
      <osPlatforms>
        <osPlatform id="0"><version>Generic Platform 1</version></osPlatform>
      </osPlatforms>
    </patch>
  </patches>
</InventoryInstance>
XML

if opu_extract_opatch_platform "$TMP/nested-generic-only.xml" >/dev/null 2>&1; then
  echo 'nested patch-local Generic Platform 1 was accepted as the host platform' >&2
  exit 1
fi

printf '<InventoryInstance><osPlatform id="226">\n' >"$TMP/malformed.xml"
if opu_extract_opatch_platform "$TMP/malformed.xml" >/dev/null 2>&1; then
  echo 'malformed OPatch XML was accepted' >&2
  exit 1
fi

# Exercise the collector's evidence boundary with a fake Oracle home. Sourcing
# declares collectors only; every Oracle command in this test is replaced.
. "$ROOT/bin/opu-topology-discover"
mkdir -p "$TMP/home/OPatch" "$TMP/home/inventory/ContentsXML"
: >"$TMP/home/OPatch/opatch"
chmod 700 "$TMP/home/OPatch/opatch"
printf 'VERSION=19.0.0.0.0\n' >"$TMP/home/inventory/ContentsXML/comps.xml"
runtime_owner() { id -un; }
opatch_as_owner() {
  case "$3" in
    version) printf 'OPatch Version: 12.2.0.1.99\n';;
    lspatches) printf '12345678;Fallback only\n';;
    lsinventory) cp "$COLLECT_XML" "$5"; return "$COLLECT_RC";;
    *) return 64;;
  esac
}
printf '<InventoryInstance><patches><patch><patchID>87654321</patchID></patch></patches></InventoryInstance>\n' >"$TMP/patches.xml"
printf '<InventoryInstance><patchID>87654321</patchID>\n' >"$TMP/truncated-patches.xml"
for sample in malformed failed-command valid; do
  COLLECT_XML="$TMP/patches.xml"; COLLECT_RC=0
  [ "$sample" != malformed ] || COLLECT_XML="$TMP/truncated-patches.xml"
  [ "$sample" != failed-command ] || COLLECT_RC=1
  home_json "$TMP/home" >"$TMP/home.json"
  if [ "$sample" = valid ]; then
    jq -e '.opatch_inventory_xml_status == "collected" and .patch_inventory_source == "opatch_lsinventory_xml" and .patches == ["87654321"] and (.opatch_inventory_xml_sha256 | test("^[a-f0-9]{64}$"))' "$TMP/home.json" >/dev/null
  else
    jq -e '.opatch_inventory_xml_status == "failed" and .patch_inventory_source == "opatch_lspatches_fallback" and .patches == ["12345678"] and .opatch_inventory_xml_sha256 == null and .platform.source_sha256 == null' "$TMP/home.json" >/dev/null || {
      echo "$sample OPatch XML was promoted to authoritative patch inventory" >&2
      exit 1
    }
  fi
done
# A failed CRS check must never become healthy because another component's
# output includes "online", or because the text says "not running".
GRID_HOME="$TMP/grid"
mkdir -p "$GRID_HOME/bin"
cat >"$GRID_HOME/bin/crsctl" <<'SH'
#!/usr/bin/env bash
if [ "$1" = query ]; then
  printf '%s\n' 'Oracle Clusterware active version on the cluster is [19.0.0.0.0]' 'The cluster upgrade state is [NORMAL]' 'The cluster active patch level is [123]'
  exit "$GRID_QUERY_RC"
fi
printf '%s\n' "$GRID_HEALTH"
exit "$GRID_CHECK_RC"
SH
chmod 700 "$GRID_HOME/bin/crsctl"
export GRID_QUERY_RC GRID_CHECK_RC GRID_HEALTH
for sample in failed-exit mixed-offline not-running failed-query healthy; do
  GRID_QUERY_RC=0; GRID_CHECK_RC=0; GRID_HEALTH='Oracle High Availability Services is online'
  case "$sample" in
    failed-exit) GRID_CHECK_RC=1;;
    mixed-offline) GRID_HEALTH=$'Oracle High Availability Services is online\nOracle Cluster Ready Services is offline';;
    not-running) GRID_HEALTH='Oracle Cluster Ready Services is not running';;
    failed-query) GRID_QUERY_RC=1;;
  esac
  grid_runtime_json >"$TMP/grid.json"
  if [ "$sample" = healthy ]; then expected=healthy; else expected=unknown; fi
  jq -e --arg expected "$expected" '.status == $expected' "$TMP/grid.json" >/dev/null || {
    echo "$sample CRS check was assigned an incorrect health status" >&2
    exit 1
  }
done
printf '%s\n' 'OPatch platform discovery test passed'
