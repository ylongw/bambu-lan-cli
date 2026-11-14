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
from .ftps import ImplicitFTPS, list_external, open_ftps
from .relay import SshTunnel, ssh_arguments
from .safety import Authorization, remote_filename
from .tunnel import BambuTunnelLocal, TunnelError, find_verified_model


def validate_path(path):
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or ".." in path.split("/")
        or any(ord(c) < 32 for c in path)
    ):
        raise CliError("目录须为无控制字符的绝对路径")
    return "/" + "/".join(part for part in path.split("/") if part and part != ".")


def ftps_operation(
    config, timeout, operation, path, data=None, authorization=None, list_timeout=30
):
    if operation not in ("ls", "download", "file-info"):
        if (
            operation not in ("upload", "delete", "rename", "mkdir", "rmdir")
            or authorization is None
        ):
            raise CliError("FTPS 写操作缺少授权")
        authorization.check(config)
    path = validate_path(path)
    if operation == "ls":
        return list_external(config, timeout, path, list_timeout)
    with open_ftps(config, timeout) as ftp:
        if operation == "file-info":
            ftp.voidcmd("TYPE I")
            return {"size": ftp.size(path)}
        if operation in ("mkdir", "rmdir"):
            if path == "/":
                raise CliError("拒绝修改根目录")
            (ftp.mkd if operation == "mkdir" else ftp.rmd)(path)
            return {"operation": operation, "acknowledged": True}
        if operation == "delete":
            ftp.delete(path)
            return {"deleted": True, "acknowledged": True}
        if operation == "rename":
            data = validate_path(data)
            names = [
                name.rsplit("/", 1)[-1]
                for name in ftp.nlst(data.rsplit("/", 1)[0] or "/")
            ]
            if data.rsplit("/", 1)[-1] in names:
                raise CliError("目标已存在，拒绝覆盖")
            ftp.rename(path, data)
            return {"renamed": True, "acknowledged": True}
        if operation == "download":
            import base64
            import hashlib
            import io

            ftp.voidcmd("TYPE I")
            expected = ftp.size(path)
            if expected is not None and expected > 256 * 1024 * 1024:
                raise CliError("SSH FTPS 下载超过 256 MiB")
            target = io.BytesIO()

            def receive(chunk):
                if target.tell() + len(chunk) > 256 * 1024 * 1024:
                    raise CliError("SSH FTPS 下载超过 256 MiB；请使用直连或 TCP 6000")
                target.write(chunk)

            ftp.retrbinary("RETR " + path, receive, blocksize=65536)
            data = target.getvalue()
            if expected is not None and expected != len(data):
                raise CliError("FTPS 下载大小不一致")
            return {
                "data_b64": base64.b64encode(data).decode(),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
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
    signal.alarm(300)
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
    if values["operation"] == "rename":
        data = data.decode("utf-8")
    try:
        result = ftps_operation(
            config,
            values["timeout"],
            values["operation"],
            values["path"],
            data,
            Authorization(values.get("execute", False), values.get("confirm_serial")),
            values.get("list_timeout", 30),
        )
        reply = {"ok": True, "result": result}
    except CliError as exc:
        reply = (
            {"ok": False, "list_error": str(exc)}
            if values["operation"] == "ls"
            else {"ok": False}
        )
    except Exception:
        reply = {"ok": False}
    encoded = json.dumps(reply)
    # Download bytes remain exact inside the encrypted, captured pipe. Never
    # replace substrings in base64; a coincidental match would corrupt files.
    print(
        encoded
        if values["operation"] == "download"
        else encoded.replace(config.access_code, "[REDACTED]")
    )


def ssh_ftps(config, args, operation, path, data=None, authorization=None):
    if operation not in ("ls", "download", "file-info"):
        if (
            operation not in ("upload", "delete", "rename", "mkdir", "rmdir")
            or authorization is None
        ):
            raise CliError("SSH FTPS 写操作缺少授权")
        authorization.check(config)
    if isinstance(data, str):
        data = data.encode("utf-8")
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
                    list_external,
                    validate_path,
                    ftps_operation,
                    remote_worker,
                )
            ),
            "remote_worker()",
        ]
    ).encode()
    bootstrap = "import resource,signal,sys; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); signal.alarm(300); n=int(sys.stdin.buffer.readline()); exec(compile(sys.stdin.buffer.read(n),'<stdin>','exec'))"
    values = json.dumps(
        {
            "serial": config.serial,
            "execute": authorization.execute if authorization else False,
            "confirm_serial": authorization.serial if authorization else None,
            "host": config.host,
            "access_code": config.access_code,
            "timeout": args.timeout,
            "list_timeout": getattr(args, "list_timeout", 30),
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
            timeout=min(args.timeout * 8 + getattr(args, "list_timeout", 30) + 30, 400),
            check=False,
        )
        if result.returncode:
            raise ValueError
        reply = json.loads(result.stdout)
        if (
            operation == "ls"
            and reply.get("ok") is False
            and isinstance(reply.get("list_error"), str)
        ):
            raise CliError(reply["list_error"])
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
    path = validate_path(
        "/timelapse" if args.kind == "timelapse" and args.path == "/" else args.path
    )
    list_timeout = getattr(args, "list_timeout", 30)
    if not 0 < list_timeout <= 180:
        raise CliError("list-timeout 须在 0..180 秒之间（不含 0）")
    return (
        ssh_ftps(config, args, "ls", path)
        if args.via
        else ftps_operation(config, args.timeout, "ls", path, list_timeout=list_timeout)
    )


def upload_file(config, args, authorization):
    authorization.check(config)
    source = Path(args.source)
    name = remote_filename(args.name or source.name)
    if not source.is_file() or not 0 < source.stat().st_size <= 200 * 1024 * 1024:
        raise CliError("源文件不存在或大小须在 1 字节到 200 MiB 之间")
    if args.internal:
        with internal_connection(config, args) as tunnel:
            result = tunnel.upload(source, name, authorization)
        failures = []
        for attempt in range(2):
            try:
                with internal_connection(config, args) as tunnel:
                    find_verified_model(tunnel.list_models(), name, result["bytes"])
                break
            except (TunnelError, OSError, CliError) as exc:
                # Only retry the independent read, never FILE_UPLOAD itself.
                if isinstance(exc, CliError):
                    reason = str(exc)
                elif isinstance(exc, TimeoutError):
                    reason = "TCP 6000 校验连接或 LIST 超时"
                else:
                    reason = f"TCP 6000 校验网络失败：{type(exc).__name__}, errno={exc.errno}"
                failures.append(f"第 {attempt + 1} 次：{reason}")
        else:
            raise CliError(
                "上传已收到完成应答，但大小校验失败；不能确认成功；"
                + "；".join(failures)
                + "；可用 ls --internal 只读复核，勿盲目重新上传"
            ) from None
        return {
            "transport": "TCP6000",
            "bytes": result["bytes"],
            "verified_fresh_connection": True,
            "verification_attempts": attempt + 1,
            "verification_warnings": failures,
        }
    data = source.read_bytes()
    return (
        ssh_ftps(config, args, "upload", "/" + name, data, authorization)
        if args.via
        else ftps_operation(
            config, args.timeout, "upload", "/" + name, data, authorization
        )
    )


def save_download(destination, producer):
    """0600 temporary file, exclusive publish, no partial or overwritten result."""
    import os
    import tempfile

    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise CliError("本地目标已存在，拒绝覆盖")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".bambu-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            result = producer(stream.write)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_file(config, args):
    import base64
    import hashlib

    if not 1 <= args.max_mib <= 16384:
        raise CliError("max-mib 须在 1..16384 之间")
    maximum = args.max_mib * 1024 * 1024

    def produce(sink):
        if args.internal:
            with internal_connection(config, args) as tunnel:
                if getattr(args, "member", None):
                    return tunnel.archive_member(
                        args.remote_path, args.member, sink, maximum, args.kind
                    )
                return tunnel.download(args.remote_path, args.kind, sink, maximum)
        path = validate_path(args.remote_path)
        if args.via:
            reply = ssh_ftps(config, args, "download", path)
            data = base64.b64decode(reply["data_b64"], validate=True)
            if hashlib.sha256(data).hexdigest() != reply.get("sha256"):
                raise CliError("SSH FTPS 下载校验失败")
            if len(data) > maximum:
                raise CliError("下载超过大小上限")
            sink(data)
            return {"bytes": len(data), "transport": "SSH FTPS"}
        count = 0

        def receive(chunk):
            nonlocal count
            count += len(chunk)
            if count > maximum:
                raise CliError("下载超过大小上限")
            sink(chunk)

        with open_ftps(config, args.timeout) as ftp:
            ftp.voidcmd("TYPE I")
            size = ftp.size(path)
            if size is not None and size > maximum:
                raise CliError("下载文件超过大小上限")
            ftp.retrbinary("RETR " + path, receive, blocksize=65536)
            if size is not None and size != count:
                raise CliError("FTPS 下载大小不一致")
        return {"bytes": count, "transport": "FTPS"}

    return save_download(args.destination, produce)


def delete_file(config, args, authorization):
    authorization.check(config)
    if args.internal:
        with internal_connection(config, args) as tunnel:
            return tunnel.delete(args.remote_path, args.kind, authorization)
    path = validate_path(args.remote_path)
    if args.via:
        return ssh_ftps(config, args, "delete", path, authorization=authorization)
    return ftps_operation(
        config, args.timeout, "delete", path, authorization=authorization
    )


def rename_file(config, args, authorization):
    authorization.check(config)
    if args.internal:
        raise CliError("TCP 6000 上游未确认重命名协议；只支持 FTPS")
    path, new_path = validate_path(args.remote_path), validate_path(args.new_path)
    if args.via:
        return ssh_ftps(config, args, "rename", path, new_path, authorization)
    return ftps_operation(config, args.timeout, "rename", path, new_path, authorization)


def directory_operation(config, args, authorization):
    authorization.check(config)
    path = validate_path(args.remote_path)
    if args.via:
        return ssh_ftps(config, args, args.command, path, authorization=authorization)
    return ftps_operation(
        config, args.timeout, args.command, path, authorization=authorization
    )


def file_info(config, args):
    path = validate_path(args.remote_path)
    if args.via:
        return ssh_ftps(config, args, "file-info", path)
    return ftps_operation(config, args.timeout, "file-info", path)
