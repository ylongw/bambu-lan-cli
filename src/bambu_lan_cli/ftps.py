# SPDX-License-Identifier: AGPL-3.0-or-later
import ftplib
import socket

from .common import tls_context
from .errors import CliError


class ImplicitFTPS(ftplib.FTP_TLS):
    """990 隐式 TLS；数据通道复用控制通道 TLS session。"""

    def connect(self, host="", port=990, timeout=15, source_address=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.source_address = source_address
        self.connection_stage = "TCP 连接"
        raw = socket.create_connection((host, port), timeout, source_address)
        try:
            self.connection_stage = "TCP 已连接，但 TLS 握手"
            self.sock = self.context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.connection_stage = "TLS 已建立，但 FTP 欢迎响应"
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                conn = self.context.wrap_socket(
                    conn,
                    server_hostname=self.host,
                    session=self.sock.session,
                )
            except Exception:
                conn.close()
                raise
        return conn, size

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        # P1/P2 firmware can stall on FTP_TLS.retrbinary's TLS unwrap. Match
        # the upstream FTPS dialect and our existing LIST close behaviour.
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                chunk = conn.recv(blocksize)
                if not chunk:
                    break
                callback(chunk)
        return self.voidresp()

    def root_list(self, path="/"):
        self.voidcmd("TYPE A")
        contents = bytearray()
        with self.transfercmd("LIST " + path) as conn:
            while True:
                chunk = conn.recv(8192)
                if not chunk:
                    break
                contents.extend(chunk)
                if len(contents) > 1_048_576:
                    raise CliError("根目录列表超过 1 MiB，已终止读取")
        self.voidresp()
        return contents.decode("utf-8", errors="replace").splitlines()


def open_ftps(config, timeout):
    client = ImplicitFTPS(context=tls_context(), timeout=timeout)
    try:
        client.connect(config.host, 990, timeout)
        client.connection_stage = "登录认证"
        client.login("bblp", config.access_code)
        client.connection_stage = "数据通道保护协商"
        client.prot_p()
        client.set_pasv(True)
        return client
    except TimeoutError:
        stage = getattr(client, "connection_stage", "连接/认证")
        client.close()
        raise CliError(
            f"FTPS {stage}超时（{timeout:g} 秒）；尚未进入目录列表，可加大 --timeout；"
            "检查网络与打印机 LAN FTPS 服务，不能据此断言未插入存储；"
            "可用 status 检查 U 盘/TF 识别状态，media-ability 查看能力"
        ) from None
    except Exception:
        client.close()
        raise


def list_external(config, timeout, path, list_timeout=30):
    """Separate connection/auth errors from a slow authenticated directory read.

    Never echo a server reply: it may contain private paths or credentials.
    A 550 alone cannot distinguish unmounted storage, missing path and permission.
    """
    client = None
    stage = "连接/认证"
    hint = "检查 U 盘/TF 卡是否被打印机识别，可先运行 media-ability 查看能力（不代表已挂载）"
    try:
        client = open_ftps(config, timeout)
        stage = "列表"
        client.timeout = list_timeout
        client.sock.settimeout(list_timeout)
        return {"ftps": "ok", "entries": client.root_list(path)}
    except TimeoutError:
        detail = (
            f"连接认证成功，但列表超时（{list_timeout:g} 秒）；可加大 --list-timeout（最大 180 秒）"
            if stage == "列表"
            else f"连接/认证超时（{timeout:g} 秒）；检查网络，可加大 --timeout"
        )
        raise CliError("FTPS " + detail + "；" + hint) from None
    except ftplib.Error as exc:
        text = str(exc)
        code = text[:3] if text[:3].isdigit() else "unknown"
        if code == "550":
            detail = (
                "路径不存在、无访问权限或外部存储未挂载/不可访问（550，设备未区分原因）"
            )
        elif code == "530":
            detail = "认证失败（530）；检查本地 access code 与开发者模式"
        elif code in ("425", "426", "450", "451"):
            detail = f"数据通道/存储暂不可用（{code}）；可稍后重试"
        else:
            detail = f"{stage}被拒绝/协议异常，FTP code={code}"
        raise CliError("FTPS " + detail + "；" + hint) from None
    except OSError as exc:
        raise CliError(
            f"FTPS {stage}网络失败：{type(exc).__name__}, errno={exc.errno}；" + hint
        ) from None
    finally:
        if client is not None:
            client.close()
