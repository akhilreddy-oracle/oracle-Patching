#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  'query crs activeversion -f')
    printf '%s\n' \
      'Oracle Clusterware active version on the cluster is [19.0.0.0.0].' \
      'The cluster upgrade state is [NORMAL].' \
      'The cluster active patch level is [1].'
    ;;
  'query crs releasepatch') printf '%s\n' 'Oracle Clusterware release patch level is [1].' ;;
  'check crs') printf '%s\n' 'CRS-4638: Oracle High Availability Services is online' ;;
  'check cluster -all') printf '%s\n' 'CRS-4537: Cluster Ready Services is online' ;;
  'stat res -t') printf '%s\n' 'Name Target State Server' 'ora.asm ONLINE ONLINE node1' ;;
  'query css votedisk') printf '%s\n' '1. ONLINE 0000 (/dev/test) [DATA]' 'Located 1 voting disk(s).' ;;
  'query crs softwareversion -all')
    printf '%s\n' \
      'Oracle Clusterware version on node [node1] is [19.0.0.0.0]' \
      'Oracle Clusterware version on node [node2] is [19.0.0.0.0]'
    ;;
  *) printf 'unsupported fake crsctl command: %s\n' "$*" >&2; exit 64 ;;
esac
