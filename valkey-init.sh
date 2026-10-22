#!/bin/bash
set -eu
mkdir -p /data/conf
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
SENTINEL_SVC="valkey-sentinel.${NS}.svc.cluster.local"
cat >/data/conf/valkey.conf <<EOF
port 6379
protected-mode no
bind 0.0.0.0
dir /data
appendonly yes
enable-debug-command local
EOF

PRIMARY_INFO=$(timeout 3 valkey-cli -h "${SENTINEL_SVC}" -p 26379 \
  sentinel get-master-addr-by-name "${SENTINEL_GRP}" 2>/dev/null || true)
case "${PRIMARY_INFO}" in
"")
  echo "sentinel is not available"
  if [[ "${HOSTNAME}" == *"valkey-0"* ]]; then
    echo "Starting as primary (sentinel absent)"
  else
    export PRIMARY_HOST="valkey-0.valkey-headless.${NS}.svc.cluster.local"
    echo "replicaof ${PRIMARY_HOST} ${PRIMARY_PORT:-6379}" >>/data/conf/valkey.conf
    echo "Starting as replica of ${PRIMARY_HOST}:${PRIMARY_PORT:-6379}"
  fi
  ;;
*"${HOSTNAME}"*)
  echo "Starting as primary (sentinel elected)"
  ;;
*)
  PRIMARY_HOST=$(printf '%s\n' "${PRIMARY_INFO}" | head -1 | tr -d '[:space:]')
  PRIMARY_PORT=$(printf '%s\n' "${PRIMARY_INFO}" | tail -1 | tr -d '[:space:]')
  echo "replicaof ${PRIMARY_HOST} ${PRIMARY_PORT:-6379}" >>/data/conf/valkey.conf
  echo "Starting as replica of ${PRIMARY_HOST}:${PRIMARY_PORT:-6379}"
  ;;
esac
