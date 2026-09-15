#!/usr/bin/env bash
# Operator-run, read-only inventory. No installs, writes, key reads or Oracle work.
set -euo pipefail

printf 'HOST %s\n' "$(hostname)"
printf 'USER %s UID %s\n' "$(id -un)" "$(id -u)"
printf 'KERNEL %s\n' "$(uname -srmo)"
if [ -r /etc/os-release ]; then
  awk -F= '$1 == "PRETTY_NAME" {print "OS " $2}' /etc/os-release
fi
printf 'CPU_COUNT %s\n' "$(getconf _NPROCESSORS_ONLN)"
if [ -r /proc/meminfo ]; then
  awk '$1 == "MemTotal:" || $1 == "MemAvailable:" {print $0}' /proc/meminfo
fi
df -Pk /opt /var/lib /tmp
for command in bash jq xmllint tar gzip ssh sha256sum flock systemctl nginx useradd; do
  if command -v "$command" >/dev/null 2>&1; then
    printf 'DEPENDENCY %s present\n' "$command"
  else
    printf 'DEPENDENCY %s missing\n' "$command"
  fi
done
# Oracle Linux may retain Python 3.6 as python3 alongside a supported version.
# Inspect executables, not shell aliases/functions; never change alternatives.
python_ready=0
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if python_path=$(type -P -- "$candidate"); then
    if "$python_path" -B - <<'PY'
import importlib.util
import sys
venv = importlib.util.find_spec('venv') is not None
ensurepip = importlib.util.find_spec('ensurepip') is not None
print('PYTHON_CANDIDATE ' + sys.executable + ' ' + sys.version.split()[0])
print('PYTHON_VENV ' + str(venv))
print('PYTHON_ENSUREPIP ' + str(ensurepip))
sys.exit(0 if sys.version_info >= (3, 10) and venv and ensurepip else 1)
PY
    then
      printf 'PYTHON_SELECTED %s\n' "$python_path"
      python_ready=1
      break
    fi
  fi
done
if [ "$python_ready" -eq 0 ]; then
  printf '%s\n' 'PYTHON_BLOCKER: controller dependencies require Python 3.10+ with venv and ensurepip; install a supported versioned interpreter'
fi
for path in /opt/oracle-patching /etc/oracle-patching /var/lib/oracle-patching /var/lib/oracle-patching-home /var/lib/oracle-patching-ollama; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    printf 'EXISTING_PATH %s\n' "$path"
  fi
done
for account in opu-controller opu-ollama; do
  if id "$account" >/dev/null 2>&1; then printf 'EXISTING_ACCOUNT %s\n' "$account"; fi
done
if command -v ss >/dev/null 2>&1; then
  printf '%s\n' 'LISTENERS (80, 443, 8765, 11434 only)'
  ss -ltn | awk 'NR == 1 || $4 ~ /:(80|443|8765|11434)$/'
fi
if command -v systemctl >/dev/null 2>&1; then
  for unit in nginx oracle-patching opu-ollama ollama; do
    printf 'SERVICE %s ' "$unit"
    systemctl is-active "$unit" 2>/dev/null || true
  done
fi
if command -v getenforce >/dev/null 2>&1; then getenforce; fi
if command -v getsebool >/dev/null 2>&1; then getsebool httpd_can_network_connect 2>/dev/null || true; fi
if command -v nvidia-smi >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
  timeout 5 nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true
else
  printf '%s\n' 'GPU inventory unavailable; CPU/RAM sizing still applies'
fi
printf '%s\n' 'READ_ONLY_COMPLETE: no install, state migration, service restart or Oracle operation was performed'
