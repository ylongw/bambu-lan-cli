# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal P2S LAN file upload/list client; never sends printer control commands.

Protocol reference: ClusterM/open-bamboo-networking (AGPL-3.0), commit
77de55a8c696c53125ca297bdf3fdf1258772335, research/06.04-port-6000.md
and src/tunnel_local.cpp, src/tunnel_upload.cpp. Independently written Python
implementation, covering RPC command types 1–5 and 7.
See LICENSE and THIRD_PARTY_NOTICES.md for license and attribution.
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import struct
import time
import uuid
from pathlib import Path


class TunnelError(Exception):
    """Only locally generated, credential-free error messages are allowed."""


class BambuTunnelLocal:
    PORT = 6000
    LOGIN_TX, LOGIN_RX = 0x0101013F, 0x0001013F
    CTRL_TX, CTRL_RX = 0x0102013F, 0x0002013F
    MAX_FRAME = 8 * 1024 * 1024

    def __init__(self, config, timeout=15, *, total_timeout=180, host=None, port=6000):
        self.host, self.port = host or config.host, port
        self.config = config
        self.timeout = timeout
        self.deadline = time.monotonic() + total_timeout
        self.sock = None
        self.frame_sequence = 0
        self.command_sequence = 0
        self._authorization = None
        self.audit = []  # Numeric metadata only; no raw frames or auth payloads.

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TunnelError("TCP 6000 操作超过总时限")
        return min(self.timeout, remaining)

    def __enter__(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Same self-signed LAN-TLS policy as the existing CLI. Use only trusted LAN.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.host, self.port), self._remaining())
        try:
            self.sock = context.wrap_socket(raw, server_hostname=self.config.serial)
            try:
                code = self.config.access_code.encode("ascii")
            except UnicodeError:
                raise TunnelError("凭证失败") from None
            if len(code) != 8:
                raise TunnelError("凭证失败")
            self._send(self.LOGIN_TX, b"bblp\0\0\0\0" + code)
            magic, payload = self._receive()
            if magic != self.LOGIN_RX:
                raise TunnelError("凭证失败")
            # Observed login acknowledgment is a 4-byte little-endian result.
            if len(payload) != 4 or struct.unpack("<I", payload)[0] != 0:
                raise TunnelError("凭证失败")
            self._send_json(
                {
                    "sequence": 0,
                    "mtype": 12291,
                    "req": {
                        "t_av": 1,
                        "mtype": 12289,
                        "peer_t": 3,
                        "pid": uuid.uuid4().hex[:8],
                        "ver": "",
                    },
                }
            )
            response = self._json_receive()
            if response.get("mtype") != 12291 or response.get("result") != 0:
                raise TunnelError("TCP 6000 setup 失败")
            self.audit.append({"stage": "authentication_setup", "result": 0})
            return self
        except BaseException:
            raw.close()
            self.close()
            raise

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __exit__(self, *args):
        self.close()

    def _send(self, magic, payload):
        self.sock.settimeout(self._remaining())
        header = struct.pack("<IIII", len(payload), magic, self.frame_sequence, 0)
        self.frame_sequence += 1
        self.sock.sendall(header)
        self.sock.sendall(payload)

    def _send_json(self, message, binary=b""):
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if binary:
            payload += b"\n\n" + binary
        self._send(self.CTRL_TX, payload)

    def _read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            self.sock.settimeout(self._remaining())
            part = self.sock.recv(count - len(data))
            if not part:
                raise TunnelError("TCP 6000 连接已关闭")
            data.extend(part)
        return bytes(data)

    def _receive(self):
        size, magic, _, _ = struct.unpack("<IIII", self._read_exact(16))
        if size > self.MAX_FRAME:
            raise TunnelError("TCP 6000 响应帧超出上限")
        if magic not in (self.LOGIN_RX, self.CTRL_RX, 0x0000013F):
            raise TunnelError("TCP 6000 响应帧类型无效")
        return magic, self._read_exact(size)

    def _packet_receive(self):
        for _ in range(64):
            magic, payload = self._receive()
            if magic == 0x0000013F:
                continue
            if magic != self.CTRL_RX:
                raise TunnelError("TCP 6000 非预期响应类型")
            try:
                text, _, binary = payload.partition(b"\n\n")
                message = json.loads(text)
            except (ValueError, UnicodeError):
                raise TunnelError("TCP 6000 JSON 响应无效") from None
            if not isinstance(message, dict):
                raise TunnelError("TCP 6000 JSON 响应无效")
            return message, binary
        raise TunnelError("TCP 6000 非预期响应过多")

    def _json_receive(self):
        message, binary = self._packet_receive()
        if binary:
            raise TunnelError("TCP 6000 非预期二进制数据")
        return message

    def _reply(self, command, sequence):
        for _ in range(128):
            response = self._json_receive()
            if (
                response.get("cmdtype") == command
                and response.get("sequence") == sequence
            ):
                if type(response.get("result")) is not int:
                    raise TunnelError("TCP 6000 响应缺少结果码")
                self.audit.append(
                    {
                        "cmdtype": command,
                        "sequence": sequence,
                        "result": response["result"],
                    }
                )
                return response
        raise TunnelError("TCP 6000 未收到匹配的命令响应")

    def _request(self, command, request):
        # No arbitrary RPCs; archive reads require explicit paths.
        if command not in (1, 2, 3, 4, 5, 7) or (
            command == 2 and not request.get("paths")
        ):
            raise TunnelError("不允许的 TCP 6000 命令")
        if command in (3, 5):
            if self._authorization is None:
                raise TunnelError("写入缺少授权")
            self._authorization.check(self.config)
        self.command_sequence += 1
        sequence = self.command_sequence
        self._send_json(
            {"mtype": 12289, "cmdtype": command, "sequence": sequence, "req": request}
        )
        return sequence, self._reply(command, sequence)

    def media_ability(self):
        _, response = self._request(
            7, {"api_version": 3, "peer": "studio", "peer_t": 3}
        )
        if response["result"] != 0:
            raise TunnelError(
                "REQUEST_MEDIA_ABILITY 失败，result=" + str(response["result"])
            )
        reply = response.get("reply")
        if not isinstance(reply, dict):
            raise TunnelError("REQUEST_MEDIA_ABILITY 响应结构无效")
        return reply

    def list_models(self, storage=None, kind="model"):
        storage = (
            storage
            if storage is not None
            else ("emmc" if kind == "model" else "internal")
        )
        if kind not in ("model", "timelapse", "video"):
            raise TunnelError("不支持的列表类型")
        sequence, response = self._request(
            1, {"type": kind, "api_version": 2, "notify": "DETAIL", "storage": storage}
        )
        entries = []
        for _ in range(128):
            if response["result"] not in (0, 1):
                raise TunnelError("LIST_INFO 失败，result=" + str(response["result"]))
            files = response.get("reply", {}).get("file_lists")
            if not isinstance(files, list) or any(
                not isinstance(f, dict) for f in files
            ):
                raise TunnelError("LIST_INFO 响应结构无效")
            entries.extend(files)
            if response["result"] == 0:
                return entries
            response = self._reply(1, sequence)
        raise TunnelError("LIST_INFO 响应数量超过上限")

    def resolve_file(self, path, kind="timelapse"):
        from .files import validate_path

        matches = [
            entry
            for entry in self.list_models(kind=kind)
            if entry.get("path") == path
            or entry.get("name") == path
            or str(entry.get("path", "")).rsplit("/", 1)[-1] == path
        ]
        if len(matches) != 1:
            raise TunnelError("列表中未找到唯一匹配文件；请使用 ls 返回的 path")
        entry = matches[0]
        full = validate_path(entry.get("path"))
        if not full.startswith(
            ("/userdata/media/", "/userdata/model/", "/media/", "/sdcard/")
        ):
            raise TunnelError("列表返回了不支持的媒体路径")
        return {**entry, "path": full}

    def delete(self, path, kind, authorization):
        authorization.check(self.config)
        self._authorization = authorization
        entry = self.resolve_file(path, kind)
        _, response = self._request(3, {"paths": [entry["path"]]})
        if response["result"] != 0:
            raise TunnelError("FILE_DEL 被设备拒绝")
        # A successful ACK is distinct from verified removal.
        remaining = self.list_models(kind=kind)
        if any(item.get("path") == entry["path"] for item in remaining):
            raise TunnelError("FILE_DEL 应答成功，但文件仍在列表；未重试")
        return {"deleted": True, "verified_by_listing": True}

    def download(self, path, kind, sink, max_bytes):
        entry = self.resolve_file(path, kind)
        return self._stream_file(
            4,
            {"path": entry["path"], "offset": 0},
            sink,
            max_bytes,
            expected_size=entry.get("size"),
        )

    def archive_member(self, path, member, sink, max_bytes, kind="model"):
        from .files import validate_path

        entry = self.resolve_file(path, kind)
        validate_path("/" + member)
        return self._stream_file(
            2, {"paths": [entry["path"] + "#" + member]}, sink, max_bytes
        )

    def project_member(self, member, sink):
        from .files import validate_path

        validate_path("/" + member)
        parameter = json.dumps(
            {
                "sequence_id": 1,
                "version": 1,
                "peer_host": "studio",
                "command": "get_project_file",
                "file_rel_path": member,
            }
        ).encode()
        return self._stream_file(
            4,
            {"path": "mem:/16", "offset": 0, "mem_dl_param_size": len(parameter)},
            sink,
            8 * 1024 * 1024,
            initial_binary=parameter,
        )

    def _stream_file(
        self, command, request, sink, max_bytes, expected_size=None, initial_binary=b""
    ):
        if command not in (2, 4):
            raise TunnelError("不允许的流读取命令")
        if type(expected_size) is int and expected_size > max_bytes:
            raise TunnelError("下载文件超过大小上限")
        self.command_sequence += 1
        sequence = self.command_sequence
        self._send_json(
            {"mtype": 12289, "cmdtype": command, "sequence": sequence, "req": request},
            initial_binary,
        )
        digest = hashlib.md5()
        size = 0
        for _ in range(100000):
            reply, binary = self._packet_receive()
            if reply.get("cmdtype") != command or reply.get("sequence") != sequence:
                continue
            if type(reply.get("result")) is not int or reply["result"] not in (0, 1):
                raise TunnelError("下载被设备拒绝；该固件可能禁止内部模型读取")
            info = reply.get("reply", {})
            if info.get("mem_dl_param_size"):
                length = info["mem_dl_param_size"]
                if type(length) is not int or not 0 < length <= len(binary):
                    raise TunnelError("内存文件头长度无效")
                try:
                    header = json.loads(binary[:length])
                except (ValueError, UnicodeError):
                    raise TunnelError("内存文件头无效") from None
                if header.get("err_no") != 0:
                    raise TunnelError("内存文件成员被拒绝")
                binary = binary[length:]
                # size/offset in this frame include the memory header.
                info = {k: v for k, v in info.items() if k not in ("size", "offset")}
            if info.get("offset", size) != size or (
                "size" in info and info["size"] != len(binary)
            ):
                raise TunnelError("下载分块偏移或长度不匹配")
            size += len(binary)
            if size > max_bytes:
                raise TunnelError("下载超过大小上限")
            sink(binary)
            digest.update(binary)
            if reply["result"] == 0:
                if "total" in info and info["total"] != size:
                    raise TunnelError("下载总长度不匹配")
                checksum = info.get("file_md5")
                if command == 4 and (
                    not isinstance(checksum, str)
                    or digest.hexdigest() != checksum.lower()
                ):
                    raise TunnelError("下载 MD5 校验失败或缺失")
                if type(expected_size) is int and expected_size != size:
                    raise TunnelError("下载大小与列表不一致")
                return {"bytes": size, "md5_verified": bool(checksum)}
        raise TunnelError("下载分块数量超限")

    def upload(self, source, name, authorization):
        authorization.check(self.config)
        self._authorization = authorization
        # Refuse overwrite before initiating FILE_UPLOAD. No delete or resume.
        if (
            not name.endswith(".gcode.3mf")
            or Path(name).name != name
            or "/" in name
            or "\\" in name
        ):
            raise TunnelError("内部上传文件名无效")
        data = Path(source).read_bytes()
        if not 0 < len(data) <= 200 * 1024 * 1024:
            raise TunnelError("内部上传文件大小须在 1 字节到 200 MiB 之间")
        ability = self.media_ability()
        if "emmc" not in ability.get("upload_storage", ability.get("storage", [])):
            raise TunnelError("设备未报告 eMMC 上传能力")
        before = self.list_models()
        if any(
            f.get("name") == name or str(f.get("path", "")).rsplit("/", 1)[-1] == name
            for f in before
        ):
            raise TunnelError("远程同名文件已存在，拒绝覆盖")
        sequence, response = self._request(
            5, {"type": "model", "storage": "emmc", "path": name, "total": len(data)}
        )
        if response["result"] != 1:
            raise TunnelError(
                "FILE_UPLOAD 初始化失败或文件已存在，result=" + str(response["result"])
            )
        parameters = response.get("reply", {})
        kb, offset = parameters.get("chunk_size"), parameters.get("offset")
        if type(kb) is not int or not 1 <= kb <= 4096 or offset != 0:
            raise TunnelError("FILE_UPLOAD 分块参数无效；不支持续传")
        chunk_size = kb * 1024
        digest = hashlib.md5(data).hexdigest()
        fragments = 0
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            request = {"offset": offset, "size": len(chunk)}
            if offset + len(chunk) == len(data):
                request["file_md5"] = digest
            # P2S requires all fragments in one pipeline, no per-fragment read/wait.
            self._send_json(
                {
                    "mtype": 12289,
                    "cmdtype": 5,
                    "sequence": sequence,
                    "frag_id": fragments,
                    "req": request,
                },
                chunk,
            )
            fragments += 1
        for _ in range(128):
            response = self._reply(5, sequence)
            if response["result"] != 1:
                break
        if response["result"] != 0:
            raise TunnelError("FILE_UPLOAD 完成失败，result=" + str(response["result"]))
        self.audit.append(
            {
                "stage": "upload_complete",
                "bytes": len(data),
                "chunk_bytes": chunk_size,
                "fragments": fragments,
            }
        )
        # Use a fresh TLS session in the CLI for independent persistent LIST_INFO verification.
        return {
            "bytes": len(data),
            "md5": digest,
            "chunk_bytes": chunk_size,
            "fragments": fragments,
            "media_ability": ability,
        }


def find_verified_model(entries, name, size=None):
    matches = [
        f
        for f in entries
        if f.get("name") == name or str(f.get("path", "")).rsplit("/", 1)[-1] == name
    ]
    if len(matches) != 1:
        raise TunnelError("LIST_INFO 未找到唯一匹配的内部文件")
    entry = matches[0]
    path = entry.get("path")
    if not isinstance(path, str) or not path.startswith("/userdata/model/"):
        raise TunnelError("LIST_INFO 未返回已知的内部模型路径")
    if path.rsplit("/", 1)[-1] != name:
        raise TunnelError("LIST_INFO 内部路径文件名不匹配")
    if type(entry.get("size")) is not int or entry["size"] <= 0:
        raise TunnelError("LIST_INFO 文件大小无效")
    if size is not None and entry["size"] != size:
        raise TunnelError("LIST_INFO 文件大小与本地不匹配")
    return {
        "name": name,
        "size": entry["size"],
        "path": path,
        "url": "file://" + path,
        "cache_url": "brtc://emmc/" + name,
    }
