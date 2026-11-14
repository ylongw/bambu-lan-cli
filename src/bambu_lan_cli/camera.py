# SPDX-License-Identifier: AGPL-3.0-or-later
"""LAN camera snapshots: HA/OpenBambuAPI JPEG framing and RTSPS via PyAV.

Credentials are used only in memory; neither URLs nor decoder errors are emitted.
TCP 6000 is a camera only on A1/P1, a file tunnel on P2/H2.
"""

import socket
import struct
from contextlib import contextmanager
from urllib.parse import quote

from .common import tls_context
from .errors import CliError
from .files import save_download
from .relay import SshTunnel


@contextmanager
def camera_endpoint(config, args, port):
    if args.via:
        with SshTunnel(config, args.via, args.timeout, target_port=port) as relay:
            yield "127.0.0.1", relay.port
    else:
        yield config.host, port


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise CliError("相机连接已关闭")
        data.extend(chunk)
    return bytes(data)


def jpeg_snapshot(config, args):
    with camera_endpoint(config, args, 6000) as (host, port):
        with socket.create_connection((host, port), args.timeout) as raw:
            with tls_context().wrap_socket(raw, server_hostname=config.serial) as sock:
                sock.settimeout(args.timeout)
                code = config.access_code.encode("ascii")
                if len(code) > 32:
                    raise CliError("相机凭证长度无效")
                sock.sendall(
                    struct.pack("<IIII", 0x40, 0x3000, 0, 0)
                    + b"bblp".ljust(32, b"\0")
                    + code.ljust(32, b"\0")
                )
                size, _, _, _ = struct.unpack("<IIII", read_exact(sock, 16))
                if not 4 <= size <= 16 * 1024 * 1024:
                    raise CliError("相机 JPEG 帧大小无效")
                data = read_exact(sock, size)
                if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                    raise CliError("相机 JPEG 帧无效")
                return data


def rtsp_snapshot(config, args):
    try:
        import av
    except ImportError:
        raise CliError(
            'RTSPS 截图需要安装可选依赖：pip install -e ".[camera]"'
        ) from None
    with camera_endpoint(config, args, 322) as (host, port):
        url = f"rtsps://bblp:{quote(config.access_code, safe='')}@{host}:{port}/streaming/live/1"
        try:
            # The URL is never passed as a process argument or returned in errors.
            with av.logging.Capture(local=False):
                with av.open(
                    url,
                    options={"rtsp_transport": "tcp"},
                    timeout=(args.timeout, args.timeout),
                ) as container:
                    for frame in container.decode(video=0):
                        import io

                        output = io.BytesIO()
                        frame.to_image().save(output, format="JPEG")
                        return output.getvalue()
        except Exception:
            raise CliError("RTSPS 截图失败；检查本地 Liveview 开关与相机依赖") from None
    raise CliError("相机未返回图像")


def snapshot(config, args):
    transport = args.transport
    if transport == "auto":
        model = config.model.lower().replace("_", "").replace(" ", "")
        transport = "jpeg" if model.startswith(("a1", "p1")) else "rtsp"

    def produce(sink):
        data = (
            jpeg_snapshot(config, args)
            if transport == "jpeg"
            else rtsp_snapshot(config, args)
        )
        sink(data)
        return {"bytes": len(data), "transport": transport, "format": "JPEG"}

    return save_download(args.destination, produce)
