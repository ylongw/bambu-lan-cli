# SPDX-License-Identifier: AGPL-3.0-or-later
import inspect
import os
import select
import shlex
import socket
import subprocess
import threading


def ssh_arguments():
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ForkAfterAuthentication=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "StrictHostKeyChecking=yes",
    ]


def mqtt_relay_worker():
    """NAS 只中继 TLS 字节；此代码不接收访问码，也不写文件。"""
    import os
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    import select
    import signal
    import socket
    import sys

    signal.alarm(int(sys.argv[2]))
    with socket.create_connection((sys.argv[1], int(sys.argv[3])), timeout=10) as peer:
        peer.settimeout(10)
        while True:
            readable, _, _ = select.select([peer, 0], [], [], 20)
            if not readable:
                continue
            for channel in readable:
                data = peer.recv(65536) if channel is peer else os.read(0, 65536)
                if not data:
                    return
                if channel is peer:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                else:
                    peer.sendall(data)


class SshTunnel:
    """单连接回环转发：SSH stdio 中继，无需服务端允许 -L。"""

    def __init__(self, config, via, timeout=15, target_port=8883):
        self.target_port = target_port
        from .config import validate_via

        validate_via(via)
        self.config, self.via, self.timeout = config, via, timeout
        self.process = self.listener = self.client_socket = self.thread = None
        self.port = None
        self.stopping = threading.Event()
        self.error = None

    def _relay(self):
        try:
            while not self.stopping.is_set():
                try:
                    client, _ = self.listener.accept()
                    break
                except socket.timeout:
                    if self.process.poll() is not None:
                        self.error = (
                            f"SSH 中继启动失败（exit={self.process.returncode}）"
                        )
                        return
            else:
                return
            self.client_socket = client
            with client:
                client.settimeout(2)
                while not self.stopping.is_set():
                    readable, _, _ = select.select(
                        [client, self.process.stdout], [], [], 0.2
                    )
                    for channel in readable:
                        data = (
                            client.recv(65536)
                            if channel is client
                            else os.read(channel.fileno(), 65536)
                        )
                        if not data:
                            return
                        if channel is client:
                            self.process.stdin.write(data)
                            self.process.stdin.flush()
                        else:
                            client.sendall(data)
        except (OSError, ValueError) as exc:
            if not self.stopping.is_set():
                self.error = "SSH 中继连接失败：" + type(exc).__name__

    def close(self):
        self.stopping.set()
        if self.client_socket is not None:
            try:
                self.client_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.listener is not None:
            self.listener.close()
        if self.process is not None:
            for stream in (
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ):
                stream.close()

    def __enter__(self):
        try:
            self.listener = socket.socket()
            self.listener.bind(("127.0.0.1", 0))
            self.port = self.listener.getsockname()[1]
            self.listener.listen(1)
            self.listener.settimeout(0.2)
            source = inspect.getsource(mqtt_relay_worker) + "\nmqtt_relay_worker()"
            remote = (
                "python3 -I -c "
                + shlex.quote(source)
                + " "
                + shlex.quote(self.config.host)
            )
            remote += (
                " " + str(int(self.timeout * 8 + 30)) + " " + str(self.target_port)
            )
            self.process = subprocess.Popen(
                ssh_arguments() + [self.via, remote],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.thread = threading.Thread(
                target=self._relay, name="bambu-ssh-relay", daemon=True
            )
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.close()
