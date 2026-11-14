# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private in-memory SSH FTPS worker and local file/tunnel services."""

import inspect
import json
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path

from .common import tls_context
from .config import validate_via
from .errors import CliError
from .ftps import ImplicitFTPS, open_ftps
from .relay import SshTunnel, ssh_arguments
from .safety import Authorization, remote_filename
from .tunnel import BambuTunnelLocal, find_verified_model


def validate_path(path):
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or ".." in path.split("/")
        or any(ord(c) < 32 for c in path)
    ):
        raise CliError("目录须为无控制字符的绝对路径")
    return path


def ftps_operation(config, timeout, operation, path, data=None, authorization=None):
    if operation != "ls":
        if operation != "upload" or authorization is None:
            raise CliError("FTPS 写操作缺少授权")
        authorization.check(config)
    validate_path(path)
    with open_ftps(config, timeout) as ftp:
        if operation == "ls":
            return {"ftps": "ok", "entries": ftp.root_list(path)}
        if operation != "upload":
            raise CliError("不允许的 FTPS 操作")
        import io

        # Check exact directory entry before STOR; never silently overwrite.
        name = path.rsplit("/", 1)[-1]
        names = [item.rsplit("/", 1)[-1] for item in ftp.nlst("/")]
        if name in names:
            raise CliError("远程文件已存在，拒绝覆盖")
        ftp.storbinary("STOR " + path, io.BytesIO(data), blocksize=65536)
        size = ftp.size(path)
        if size != len(data):
            raise CliError("FTPS 大小校验失败")
        return {"ftps": "ok", "bytes": size, "verified": True}


def remote_worker():
    import resource
    import signal
    import sys
    from types import SimpleNamespace

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.alarm(180)
    length = int(sys.stdin.buffer.readline())
    if not 0 < length <= 65536:
        raise ValueError
    values = json.loads(sys.stdin.buffer.read(length))
    config = SimpleNamespace(
        host=values["host"],
        access_code=values["access_code"],
        serial=values.get("serial", ""),
    )
    size = values.get("size", 0)
    if type(size) is not int or not 0 <= size <= 200 * 1024 * 1024:
        raise ValueError
    data = sys.stdin.buffer.read(size)
    if len(data) != size:
        raise ValueError
    try:
        result = ftps_operation(
            config,
            values["timeout"],
            values["operation"],
            values["path"],
            data,
            Authorization(values.get("execute", False), values.get("confirm_serial")),
        )
        reply = {"ok": True, "result": result}
    except Exception:
        reply = {"ok": False}
    print(json.dumps(reply).replace(config.access_code, "[REDACTED]"))


def ssh_ftps(config, args, operation, path, data=None, authorization=None):
    if operation != "ls":
        if operation != "upload" or authorization is None:
            raise CliError("SSH FTPS 写操作缺少授权")
        authorization.check(config)
    validate_via(args.via)
    source = "\n".join(
        [
            "import ftplib, json, re, socket, ssl, sys\nfrom dataclasses import dataclass, field",
            *(
                inspect.getsource(obj)
                for obj in (
                    CliError,
                    Authorization,
                    tls_context,
                    ImplicitFTPS,
                    open_ftps,
                    validate_path,
                    ftps_operation,
                    remote_worker,
                )
            ),
            "remote_worker()",
        ]
    ).encode()
    bootstrap = "import resource,signal,sys; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); signal.alarm(180); n=int(sys.stdin.buffer.readline()); exec(compile(sys.stdin.buffer.read(n),'<stdin>','exec'))"
    values = json.dumps(
        {
            "serial": config.serial,
            "execute": authorization.execute if authorization else False,
            "confirm_serial": authorization.serial if authorization else None,
            "host": config.host,
            "access_code": config.access_code,
            "timeout": args.timeout,
            "operation": operation,
            "path": path,
            "size": len(data or b""),
        }
    ).encode()
    stdin = (
        str(len(source)).encode()
        + b"\n"
        + source
        + str(len(values)).encode()
        + b"\n"
        + values
        + (data or b"")
    )
    try:
        result = subprocess.run(
            ssh_arguments() + [args.via, "python3 -I -c " + shlex.quote(bootstrap)],
            input=stdin,
            capture_output=True,
            timeout=min(args.timeout * 8 + 30, 190),
            check=False,
        )
        if result.returncode:
            raise ValueError
        reply = json.loads(result.stdout)
        if reply.get("ok") is not True or not isinstance(reply.get("result"), dict):
            raise ValueError
        return reply["result"]
    except (ValueError, subprocess.TimeoutExpired):
        raise CliError(
            "SSH FTPS 失败或超时；检查 U 盘，P2S 内置存储可使用 --internal"
        ) from None


@contextmanager
def internal_connection(config, args):
    if args.via:
        with SshTunnel(config, args.via, args.timeout, target_port=6000) as relay:
            with BambuTunnelLocal(
                config, args.timeout, host="127.0.0.1", port=relay.port
            ) as tunnel:
                yield tunnel
    else:
        with BambuTunnelLocal(config, args.timeout) as tunnel:
            yield tunnel


def list_files(config, args):
    if args.internal:
        with internal_connection(config, args) as tunnel:
            return {
                "transport": "TCP6000",
                "entries": tunnel.list_models(kind=args.kind),
            }
    path = validate_path(args.path)
    try:
        return (
            ssh_ftps(config, args, "ls", path)
            if args.via
            else ftps_operation(config, args.timeout, "ls", path)
        )
    except (OSError, __import__("ftplib").Error):
        raise CliError(
            "USB FTPS 不可用；检查 U 盘是否插入。P2S 内置存储请使用 ls --internal"
        ) from None


def upload_file(config, args, authorization):
    authorization.check(config)
    source = Path(args.source)
    name = remote_filename(args.name or source.name)
    if not source.is_file() or not 0 < source.stat().st_size <= 200 * 1024 * 1024:
        raise CliError("源文件不存在或大小须在 1 字节到 200 MiB 之间")
    if args.internal:
        with internal_connection(config, args) as tunnel:
            result = tunnel.upload(source, name, authorization)
        with internal_connection(config, args) as tunnel:
            find_verified_model(tunnel.list_models(), name, source.stat().st_size)
        return {
            "transport": "TCP6000",
            "bytes": result["bytes"],
            "verified_fresh_connection": True,
        }
    data = source.read_bytes()
    return (
        ssh_ftps(config, args, "upload", "/" + name, data, authorization)
        if args.via
        else ftps_operation(
            config, args.timeout, "upload", "/" + name, data, authorization
        )
    )
