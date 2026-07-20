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

printf '%s\n' 'OPatch platform discovery test passed'
