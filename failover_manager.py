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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("failover-manager")

SENTINEL_HOST = os.environ.get("SENTINEL_HOST", "valkey-sentinel")
SENTINEL_PORT = int(os.environ.get("SENTINEL_PORT", "26379"))
MASTER_NAME = os.environ.get("MASTER_NAME", "mymaster")
HAPROXY_SOCK = os.environ.get("HAPROXY_SOCK", "/var/run/haproxy/haproxy.sock")
BACKEND_NAME = os.environ.get("HAPROXY_BACKEND", "valkey_primary")

# Optional Sentinel credentials (issue #3). Leave unset for no auth.
# SENTINEL_USERNAME is only needed for an ACL user; with plain
# `requirepass` in sentinel.conf set just SENTINEL_PASSWORD.
SENTINEL_USERNAME = os.environ.get("SENTINEL_USERNAME") or None
SENTINEL_PASSWORD = os.environ.get("SENTINEL_PASSWORD") or None

# How often to PING sentinel on the otherwise idle pub/sub connection,
# so a dead connection is detected instead of waiting forever (issue #1).
HEALTH_CHECK_INTERVAL = int(os.environ.get("SENTINEL_HEALTH_CHECK_INTERVAL", "15"))


def haproxy_cmd(cmd: str) -> str:
    """Send a command to HAProxy via Unix socket and return the response."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(HAPROXY_SOCK)
        s.sendall((cmd + "\n").encode())
        response = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            response += chunk
    return response.decode().strip()


def haproxy_servers() -> list[dict]:
    """
    Read the backend's servers from HAProxy itself (issue #2), so the
    server list always matches haproxy.cfg - no hard-coded pod names,
    service name or namespace. Parsed by column name from the header of
    `show servers state`, so column additions in newer HAProxy are fine.
    """
    out = haproxy_cmd(f"show servers state {BACKEND_NAME}")
    cols = None
    servers = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("#"):
            cols = line.lstrip("#").split()
        elif cols and line:
            vals = line.split()
            if len(vals) >= len(cols):
                servers.append(dict(zip(cols, vals)))
    if not servers:
        log.warning(f"HAProxy returned no servers for backend {BACKEND_NAME}: {out!r}")
    return servers


def _norm(host: str) -> str:
    return host.rstrip(".").lower()


def _resolve(host: str) -> set[str]:
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return set()


def get_server_name(addr: str, port: str) -> str | None:
    """
    Map a sentinel-reported address to an HAProxy server name.
    Sentinel reports an FQDN with announce-hostnames yes, otherwise an IP.
    Matched in order: server FQDN, server name, current server IP, then
    DNS resolution of either side.
    """
    servers = [
        s for s in haproxy_servers() if s.get("srv_port") in (None, "0", str(port))
    ]
    a = _norm(addr)
    for s in servers:
        fqdn = s.get("srv_fqdn", "-")
        if fqdn != "-" and _norm(fqdn) == a:
            return s["srv_name"]
    for s in servers:
        if _norm(s["srv_name"]) == a:
            return s["srv_name"]
    for s in servers:
        if s.get("srv_addr") == addr:
            return s["srv_name"]
    # Fallback: resolve and compare IPs (e.g. sentinel reports an IP but
    # HAProxy hasn't re-resolved yet, or vice versa).
    addr_ips = _resolve(addr) or {addr}
    for s in servers:
        fqdn = s.get("srv_fqdn", "-")
        if fqdn != "-" and addr_ips & _resolve(fqdn):
            return s["srv_name"]
    return None


def set_primary(new_addr: str, new_port: str):
    """
    Update HAProxy to route traffic to the new primary.
    HAProxy's tcp-check (role:master) acts as a safety net — demoted
    replicas will fail the check and be taken out automatically.
    """
    try:
        server_name = get_server_name(new_addr, new_port)
    except OSError as e:
        log.warning(
            f"HAProxy socket error while resolving {new_addr}: {e}, tcp-check will self-correct"
        )
        return
    if not server_name:
        log.warning(
            f"Could not resolve server name for {new_addr}, HAProxy tcp-check will self-correct"
        )
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
            log.info(
                f"Connecting to sentinel {SENTINEL_HOST}:{SENTINEL_PORT}"
                f"{' (auth)' if SENTINEL_PASSWORD else ''}..."
            )
            r = redis.Redis(
                host=SENTINEL_HOST,
                port=SENTINEL_PORT,
                username=SENTINEL_USERNAME,
                password=SENTINEL_PASSWORD,
                # socket_timeout only bounds individual replies now; the
                # pub/sub loop below polls with its own short timeout, so
                # an idle channel no longer raises "Timeout reading from
                # socket" (issue #1).
                socket_timeout=10,
                socket_connect_timeout=5,
                socket_keepalive=True,
                health_check_interval=HEALTH_CHECK_INTERVAL,
                decode_responses=True,
            )
            r.ping()
            log.info("Connected to sentinel")
            return r
        except Exception as e:
            # redis-py reports a bad password as AuthenticationError or,
            # on some versions, as a WRONGPASS/NOAUTH ResponseError.
            if isinstance(e, redis.AuthenticationError) or str(e).startswith(
                ("WRONGPASS", "NOAUTH")
            ):
                log.error(
                    f"Sentinel authentication failed: {e} (check SENTINEL_USERNAME/SENTINEL_PASSWORD), retrying in 5s..."
                )
                time.sleep(5)
            else:
                log.warning(f"Sentinel connection failed: {e}, retrying in 2s...")
                time.sleep(2)


def wait_for_haproxy_socket():
    """Wait until HAProxy socket is available."""
    log.info(f"Waiting for HAProxy socket at {HAPROXY_SOCK}...")
    while not os.path.exists(HAPROXY_SOCK):
        time.sleep(1)
    log.info("HAProxy socket ready")


def reconcile(r):
    """
    Point HAProxy at whatever sentinel currently thinks is primary.
    Run after every (re)subscribe so a +switch-master published while we
    were disconnected is not lost.
    """
    try:
        addr = r.sentinel_get_master_addr_by_name(MASTER_NAME)
    except Exception as e:
        log.warning(f"Could not query current primary for '{MASTER_NAME}': {e}")
        return
    if addr:
        log.info(f"Current primary per sentinel: {addr[0]}:{addr[1]}")
        set_primary(addr[0], str(addr[1]))


def main():
    wait_for_haproxy_socket()

    while True:
        r = connect_sentinel()
        pubsub = r.pubsub(ignore_subscribe_messages=True)

        try:
            # +switch-master fires when sentinel completes a failover:
            # format: "<master-name> <old-ip> <old-port> <new-ip> <new-port>"
            pubsub.subscribe("+switch-master")
            log.info(
                f"Subscribed to sentinel pub/sub, watching for +switch-master on '{MASTER_NAME}'"
            )
            # Subscribe first, then reconcile, so nothing falls in between.
            reconcile(r)

            while True:
                # Poll with a short timeout instead of a blocking listen():
                # returns None when idle, and lets redis-py send its
                # periodic health-check PING on this connection.
                message = pubsub.get_message(timeout=1.0)
                if not message or message["type"] != "message":
                    continue

                channel = message["channel"]
                data = message["data"]

                log.info(f"Sentinel event [{channel}]: {data}")

                if channel == "+switch-master":
                    # data: "mymaster <old-addr> <old-port> <new-addr> <new-port>"
                    # addr is a FQDN when announce-hostnames yes is set.
                    parts = data.split()
                    if len(parts) == 5 and parts[0] == MASTER_NAME:
                        old_addr, old_port, new_addr, new_port = (
                            parts[1],
                            parts[2],
                            parts[3],
                            parts[4],
                        )
                        log.info(
                            f"+switch-master: {old_addr}:{old_port} -> {new_addr}:{new_port}"
                        )
                        set_primary(new_addr, new_port)

        except Exception as e:
            log.error(f"Sentinel pub/sub error: {e}, reconnecting...")
            try:
                pubsub.close()
            except Exception:
                pass
            time.sleep(2)


if __name__ == "__main__":
    main()
