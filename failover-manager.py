#!/usr/bin/env python3
"""
Sentinel failover manager for HAProxy.

Subscribes to Valkey Sentinel pub/sub and listens for +switch-master
events. On failover, updates HAProxy via its Unix socket to immediately
route traffic to the new primary without waiting for the next poll cycle.

The HAProxy tcp-check (role:master) acts as a safety net - if this
process is slow or crashes, HAProxy will self-correct within ~1s.
"""
import os
import socket
import time
import logging
import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("failover-manager")

SENTINEL_HOST = os.environ.get("SENTINEL_HOST", "valkey-sentinel")
SENTINEL_PORT = int(os.environ.get("SENTINEL_PORT", "26379"))
MASTER_NAME   = os.environ.get("MASTER_NAME", "mymaster")
HAPROXY_SOCK  = os.environ.get("HAPROXY_SOCK", "/var/run/haproxy/haproxy.sock")
BACKEND_NAME  = os.environ.get("HAPROXY_BACKEND", "valkey_primary")
NAMESPACE     = os.environ.get("NAMESPACE", "valkey")

# Maps pod IP -> server name in HAProxy backend
# HAProxy uses the headless DNS names as server names
HAPROXY_SERVERS = {
    "valkey-0": "valkey-0",
    "valkey-1": "valkey-1",
    "valkey-2": "valkey-2",
}


def haproxy_cmd(cmd: str) -> str:
    """Send a command to HAProxy via Unix socket and return the response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(HAPROXY_SOCK)
        s.sendall((cmd + "\n").encode())
        response = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response += chunk
    return response.decode().strip()


def get_server_name(addr: str) -> str | None:
    """
    Map a sentinel-reported address to an HAProxy server name.
    With announce-hostnames yes, sentinel reports FQDNs like
    valkey-1.valkey-headless.ns.svc.cluster.local — extract the pod
    name from the first DNS label. Falls back to IP resolution.
    """
    pod = addr.split(".")[0]
    if pod in HAPROXY_SERVERS:
        return pod
    # Fallback: addr is a raw IP — resolve each known pod and compare.
    import socket as _sock
    for server_name in HAPROXY_SERVERS:
        fqdn = f"{server_name}.valkey-headless.{NAMESPACE}.svc.cluster.local"
        try:
            if _sock.gethostbyname(fqdn) == addr:
                return server_name
        except _sock.gaierror:
            continue
    return None


def set_primary(new_addr: str, new_port: str):
    """
    Update HAProxy to route traffic to the new primary.
    HAProxy's tcp-check (role:master) acts as a safety net — demoted
    replicas will fail the check and be taken out automatically.
    """
    server_name = get_server_name(new_addr)
    if not server_name:
        log.warning(f"Could not resolve server name for {new_addr}, HAProxy tcp-check will self-correct")
        return

    # Enable the new primary server in case it was marked down
    cmd = f"enable server {BACKEND_NAME}/{server_name}"
    resp = haproxy_cmd(cmd)
    log.info(f"HAProxy enable server {server_name}: {resp or 'ok'}")

    log.info(f"Failover complete: new primary is {server_name} ({new_addr}:{new_port})")


def connect_sentinel():
    """Connect to sentinel with retry."""
    while True:
        try:
            log.info(f"Connecting to sentinel {SENTINEL_HOST}:{SENTINEL_PORT}...")
            r = redis.Redis(
                host=SENTINEL_HOST,
                port=SENTINEL_PORT,
                socket_timeout=5,
                socket_connect_timeout=5,
                decode_responses=True,
            )
            r.ping()
            log.info("Connected to sentinel")
            return r
        except Exception as e:
            log.warning(f"Sentinel connection failed: {e}, retrying in 2s...")
            time.sleep(2)


def wait_for_haproxy_socket():
    """Wait until HAProxy socket is available."""
    log.info(f"Waiting for HAProxy socket at {HAPROXY_SOCK}...")
    while not os.path.exists(HAPROXY_SOCK):
        time.sleep(1)
    log.info("HAProxy socket ready")


def main():
    wait_for_haproxy_socket()

    while True:
        r = connect_sentinel()
        pubsub = r.pubsub()

        try:
            # +switch-master fires when sentinel completes a failover:
            # format: "<master-name> <old-ip> <old-port> <new-ip> <new-port>"
            pubsub.subscribe("+switch-master")
            log.info(f"Subscribed to sentinel pub/sub, watching for +switch-master on '{MASTER_NAME}'")

            for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                channel = message["channel"]
                data    = message["data"]

                log.info(f"Sentinel event [{channel}]: {data}")

                if channel == "+switch-master":
                    # data: "mymaster <old-addr> <old-port> <new-addr> <new-port>"
                    # addr is a FQDN when announce-hostnames yes is set.
                    parts = data.split()
                    if len(parts) == 5 and parts[0] == MASTER_NAME:
                        old_addr, old_port, new_addr, new_port = parts[1], parts[2], parts[3], parts[4]
                        log.info(f"+switch-master: {old_addr}:{old_port} -> {new_addr}:{new_port}")
                        set_primary(new_addr, new_port)

        except Exception as e:
            log.error(f"Sentinel pub/sub error: {e}, reconnecting...")
            try:
                pubsub.unsubscribe()
            except Exception:
                pass
            time.sleep(2)


if __name__ == "__main__":
    main()
