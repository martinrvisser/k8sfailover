#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="valkey-ha-e2e"
NAMESPACE="valkey"
LOG_FILE="e2e_traffic.log"
RESULTS_FILE="e2e_results.log"
OP_INTERVAL="${OP_INTERVAL:-0.2}"

# The manifest points at a private registry KinD can't reach, so build the
# failover-manager image locally and swap it in at apply time.
REGISTRY_FM_IMAGE="10.30.52.67:32000/failover-manager:latest"
FM_IMAGE="${FM_IMAGE:-failover-manager:e2e}"
PUBLIC_IMAGES=(percona/valkey-bundle:9.1 haproxy:2.9-alpine)

echo "🐳 Building failover-manager image '$FM_IMAGE'..."
docker build -t "$FM_IMAGE" -f Dockerfile.failover-manager .

echo "🐳 Pulling public images..."
for img in "${PUBLIC_IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
done

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
    echo "🧹 Deleting leftover KinD cluster '$CLUSTER_NAME' from a previous run..."
    kind delete cluster --name "$CLUSTER_NAME"
fi

echo "🚀 Spinning up KinD cluster '$CLUSTER_NAME'..."
kind create cluster --name "$CLUSTER_NAME"

echo "📥 Loading images into KinD..."
# Not `kind load docker-image`: it imports with --all-platforms, which fails
# with "content digest ... not found" when Docker uses the containerd image
# store, because `docker save` only holds layers for the local platform.
# --platform must be explicit: ctr's default matcher on amd64 also accepts
# linux/386, whose layers were never pulled.
PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
for node in $(kind get nodes --name "$CLUSTER_NAME"); do
    for img in "$FM_IMAGE" "${PUBLIC_IMAGES[@]}"; do
        docker save "$img" | docker exec -i "$node" \
            ctr --namespace=k8s.io images import --digests --snapshotter=overlayfs \
                --platform "$PLATFORM" -
    done
done

echo "📦 Applying namespace and services..."
export NAMESPACE="valkey"
envsubst '${NAMESPACE}' < "07-client-service.yaml" | kubectl apply -f -

echo "⏳ Waiting for the default ServiceAccount to be provisioned..."
while ! kubectl get serviceaccount default -n "$NAMESPACE" >/dev/null 2>&1; do
    sleep 1
done

echo "📦 Applying remaining Valkey HA manifests..."
for file in 01-sentinel-configmap.yaml 02-valkey-statefulset.yaml 03-services.yaml 04-haproxy-configmap.yaml 05-failover-manager-configmap.yaml 06-haproxy-deployment.yaml; do
    envsubst '${NAMESPACE}' < "$file" \
        | sed "s|image: ${REGISTRY_FM_IMAGE}|image: ${FM_IMAGE}\n          imagePullPolicy: IfNotPresent|" \
        | kubectl apply -f -
done

echo "⏳ Waiting for all pods in namespace '$NAMESPACE' to be Ready..."
# Wait for the HAProxy deployment and the Valkey pods
kubectl wait --namespace "$NAMESPACE" --for=condition=Ready pods --all --timeout=300s

# Pod readiness (a PING) isn't enough: Sentinel spends ~30s in TILT after
# startup before it has discovered the replicas and the other Sentinels, and
# HAProxy caches the early NXDOMAIN for valkey-0 for 30s (`hold nx`). Failing
# over before then means no quorum and no primary in HAProxy.
wait_for() {  # wait_for <timeout_s> <description> <command...>
    local timeout=$1 desc=$2; shift 2
    local deadline=$((SECONDS + timeout))
    until "$@"; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "❌ TEST FAILED: timed out after ${timeout}s waiting for $desc."
            exit 1
        fi
        sleep 2
    done
}

sentinels_converged() {
    local pod info
    for pod in valkey-0 valkey-1 valkey-2; do
        info=$(kubectl exec -n "$NAMESPACE" "$pod" -c sentinel -- \
            valkey-cli -p 26379 sentinel master mymaster 2>/dev/null) || return 1
        # Reply is alternating key/value lines.
        awk 'NR%2==1{k=$0; next} {v[k]=$0}
             END{exit !(v["flags"]=="master" && v["num-slaves"]>=2 && v["num-other-sentinels"]>=2)}' \
            <<<"$info" || return 1
    done
}

haproxy_routes_to_primary() {
    # Only valkey-0 UP: replicas start out UP until their ROLE check fails.
    kubectl exec -n "$NAMESPACE" deploy/haproxy -c haproxy -- \
        wget -qO- 'http://127.0.0.1:8404/stats;csv' 2>/dev/null \
        | awk -F, '$1=="valkey_primary" && $2!="BACKEND" && $18=="UP"{n++; s=$2}
                   END{exit !(n==1 && s=="valkey-0")}'
}

echo "⏳ Waiting for Sentinels to discover replicas and each other..."
wait_for 120 "Sentinel convergence" sentinels_converged

echo "⏳ Waiting for HAProxy to route only to valkey-0..."
wait_for 90 "HAProxy to mark valkey-0 as the only UP server" haproxy_routes_to_primary

echo "🚦 Starting continuous write/read traffic..."
# write_read_loop.py opens its own port-forward to service/valkey-endpoint and
# appends one "<ts> <op#> <WRITE|READ> <key> <OK|ERR> <detail>" line per
# operation to --log; its live console output goes to $LOG_FILE.
rm -f "$RESULTS_FILE"
python3 write_read_loop.py --namespace "$NAMESPACE" --log "$RESULTS_FILE" \
    --interval "$OP_INTERVAL" > "$LOG_FILE" 2>&1 &
TRAFFIC_PID=$!

echo "🌊 Letting baseline traffic flow for 10 seconds..."
sleep 10

if ! kill -0 "$TRAFFIC_PID" 2>/dev/null; then
    echo "❌ TEST FAILED: traffic generator exited before the failover was triggered."
    cat "$LOG_FILE"
    exit 1
fi

echo "🔥 TRIGGERING FAILOVER: Force deleting primary pod (valkey-0)..."
kubectl delete pod valkey-0 -n "$NAMESPACE" --force --grace-period=0

echo "⏳ Waiting 20 seconds for Sentinel quorum, election, and Failover Manager HAProxy rewrite..."
sleep 20

echo "🛑 Stopping traffic generator..."
kill "$TRAFFIC_PID" 2>/dev/null || true
wait "$TRAFFIC_PID" 2>/dev/null || true

echo "📊 Analyzing failover logs..."
OK_COUNT=$(grep -c ' OK ' "$RESULTS_FILE" 2>/dev/null || true)
ERROR_COUNT=$(grep -c ' ERR ' "$RESULTS_FILE" 2>/dev/null || true)
echo "   ${OK_COUNT:-0} operations succeeded, ${ERROR_COUNT:-0} failed (details in $RESULTS_FILE)"

if [ "${OK_COUNT:-0}" -eq 0 ]; then
    echo "❌ TEST FAILED: no successful operations recorded; traffic never reached Valkey."
    cat "$LOG_FILE"
    exit 1
fi

echo "📉 ERR lines during the failover window: ${ERROR_COUNT:-0}"
echo "👑 Primary per Sentinel after failover: $(kubectl exec -n "$NAMESPACE" valkey-1 -c sentinel -- \
    valkey-cli -p 26379 sentinel get-master-addr-by-name mymaster 2>/dev/null | head -1)"

echo "🧹 Tearing down KinD cluster..."
kind delete cluster --name "$CLUSTER_NAME"
