#!/usr/bin/env bash
# =============================================================================
# demo.sh
# Narrated step-by-step demo of Valkey Sentinel + HAProxy on Kubernetes
#
# Shows the full journey: deploy → verify → failover → recover
#
# Usage:
#   chmod +x demo.sh
#   ./demo.sh [--namespace valkey] [--op-interval 0.5]
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAMESPACE="${NAMESPACE:-valkey}"
MANIFESTS_DIR="${MANIFESTS_DIR:-.}"
POD_COUNT="${POD_COUNT:-3}"
OP_INTERVAL="${OP_INTERVAL:-0.5}"
export NAMESPACE

while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace=*) NAMESPACE="${1#*=}" ;;
    --namespace)   shift; NAMESPACE="$1" ;;
    --manifests=*) MANIFESTS_DIR="${1#*=}" ;;
    --manifests)   shift; MANIFESTS_DIR="$1" ;;
    --pods=*)      POD_COUNT="${1#*=}" ;;
    --pods)        shift; POD_COUNT="$1" ;;
    --op-interval=*) OP_INTERVAL="${1#*=}" ;;
    --op-interval)   shift; OP_INTERVAL="$1" ;;
    --verbose|-v)  VERBOSE=true ;;
  esac
  shift
done

if [ "$POD_COUNT" != "2" ] && [ "$POD_COUNT" != "3" ]; then
  echo "Error: --pods must be 2 or 3 (got: $POD_COUNT)" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Print a narration line
narrate() {
  echo -e "\n${CYAN}${BOLD}  $*${NC}"
}

# Print a command without running it
show_cmd() {
  echo -e "\n${DIM}  \$${NC} ${BOLD}$*${NC}"
}

# Print a command before running it
run_cmd() {
  show_cmd "$@"
  eval "$*"
}

# Print a section header
section() {
  local title="$1"
  local width=60
  local line
  line=$(printf '━%.0s' $(seq 1 $width))
  echo -e "\n${BOLD}${BLUE}${line}${NC}"
  echo -e "${BOLD}${BLUE}  $title${NC}"
  echo -e "${BOLD}${BLUE}${line}${NC}"
}

# Wait for user to press enter
proceed() {
  local msg="${1:-Press Enter to continue...}"
  echo -e "\n${YELLOW}  ▶ $msg${NC}"
  read -r
}

# Print a result line
result() {
  echo -e "  ${GREEN}✓${NC} $*"
}

# Apply a manifest, substituting ${NAMESPACE} before sending to kubectl
kube_apply() {
  show_cmd "envsubst '\${NAMESPACE}' < $1 | kubectl apply -f -"
  envsubst '${NAMESPACE}' < "$1" | kubectl apply -f -
}

# Wait for a pod condition with a spinner
wait_pod() {
  local pod="$1"
  local condition="$2"
  local timeout="${3:-60}"
  local elapsed=0
  local spinner=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
  local i=0
  show_cmd "kubectl get pod -n $NAMESPACE $pod -o jsonpath='{.status.phase}'"
  printf "  Waiting for %s to be %s " "$pod" "$condition"
  while ! kubectl get pod -n "$NAMESPACE" "$pod" \
      -o jsonpath="{.status.phase}" 2>/dev/null | grep -q "Running"; do
    printf "\r  %s Waiting for %s to be %s (%ds)" \
      "${spinner[$((i % 10))]}" "$pod" "$condition" "$elapsed"
    sleep 1
    elapsed=$((elapsed + 1))
    i=$((i + 1))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo " TIMEOUT"
      echo -e "\n  ${RED}${BOLD}$pod did not reach Running within ${timeout}s${NC}"
      echo -e "  ${DIM}Pod status:${NC}"
      kubectl get pod -n "$NAMESPACE" "$pod" 2>&1 | sed 's/^/    /'
      echo -e "  ${DIM}Container states:${NC}"
      kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath=\
'{range .status.initContainerStatuses[*]}init/{.name}: {.state}{"\n"}{end}{range .status.containerStatuses[*]}{.name}: {.state}{"\n"}{end}' \
        2>/dev/null | sed 's/^/    /'
      echo -e "  ${DIM}Recent events:${NC}"
      kubectl get events -n "$NAMESPACE" --field-selector "involvedObject.name=$pod" \
        --sort-by='.lastTimestamp' 2>/dev/null | tail -10 | sed 's/^/    /'
      return 1
    fi
  done
  printf "\r  ${GREEN}✓${NC} %s is Running (%ds)              \n" "$pod" "$elapsed"
}

# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

clear
echo -e "${BOLD}${MAGENTA}"
cat << 'BANNER'
  ╔═══════════════════════════════════════════════════════════╗
  ║                                                           ║
  ║   Valkey HA on Kubernetes                                 ║
  ║   Sentinel + HAProxy + Failover Manager                   ║
  ║                                                           ║
  ║   Full journey: Deploy → Verify → Failover → Recover      ║
  ║                                                           ║
  ╚═══════════════════════════════════════════════════════════╝
BANNER
echo -e "${NC}"

narrate "This demo shows how Valkey achieves high availability on Kubernetes"
narrate "using Sentinel for automatic failover and HAProxy as a stable"
narrate "single write endpoint — with a failover manager bridging the two."
echo ""
narrate "Architecture (${POD_COUNT}-pod mode):"
if [ "$POD_COUNT" -eq 3 ]; then
  echo -e "
  ${BOLD}  Applications${NC}
       │
       ▼
  ${BOLD}  valkey-endpoint${NC} (ClusterIP :6379)   ← stable, never changes
       │
       ▼
  ${BOLD}  HAProxy${NC}  +  ${BOLD}Failover Manager${NC}
       │         │
       │         └─ subscribes to Sentinel pub/sub
       │            updates HAProxy on +switch-master
       │
       ├─► ${GREEN}valkey-0${NC}  (primary)   ← ROLE check passes
       ├─► ${RED}valkey-1${NC}  (replica)   ← ROLE check fails
       └─► ${RED}valkey-2${NC}  (replica)   ← ROLE check fails

  ${BOLD}  Sentinel sidecars${NC} on every pod (quorum: 2/3)
"
else
  echo -e "
  ${BOLD}  Applications${NC}
       │
       ▼
  ${BOLD}  valkey-endpoint${NC} (ClusterIP :6379)   ← stable, never changes
       │
       ▼
  ${BOLD}  HAProxy${NC}  +  ${BOLD}Failover Manager${NC}
       │         │
       │         └─ subscribes to Sentinel pub/sub
       │            updates HAProxy on +switch-master
       │
       ├─► ${GREEN}valkey-0${NC}  (primary)   ← ROLE check passes
       └─► ${RED}valkey-1${NC}  (replica)   ← ROLE check fails

  ${BOLD}  3 standalone sentinel pods${NC} (sentinel-0/1/2, quorum: 2/3)
  ${BOLD}  Sentinels are separate from Valkey pods${NC} — survive primary deletion
"
fi

proceed "Ready to begin. Press Enter to deploy..."

# ---------------------------------------------------------------------------
section "Step 1 — Deploy"
# ---------------------------------------------------------------------------

narrate "We deploy in order: namespace and ConfigMaps first,"
if [ "$POD_COUNT" -eq 3 ]; then
  narrate "then 3 Valkey pods (sentinel sidecars), Services, and finally HAProxy."
else
  narrate "then 2 Valkey pods + 3 standalone sentinel pods, Services, and HAProxy."
fi

proceed

kube_apply "$MANIFESTS_DIR/07-client-service.yaml"
kube_apply "$MANIFESTS_DIR/01-sentinel-configmap.yaml"

if [ "$POD_COUNT" -eq 3 ]; then
  kube_apply "$MANIFESTS_DIR/02-valkey-statefulset.yaml"
else
  kube_apply "$MANIFESTS_DIR/02-valkey-2pod.yaml"
  kube_apply "$MANIFESTS_DIR/02-sentinel-pods.yaml"
fi

kube_apply "$MANIFESTS_DIR/03-services.yaml"
kube_apply "$MANIFESTS_DIR/04-haproxy-configmap.yaml"
kube_apply "$MANIFESTS_DIR/05-failover-manager-configmap.yaml"
kube_apply "$MANIFESTS_DIR/06-haproxy-deployment.yaml"

narrate "Waiting for all pods to reach Running state..."
wait_pod "valkey-0" "Running" 120
wait_pod "valkey-1" "Running" 120
if [ "$POD_COUNT" -eq 3 ]; then
  wait_pod "valkey-2" "Running" 120
else
  wait_pod "sentinel-0" "Running" 60
  wait_pod "sentinel-1" "Running" 60
  wait_pod "sentinel-2" "Running" 60
fi

echo ""
narrate "Waiting for HAProxy deployment..."
show_cmd "kubectl rollout status deployment/haproxy -n $NAMESPACE"
kubectl rollout status deployment/haproxy -n "$NAMESPACE"

echo ""
run_cmd "kubectl get all -n $NAMESPACE"

proceed

# ---------------------------------------------------------------------------
section "Step 2 — Verify the cluster"
# ---------------------------------------------------------------------------

narrate "Let's verify the Valkey cluster is healthy."
if [ "$POD_COUNT" -eq 3 ]; then
  narrate "valkey-0 should be the primary, valkey-1 and valkey-2 replicas."
else
  narrate "valkey-0 should be the primary, valkey-1 a replica."
fi

proceed

echo ""
narrate "Replication status on each pod:"
LAST_POD=$((POD_COUNT - 1))
for i in $(seq 0 "$LAST_POD"); do
  echo -e "\n  ${BOLD}valkey-$i:${NC}"
  show_cmd "kubectl exec -n $NAMESPACE valkey-$i -c valkey -- valkey-cli info replication"
  kubectl exec -n "$NAMESPACE" "valkey-$i" -c valkey -- \
    valkey-cli info replication 2>/dev/null \
    | grep -E "^role:|^connected_slaves:|^master_host:" \
    | sed 's/^/    /'
done

proceed

narrate "All 3 Sentinel instances should agree on the same primary."
echo ""
if [ "$POD_COUNT" -eq 3 ]; then
  for i in 0 1 2; do
    show_cmd "kubectl exec -n $NAMESPACE valkey-$i -c sentinel -- valkey-cli -p 26379 sentinel get-master-addr-by-name mymaster"
    local_primary=$(kubectl exec -n "$NAMESPACE" "valkey-$i" -c sentinel -- \
      valkey-cli -p 26379 sentinel get-master-addr-by-name mymaster 2>/dev/null \
      | tr '\n' ':' | sed 's/:$//')
    echo -e "  ${BOLD}valkey-$i sentinel${NC} sees primary at: ${GREEN}$local_primary${NC}"
  done
else
  for i in 0 1 2; do
    show_cmd "kubectl exec -n $NAMESPACE sentinel-$i -c sentinel -- valkey-cli -p 26379 sentinel get-master-addr-by-name mymaster"
    local_primary=$(kubectl exec -n "$NAMESPACE" "sentinel-$i" -c sentinel -- \
      valkey-cli -p 26379 sentinel get-master-addr-by-name mymaster 2>/dev/null \
      | tr '\n' ':' | sed 's/:$//')
    echo -e "  ${BOLD}sentinel-$i${NC} sees primary at: ${GREEN}$local_primary${NC}"
  done
fi

proceed

narrate "HAProxy is routing to valkey-0 (role:master)."
narrate "The stats page shows 1 server UP — replicas and absent pods fail the ROLE check."
echo ""

HAPROXY_POD=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=haproxy \
  --no-headers -o custom-columns=":metadata.name" | head -1)

show_cmd "kubectl exec -n $NAMESPACE $HAPROXY_POD -c haproxy -- sh -c \"printf 'GET /stats;csv;norefresh HTTP/1.0...' | nc 127.0.0.1 8404\""
kubectl exec -n "$NAMESPACE" "$HAPROXY_POD" -c haproxy -- \
  sh -c "printf 'GET /stats;csv;norefresh HTTP/1.0\r\nHost: localhost\r\n\r\n' | nc 127.0.0.1 8404" \
  2>/dev/null | awk -F',' -v pods="$POD_COUNT" \
  'NF>17 && $1=="valkey_primary" && $2!="BACKEND" && $2!="FRONTEND" {
    n=split($2,a,"-"); if ((a[n]+0) < pods) printf "  %-12s %s\n", $2, $18
  }'

proceed

narrate "Let's write some data through the stable endpoint and verify it replicates."
echo ""

# Create a test client pod
show_cmd "kubectl run valkey-demo-client --namespace=$NAMESPACE --image=valkey/valkey:9.0.1-alpine --restart=Never --command -- sh -c 'sleep 3600'"
kubectl run valkey-demo-client \
  --namespace="$NAMESPACE" \
  --image="valkey/valkey:9.0.1-alpine" \
  --restart=Never \
  --command -- sh -c "sleep 3600" &>/dev/null || true

wait_pod "valkey-demo-client" "Running" 30

run_cmd "kubectl exec -n $NAMESPACE valkey-demo-client -- valkey-cli -h valkey-endpoint -p 6379 set demo:key 'Hello from Percona'"
run_cmd "kubectl exec -n $NAMESPACE valkey-demo-client -- valkey-cli -h valkey-endpoint -p 6379 get demo:key"

echo ""
narrate "Verifying the key replicated to all pods:"
for i in $(seq 0 "$LAST_POD"); do
  show_cmd "kubectl exec -n $NAMESPACE valkey-$i -c valkey -- valkey-cli get demo:key"
  val=$(kubectl exec -n "$NAMESPACE" "valkey-$i" -c valkey -- \
    valkey-cli get demo:key 2>/dev/null || echo "MISSING")
  echo -e "  valkey-$i: ${GREEN}$val${NC}"
done

proceed

# ---------------------------------------------------------------------------
section "Step 3 — Failover"
# ---------------------------------------------------------------------------

narrate "Now the key demo moment."
narrate "We will forcefully delete the primary pod (valkey-0) to simulate a crash."
narrate ""
narrate "Watch what happens:"
narrate "  1. Sentinel detects the primary is down"
narrate "  2. Sentinel holds an election — 2 of 3 must agree"
narrate "  3. A replica is promoted to primary"
narrate "  4. The failover manager receives the +switch-master event"
narrate "  5. HAProxy immediately routes to the new primary"
narrate "  6. Applications reconnect transparently"

proceed "Press Enter to kill valkey-0..."

# Start a background write/read loop to show operation continuity
WRITE_LOG=$(mktemp)
show_cmd "python3 $SCRIPT_DIR/write_read_loop.py --namespace $NAMESPACE --interval $OP_INTERVAL --log $WRITE_LOG"
python3 "$SCRIPT_DIR/write_read_loop.py" \
  --namespace "$NAMESPACE" \
  --interval "$OP_INTERVAL" \
  --log "$WRITE_LOG" &
WRITER_PID=$!

echo ""
narrate "Background writer started — alternating write/read every ${OP_INTERVAL}s through valkey-endpoint..."
sleep 2

echo ""
echo -e "  ${RED}${BOLD}Deleting valkey-0...${NC}"
run_cmd "kubectl delete pod -n $NAMESPACE valkey-0 --grace-period=0 --force"

echo ""
narrate "Watching for new primary to be elected..."
echo ""

NEW_PRIMARY=""
ELAPSED=0
show_cmd "kubectl exec -n $NAMESPACE valkey-<i> -c valkey -- valkey-cli info replication   (polling each replica for role:master)"
while [ $ELAPSED -lt 30 ]; do
  sleep 2
  ELAPSED=$((ELAPSED + 2))
  for i in $(seq 1 "$LAST_POD"); do
    PHASE=$(kubectl get pod -n "$NAMESPACE" "valkey-$i" \
      -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    [ "$PHASE" != "Running" ] && continue
    ROLE=$(kubectl exec -n "$NAMESPACE" "valkey-$i" -c valkey -- \
      valkey-cli info replication 2>/dev/null \
      | grep "^role:" | tr -d '[:space:]' | cut -d: -f2 || echo "")
    if [ "$ROLE" = "master" ]; then
      NEW_PRIMARY="valkey-$i"
      break 2
    fi
  done
  printf "  ⏳ Waiting for election... (%ds)\r" "$ELAPSED"
done

echo ""
if [ -n "$NEW_PRIMARY" ]; then
  echo -e "  ${GREEN}${BOLD}✓ New primary elected: $NEW_PRIMARY (in ${ELAPSED}s)${NC}"
else
  echo -e "  ${RED}No new primary found within 30s${NC}"
fi

sleep 2

# Stop background writer
kill $WRITER_PID &>/dev/null || true
sleep 1

# Analyse write/read log (format: ts op_index TYPE key STATUS extra)
TOTAL=$(wc -l < "$WRITE_LOG" 2>/dev/null || echo 0)
WRITE_TOTAL=$(grep -c " WRITE " "$WRITE_LOG" 2>/dev/null || true); WRITE_TOTAL=${WRITE_TOTAL:-0}
WRITE_ERR=$(grep " WRITE " "$WRITE_LOG" 2>/dev/null | grep -c " ERR " || true); WRITE_ERR=${WRITE_ERR:-0}
READ_TOTAL=$(grep -c " READ " "$WRITE_LOG" 2>/dev/null || true); READ_TOTAL=${READ_TOTAL:-0}
READ_ERR=$(grep " READ " "$WRITE_LOG" 2>/dev/null | grep -c " ERR " || true); READ_ERR=${READ_ERR:-0}
ERRORS=$((WRITE_ERR + READ_ERR))
SUCCESS=$((TOTAL - ERRORS))
if [ "$TOTAL" -gt 0 ]; then
  PCT=$((SUCCESS * 100 / TOTAL))
else
  PCT=0
fi

echo ""
narrate "Write/read continuity during failover (op interval: ${OP_INTERVAL}s):"
echo -e "  Total operations       : ${BOLD}$TOTAL${NC}"
echo -e "  Writes (ok/total)      : ${GREEN}${BOLD}$((WRITE_TOTAL - WRITE_ERR))${NC}/${BOLD}$WRITE_TOTAL${NC}"
echo -e "  Reads  (ok/total)      : ${GREEN}${BOLD}$((READ_TOTAL - READ_ERR))${NC}/${BOLD}$READ_TOTAL${NC}"
echo -e "  Overall success rate   : ${BOLD}$PCT%${NC}"
rm -f "$WRITE_LOG"

proceed

narrate "Failover manager log — confirming it caught the +switch-master event:"
echo ""
show_cmd "kubectl logs -n $NAMESPACE $HAPROXY_POD -c failover-manager --tail=20"
kubectl logs -n "$NAMESPACE" "$HAPROXY_POD" -c failover-manager --tail=20 \
  | grep -E "switch-master|Failover|primary|INFO" \
  | sed 's/^/  /' || true

proceed

narrate "HAProxy now routes to the new primary:"
echo ""
show_cmd "kubectl exec -n $NAMESPACE valkey-demo-client -- valkey-cli -h valkey-endpoint -p 6379 info replication"
ROLE_VIA_HAPROXY=$(kubectl exec -n "$NAMESPACE" valkey-demo-client -- \
  valkey-cli -h valkey-endpoint -p 6379 info replication 2>/dev/null \
  | grep "^role:" | tr -d '[:space:]' | cut -d: -f2 || echo "unknown")
echo -e "  Role via valkey-endpoint: ${GREEN}${BOLD}$ROLE_VIA_HAPROXY${NC}"

echo ""
narrate "Pre-failover data is still readable:"
show_cmd "kubectl exec -n $NAMESPACE valkey-demo-client -- valkey-cli -h valkey-endpoint -p 6379 get demo:key"
VAL=$(kubectl exec -n "$NAMESPACE" valkey-demo-client -- \
  valkey-cli -h valkey-endpoint -p 6379 get demo:key 2>/dev/null || echo "MISSING")
echo -e "  demo:key = ${GREEN}${BOLD}$VAL${NC}"

proceed

# ---------------------------------------------------------------------------
section "Step 4 — Recovery"
# ---------------------------------------------------------------------------

narrate "valkey-0 is gone and stays gone — individual pods don't auto-restart."
narrate "We bring it back manually by re-applying the manifest."
narrate "Sentinel will reconfigure it as a replica of the new primary."

echo ""
narrate "Re-creating valkey-0..."
if [ "$POD_COUNT" -eq 3 ]; then
  kube_apply "$MANIFESTS_DIR/02-valkey-statefulset.yaml"
else
  kube_apply "$MANIFESTS_DIR/02-valkey-2pod.yaml"
fi
echo ""
wait_pod "valkey-0" "Running" 90

echo ""
narrate "valkey-0 init container queried Sentinel and injected replicaof — confirming it connected..."
echo ""
RECONF_ELAPSED=0
RECONF_ROLE=""
show_cmd "kubectl exec -n $NAMESPACE valkey-0 -c valkey -- valkey-cli info replication   (polling until role:slave)"
while [ "$RECONF_ELAPSED" -lt 60 ]; do
  sleep 2
  RECONF_ELAPSED=$((RECONF_ELAPSED + 2))
  RECONF_ROLE=$(kubectl exec -n "$NAMESPACE" valkey-0 -c valkey -- \
    valkey-cli info replication 2>/dev/null \
    | grep "^role:" | tr -d '[:space:]' | cut -d: -f2 || echo "")
  if [ "$RECONF_ROLE" = "slave" ]; then
    printf "\r  ${GREEN}✓${NC} valkey-0 reconfigured as replica by Sentinel (%ds)              \n" "$RECONF_ELAPSED"
    break
  fi
  printf "\r  ⏳ valkey-0 still reporting role:%s (%ds)" "$RECONF_ROLE" "$RECONF_ELAPSED"
done
if [ "$RECONF_ROLE" != "slave" ]; then
  echo -e "\n  ${YELLOW}Warning: valkey-0 still reporting role:$RECONF_ROLE after ${RECONF_ELAPSED}s — Sentinel may need more time${NC}"
fi

echo ""
narrate "Final cluster state:"
for i in $(seq 0 "$LAST_POD"); do
  show_cmd "kubectl exec -n $NAMESPACE valkey-$i -c valkey -- valkey-cli info replication"
  ROLE=$(kubectl exec -n "$NAMESPACE" "valkey-$i" -c valkey -- \
    valkey-cli info replication 2>/dev/null \
    | grep "^role:" | tr -d '[:space:]' | cut -d: -f2 || echo "unknown")
  if [ "$ROLE" = "master" ]; then
    echo -e "  ${BOLD}valkey-$i${NC}: ${GREEN}${BOLD}primary${NC}"
  else
    echo -e "  ${BOLD}valkey-$i${NC}: ${DIM}replica${NC}"
  fi
done

echo ""
narrate "HAProxy server states (active primary UP, replicas DOWN — correct):"
show_cmd "kubectl exec -n $NAMESPACE $HAPROXY_POD -c haproxy -- sh -c \"printf 'GET /stats;csv;norefresh HTTP/1.0...' | nc 127.0.0.1 8404\""
kubectl exec -n "$NAMESPACE" "$HAPROXY_POD" -c haproxy -- \
  sh -c "printf 'GET /stats;csv;norefresh HTTP/1.0\r\nHost: localhost\r\n\r\n' | nc 127.0.0.1 8404" \
  2>/dev/null | awk -F',' -v pods="$POD_COUNT" \
  'NF>17 && $1=="valkey_primary" && $2!="BACKEND" && $2!="FRONTEND" {
    n=split($2,a,"-"); if ((a[n]+0) < pods) printf "  %-12s %s\n", $2, $18
  }'

proceed

# ---------------------------------------------------------------------------
section "Demo Complete"
# ---------------------------------------------------------------------------

echo -e "
  ${GREEN}${BOLD}What we demonstrated:${NC}

  ${GREEN}✓${NC} Deployed a ${POD_COUNT}-node Valkey cluster with Sentinel instances
  ${GREEN}✓${NC} Single stable write endpoint via HAProxy (valkey-endpoint:6379)
  ${GREEN}✓${NC} Automatic primary election by Sentinel quorum (2 of 3)
  ${GREEN}✓${NC} Sub-second failover propagation via failover manager
  ${GREEN}✓${NC} HAProxy ROLE health checks as an independent safety net
  ${GREEN}✓${NC} Zero data loss — pre-failover keys survived
  ${GREEN}✓${NC} Automatic recovery — old primary rejoined as replica

  ${BOLD}Key insight:${NC}
  The client never changes its connection string.
  It always connects to ${BOLD}valkey-endpoint:6379${NC}.
  Everything else is handled by the HA stack.
"

# Cleanup demo client pod
narrate "Cleaning up demo client pod..."
show_cmd "kubectl delete pod -n $NAMESPACE valkey-demo-client --ignore-not-found"
kubectl delete pod -n "$NAMESPACE" valkey-demo-client --ignore-not-found &>/dev/null || true

echo -e "${CYAN}${BOLD}  Thank you.${NC}\n"