# Valkey HA Demo — Sentinel + HAProxy + Failover Manager

A narrated, interactive demo that deploys a highly-available Valkey cluster
on Kubernetes and walks through a real primary failover, showing that a
client connecting to one stable endpoint (`valkey-endpoint:6379`) never
needs to know which pod is currently the primary.

## Architecture

```
Applications
     │
     ▼
valkey-endpoint (ClusterIP :6379)   ← stable, never changes
     │
     ▼
HAProxy  +  Failover Manager
     │         │
     │         └─ subscribes to Sentinel pub/sub, updates HAProxy on +switch-master
     │
     ├─► valkey-0  (primary)   ← passes HAProxy's ROLE check
     ├─► valkey-1  (replica)   ← fails the ROLE check
     └─► valkey-2  (replica)   ← fails the ROLE check
```

- **Valkey pods** — each runs a `valkey` container plus a `sentinel` sidecar.
  Sentinel sidecars form a quorum (2 of 3) and decide who the primary is.
- **HAProxy** — the single entry point. It health-checks each Valkey pod with
  a `ROLE` command so only the current primary shows as UP.
- **Failover manager** — subscribes to Sentinel's pub/sub channel and
  rewrites HAProxy's backend the moment Sentinel announces `+switch-master`,
  so HAProxy doesn't have to wait on its own health-check interval.
- **`valkey-endpoint`** — the Service applications actually connect to; it
  points at HAProxy, not at Valkey directly.

There's also a **2-pod mode** (`--pods 2`) for a smaller demo: 2 Valkey pods
plus 3 *standalone* Sentinel pods (separate from the Valkey pods, so Sentinel
survives even if the primary pod is deleted).

## Files

| File | What it is |
|---|---|
| `demo.sh` | The demo driver — narrates and runs each step interactively (see below). |
| `write_read_loop.py` | Standalone script used during the failover step to hammer `valkey-endpoint` with alternating writes/reads and show live continuity (or errors) while the primary changes. |
| `07-client-service.yaml` | Creates the namespace and the `valkey-endpoint` Service (backed by HAProxy) — applied first. |
| `01-sentinel-configmap.yaml` | Sentinel config (`ConfigMap`) shared by all Sentinel instances. |
| `02-valkey-statefulset.yaml` | The 3 Valkey pods (`valkey-0/1/2`), each with a `valkey` + `sentinel` sidecar. Despite the filename, these are plain `Pod` manifests, not an actual StatefulSet — nothing auto-recreates a deleted pod, which is the point of the recovery step. Used when `--pods 3` (default). |
| `02-valkey-2pod.yaml` | 2-pod alternative to the above (`valkey-0/1` only). Used when `--pods 2`. |
| `02-sentinel-pods.yaml` | 3 standalone Sentinel pods (`sentinel-0/1/2`), only used in 2-pod mode. |
| `03-services.yaml` | Internal Services: `valkey-headless`, `valkey-sentinel`, `valkey-read`. |
| `04-haproxy-configmap.yaml` | HAProxy config, including the `ROLE`-based health check. |
| `05-failover-manager-configmap.yaml` | The failover manager's script, mounted into the HAProxy pod. |
| `06-haproxy-deployment.yaml` | The HAProxy `Deployment` (runs the `haproxy` + `failover-manager` containers). |

Everything else in this directory (`04-haproxy-configmap-old*.yaml`,
`test-valkey-sentinel.sh`, `cluster.yaml`, `registry.yaml`, `PandR.yaml`,
`fo.log`, etc.) is left over from other work and isn't part of this demo.

## Running it

```bash
chmod +x demo.sh
./demo.sh [--namespace valkey] [--pods 3|2] [--manifests .] [--op-interval 0.5]
```

- `--namespace` — target namespace (default `valkey`).
- `--pods` — `3` (StatefulSet-style pods + sidecar Sentinels, default) or
  `2` (fewer Valkey pods + standalone Sentinel pods).
- `--manifests` — directory containing the YAML files above (default: current directory).
- `--op-interval` — seconds between each write/read operation in
  `write_read_loop.py` during the failover step (default `0.5`).

## The four steps

1. **Deploy** — applies the manifests in order, waits for every pod and the
   HAProxy rollout to be `Running`.
2. **Verify** — checks replication roles on each Valkey pod, confirms all
   Sentinels agree on the primary, checks HAProxy's stats page, then writes
   a key through `valkey-endpoint` and confirms it replicated everywhere.
3. **Failover** — force-deletes the primary pod (`valkey-0`) to simulate a
   crash, starts `write_read_loop.py` in the background so you can watch
   writes/reads succeed or fail live, waits for Sentinel to elect a new
   primary, then reports write/read continuity and confirms pre-failover
   data survived.
4. **Recovery** — re-applies the pod manifest to bring `valkey-0` back,
   waits for Sentinel to reconfigure it as a replica of the new primary, and
   prints the final cluster + HAProxy state.

## Authentication

Both credential sets are optional; without the Secrets everything runs
unauthenticated as before.

- **Failover manager → Sentinel**: create `valkey-sentinel-auth` with a
  `password` key (and `username` if you use a Sentinel ACL user rather than
  `requirepass`). The sidecar reads it as `SENTINEL_USERNAME` /
  `SENTINEL_PASSWORD`.
- **HAProxy health check → Valkey**: create `valkey-auth` with `username`
  (`default` for plain `requirepass`) and `password`. The `ROLE` check then
  sends `AUTH` first; without it every server fails the check once Valkey
  requires a password.

```bash
kubectl -n valkey create secret generic valkey-sentinel-auth --from-literal=password='...'
kubectl -n valkey create secret generic valkey-auth --from-literal=username=default --from-literal=password='...'
```

Enabling auth on Valkey and Sentinel themselves (`requirepass`,
`masterauth`, `sentinel auth-pass`) is done in their own manifests.

