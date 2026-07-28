#!/usr/bin/env python3
"""Drive alternating write/read traffic directly against valkey-endpoint.

Used by demo.sh during the failover step to show write and read continuity
while Sentinel elects a new primary. Connects straight to the valkey-endpoint
Service (the same stable address a real client would use) over a self-managed
`kubectl port-forward`, rather than shelling into a pod for every operation.
Each iteration writes a key, then reads it back to verify, waiting --interval
seconds between every operation (not just every iteration). Failures — the
whole point of watching this during a failover — are printed live as they
happen, in addition to being logged.
"""
import argparse
import select
import signal
import socket
import subprocess
import sys
import time

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"

STOP = False


def handle_stop(signum, frame):
    global STOP
    STOP = True


class RespError(Exception):
    pass


def resp_encode(*parts):
    out = f"*{len(parts)}\r\n".encode()
    for part in parts:
        b = part.encode() if isinstance(part, str) else part
        out += f"${len(b)}\r\n".encode() + b + b"\r\n"
    return out


def resp_read_reply(f):
    line = f.readline()
    if not line:
        raise ConnectionError("connection closed by peer")
    line = line.rstrip(b"\r\n")
    kind, rest = line[:1], line[1:]
    if kind == b"+":
        return rest.decode()
    if kind == b"-":
        raise RespError(rest.decode())
    if kind == b":":
        return int(rest)
    if kind == b"$":
        n = int(rest)
        if n == -1:
            return None
        data = f.read(n + 2)
        if len(data) < n:
            raise ConnectionError("connection closed mid-reply")
        return data[:n].decode()
    if kind == b"*":
        n = int(rest)
        if n == -1:
            return None
        return [resp_read_reply(f) for _ in range(n)]
    raise RespError(f"unexpected reply type: {line!r}")


class ValkeyConn:
    def __init__(self, host, port, timeout=3):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.f = None

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.f = self.sock.makefile("rb")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.f = None

    def command(self, *parts):
        if self.sock is None:
            self.connect()
        self.sock.sendall(resp_encode(*parts))
        return resp_read_reply(self.f)


def free_local_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_port_forward(namespace, local_port, remote_port, ready_timeout=15):
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, "service/valkey-endpoint",
         f"{local_port}:{remote_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("kubectl port-forward exited before becoming ready")
        ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        if ready:
            line = proc.stdout.readline()
            if "Forwarding from" in line:
                return proc
    proc.terminate()
    raise RuntimeError("timed out waiting for kubectl port-forward to become ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--remote-port", type=int, default=6379)
    parser.add_argument("--interval", type=float, default=0.5,
                         help="seconds to wait between each operation")
    parser.add_argument("--log", required=True, help="path to append result lines to")
    parser.add_argument("--key-prefix", default="write")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    local_port = free_local_port()
    print(f"  Forwarding 127.0.0.1:{local_port} -> service/valkey-endpoint:{args.remote_port}",
          flush=True)
    forward = start_port_forward(args.namespace, local_port, args.remote_port)
    conn = ValkeyConn("127.0.0.1", local_port)

    op_index = 0
    try:
        with open(args.log, "a", buffering=1) as log:
            while not STOP:
                n = op_index // 2
                key = f"{args.key_prefix}:{n}"
                ts = time.time()

                if op_index % 2 == 0:
                    optype, value = "WRITE", f"val-{n}"
                    try:
                        reply = conn.command("SET", key, value)
                        ok, detail = reply == "OK", reply
                    except (OSError, RespError) as e:
                        conn.close()
                        ok, detail = False, str(e)
                else:
                    optype, expected = "READ", f"val-{n}"
                    try:
                        reply = conn.command("GET", key)
                        ok, detail = reply == expected, reply
                    except (OSError, RespError) as e:
                        conn.close()
                        ok, detail = False, str(e)

                status = "OK" if ok else "ERR"
                log.write(f"{ts:.3f} {op_index} {optype} {key} {status} {detail}\n")
                if ok:
                    print(f"  {GREEN}✓ {optype} {key} = {detail}{NC}", flush=True)
                else:
                    print(f"  {RED}✗ {optype} {key} failed: {detail}{NC}", flush=True)

                op_index += 1
                time.sleep(args.interval)
    finally:
        conn.close()
        forward.terminate()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()


if __name__ == "__main__":
    main()
