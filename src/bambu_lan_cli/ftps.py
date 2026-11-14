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
        raw = socket.create_connection((host, port), timeout, source_address)
        try:
            self.sock = self.context.wrap_socket(raw, server_hostname=host)
        except Exception:
            raw.close()
            raise
        self.af = self.sock.family
        self.file = self.sock.makefile("r", encoding=self.encoding)
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
        client.login("bblp", config.access_code)
        client.prot_p()
        client.set_pasv(True)
        return client
    except Exception:
        client.close()
        raise
