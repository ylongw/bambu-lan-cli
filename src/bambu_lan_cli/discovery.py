# SPDX-License-Identifier: AGPL-3.0-or-later
# SSDP design referenced from bpm/bambudiscovery.py (WTFPL-2.0).
import socket
import time


def discover(timeout, config=None):
    msg = (
        b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nMAN: "ssdp:discover"\r\n'
        b"MX: 1\r\nST: urn:bambulab-com:device:3dprinter:1\r\n\r\n"
    )
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", 2021))
        sock.settimeout(0.3)
        sock.sendto(msg, ("239.255.255.250", 1990))
        if config:
            sock.sendto(msg, (config.host, 2021))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            headers = {}
            for line in data.decode(errors="replace").split("\r\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower().strip()] = value.strip()
            if "usn" in headers:
                # Deliberately never output unknown devices' identities or names.
                found[addr[0]] = {
                    "model": headers.get("devmodel.bambu.com"),
                    "firmware": headers.get("devversion.bambu.com"),
                    "identity": "[REDACTED]",
                }
    return {
        "command": "discover",
        "count": len(found),
        "printers": list(found.values()),
        "scope": "local LAN; SSH discovery is not supported",
    }
