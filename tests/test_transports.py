# SPDX-License-Identifier: AGPL-3.0-or-later
import io
import json
import struct
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bambu_lan_cli.errors import CliError
from bambu_lan_cli.mqtt import MqttConnection
from bambu_lan_cli.relay import SshTunnel
from bambu_lan_cli.safety import Authorization
from bambu_lan_cli.tunnel import BambuTunnelLocal, TunnelError, find_verified_model


class Socket:
    def __init__(self, data=b""):
        self.data = io.BytesIO(data)
        self.sent = bytearray()

    def settimeout(self, value):
        pass

    def recv(self, count):
        return self.data.read(min(count, 3))

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        pass


def test_frame_partial_read(config):
    t = BambuTunnelLocal(config)
    t.sock = Socket(struct.pack("<IIII", 4, t.LOGIN_RX, 0, 0) + b"1234")
    assert t._receive() == (t.LOGIN_RX, b"1234")
    t._send(t.CTRL_TX, b"{}")
    assert struct.unpack("<IIII", t.sock.sent[:16]) == (2, t.CTRL_TX, 0, 0)
    assert t.sock.sent[16:] == b"{}"


@pytest.mark.parametrize("size,magic", [(9 * 1024 * 1024, 0x0002013F), (0, 1)])
def test_invalid_frame(config, size, magic):
    t = BambuTunnelLocal(config)
    t.sock = Socket(struct.pack("<IIII", size, magic, 0, 0))
    with pytest.raises(TunnelError):
        t._receive()


def test_tunnel_readonly_gate(config):
    t = BambuTunnelLocal(config)
    with pytest.raises(TunnelError):
        t._request(5, {})
    with pytest.raises(TunnelError):
        t._request(2, {})
    with pytest.raises(CliError):
        t.upload("missing", "sample.gcode.3mf", Authorization(False, config.serial))


def test_upload_pipeline(config, tmp_path):
    source = tmp_path / "sample.gcode.3mf"
    source.write_bytes(b"x" * 2500)
    t = BambuTunnelLocal(config)
    t.media_ability = Mock(return_value={"upload_storage": ["emmc"]})
    t.list_models = Mock(return_value=[])
    t._request = Mock(
        return_value=(1, {"result": 1, "reply": {"chunk_size": 1, "offset": 0}})
    )
    t._send_json = Mock()
    t._reply = Mock(return_value={"result": 0})
    result = t.upload(source, source.name, Authorization(True, config.serial))
    assert result["fragments"] == 3 and result["bytes"] == 2500
    assert t._request.call_args.args[0] == 5
    assert [c.args[0]["req"]["offset"] for c in t._send_json.call_args_list] == [
        0,
        1024,
        2048,
    ]
    assert "file_md5" in t._send_json.call_args_list[-1].args[0]["req"]
    t.list_models.return_value = [{"name": source.name}]
    t._request.reset_mock()
    with pytest.raises(TunnelError):
        t.upload(source, source.name, Authorization(True, config.serial))
    t._request.assert_not_called()


def test_verified_file():
    entries = [
        {
            "name": "sample.gcode.3mf",
            "path": "/userdata/model/sample.gcode.3mf",
            "size": 123,
        }
    ]
    assert find_verified_model(entries, "sample.gcode.3mf", 123)["url"].startswith(
        "file:///userdata/model/"
    )
    for size in (0, 124):
        with pytest.raises(TunnelError):
            find_verified_model(entries, "sample.gcode.3mf", size)
    with pytest.raises(TunnelError):
        find_verified_model(entries * 2, "sample.gcode.3mf")


def test_upload_fresh_connection(config, tmp_path, monkeypatch):
    import bambu_lan_cli.files as files

    source = tmp_path / "sample.gcode.3mf"
    source.write_bytes(b"123")
    first, second = Mock(), Mock()
    first.upload.return_value = {"bytes": 3}
    second.list_models.return_value = [
        {"name": source.name, "path": "/userdata/model/" + source.name, "size": 3}
    ]
    sessions = []

    @contextmanager
    def connect(*args):
        obj = first if not sessions else second
        sessions.append(obj)
        yield obj

    monkeypatch.setattr(files, "internal_connection", connect)
    args = SimpleNamespace(source=str(source), name=None, internal=True)
    assert files.upload_file(config, args, Authorization(True, config.serial))[
        "verified_fresh_connection"
    ]
    assert sessions == [first, second]


def test_mqtt_sequences_ack_and_gate(config):
    connection = MqttConnection(config, 0.01)
    connection.publish = Mock()

    def reply(*args, **kwargs):
        section, body = next(iter(args[0].items()))
        connection.reports.append(
            {
                section: {
                    "sequence_id": body["sequence_id"],
                    "command": body["command"],
                    "result": "SUCCESS",
                }
            }
        )

    connection.publish.side_effect = reply
    auth = Authorization(True, config.serial)
    connection.command({"command": "resume"}, auth)
    first = connection.publish.call_args.args[0]["print"]
    connection.command({"command": "resume"}, auth)
    second = connection.publish.call_args.args[0]["print"]
    assert (
        int(second["sequence_id"]) > int(first["sequence_id"]) and first["param"] == ""
    )
    assert connection.publish.call_args.kwargs["qos"] == 0
    connection.publish.reset_mock()
    with pytest.raises(CliError):
        connection.command({"command": "resume"}, Authorization(False, config.serial))
    connection.publish.assert_not_called()


def test_mqtt_raw_write_gate(config):
    conn = MqttConnection(config, 0.01)
    with pytest.raises(CliError):
        conn.publish({"print": {"command": "resume"}})


def test_mqtt_merge(config):
    conn = MqttConnection(config, 1)
    for payload in [
        {"print": {"device": {"ctc": {"info": {"temp": 32}}}}},
        {"print": {"gcode_state": "PAUSE"}},
        {"print": {"device": {"airduct": 1}}},
    ]:
        conn._on_message(
            None, None, SimpleNamespace(payload=json.dumps(payload).encode())
        )
    assert (
        conn.status["device"]["ctc"]["info"]["temp"] == 32
        and conn.status["device"]["airduct"] == 1
    )
    conn._on_message(None, None, SimpleNamespace(payload=b"invalid"))
    assert conn.report_count == 3


@pytest.mark.parametrize("port", [8883, 6000])
def test_ssh_byte_relay(config, port, monkeypatch):
    import bambu_lan_cli.relay as relay

    process = Mock()
    process.poll.return_value = 0
    monkeypatch.setattr(relay.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(relay.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(relay.threading.Thread, "join", lambda *a, **k: None)
    # Listener is loopback only; no connection to hardware is made.
    with SshTunnel(config, "my-nas", 1, target_port=port) as t:
        argv = relay.subprocess.Popen.call_args.args[0]
        assert argv[-2] == "my-nas" and "python3 -I -c" in argv[-1]
        assert argv[-1].endswith(" " + str(port)) and config.access_code not in argv[-1]
        assert t.listener.getsockname()[0] == "127.0.0.1"


def test_ssh_ftps_secrets_stdin_only(config, monkeypatch):
    import bambu_lan_cli.files as files

    result = SimpleNamespace(
        returncode=0, stdout=b'{"ok":true,"result":{"ftps":"ok","entries":[]}}'
    )
    run = Mock(return_value=result)
    monkeypatch.setattr(files.subprocess, "run", run)
    files.ssh_ftps(config, SimpleNamespace(via="my-nas", timeout=1), "ls", "/")
    argv = run.call_args.args[0]
    stdin = run.call_args.kwargs["input"]
    assert (
        config.access_code not in " ".join(argv)
        and config.access_code.encode() in stdin
    )
    n, rest = stdin.split(b"\n", 1)
    source = rest[: int(n)].decode()
    compile(source, "<worker>", "exec")
    assert "RLIMIT_CORE" in source and "signal.alarm" in source


def test_ftps_session_reuse(monkeypatch):
    import ftplib

    from bambu_lan_cli.ftps import ImplicitFTPS

    ctx = Mock()
    sock = Mock()
    sock.session = object()
    ftp = ImplicitFTPS(context=ctx)
    ftp.sock = sock
    ftp._prot_p = True
    ftp.host = "localhost"
    raw = Mock()
    monkeypatch.setattr(ftplib.FTP, "ntransfercmd", lambda *a: (raw, None))
    ftp.ntransfercmd("LIST /")
    assert ctx.wrap_socket.call_args.kwargs["session"] is sock.session


def test_tunnel_login_and_list(config, monkeypatch):
    import bambu_lan_cli.tunnel as module

    def frame(magic, payload):
        return struct.pack("<IIII", len(payload), magic, 0, 0) + payload

    reply = {"mtype": 12291, "sequence": 0, "result": 0}
    listing = {"cmdtype": 1, "sequence": 1, "result": 0, "reply": {"file_lists": []}}
    sock = Socket(
        frame(BambuTunnelLocal.LOGIN_RX, b"\0" * 4)
        + frame(BambuTunnelLocal.CTRL_RX, json.dumps(reply).encode())
        + frame(BambuTunnelLocal.CTRL_RX, json.dumps(listing).encode())
    )
    monkeypatch.setattr(module.socket, "create_connection", lambda *a: sock)
    ctx = Mock()
    ctx.wrap_socket.return_value = sock
    monkeypatch.setattr(module.ssl, "SSLContext", lambda *a: ctx)
    with BambuTunnelLocal(config) as t:
        assert t.list_models() == []
    assert config.access_code.encode() in sock.sent


def test_ftps_mock_upload_and_no_overwrite(config, monkeypatch):
    import bambu_lan_cli.files as module

    ftp = Mock()
    ftp.nlst.return_value = []
    ftp.size.return_value = 3

    @contextmanager
    def connect(*a):
        yield ftp

    monkeypatch.setattr(module, "open_ftps", connect)
    assert (
        module.ftps_operation(
            config,
            1,
            "upload",
            "/sample.gcode.3mf",
            b"123",
            Authorization(True, config.serial),
        )["bytes"]
        == 3
    )
    assert ftp.storbinary.call_args.args[0] == "STOR /sample.gcode.3mf"
    ftp.storbinary.reset_mock()
    ftp.nlst.return_value = ["sample.gcode.3mf"]
    with pytest.raises(CliError):
        module.ftps_operation(
            config,
            1,
            "upload",
            "/sample.gcode.3mf",
            b"123",
            Authorization(True, config.serial),
        )
    ftp.storbinary.assert_not_called()


def test_discovery_masks_unknown_identity(config, monkeypatch):
    import bambu_lan_cli.discovery as module

    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.recvfrom.side_effect = [
        (
            b"USN: unknown-private-serial\r\nDevModel.bambu.com: P2S\r\nDevName.bambu.com: private-name\r\n",
            ("192.168.1.100", 2021),
        )
    ]
    monkeypatch.setattr(module.socket, "socket", lambda *a: sock)
    times = iter([0, 0, 2])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    result = module.discover(1, config)
    assert (
        result["count"] == 1
        and "unknown-private-serial" not in str(result)
        and "private-name" not in str(result)
    )
    assert sock.sendto.call_count == 2


def test_remote_ftps_worker_executes_in_memory(config, monkeypatch, capsys):
    import inspect
    import resource
    import signal

    import bambu_lan_cli.files as module

    source = "\n".join(
        [
            "import ftplib, json, socket, ssl, sys\nfrom dataclasses import dataclass, field",
            *(
                inspect.getsource(obj)
                for obj in (
                    module.CliError,
                    module.Authorization,
                    module.tls_context,
                    module.ImplicitFTPS,
                    module.open_ftps,
                    module.list_external,
                    module.validate_path,
                    module.ftps_operation,
                    module.remote_worker,
                )
            ),
        ]
    )
    namespace = {}
    exec(compile(source, "<worker-test>", "exec"), namespace)
    ftp = Mock()
    ftp.root_list.return_value = ["-" + config.access_code]

    @contextmanager
    def connect(*args):
        yield ftp

    namespace["open_ftps"] = lambda *args: ftp
    values = json.dumps(
        dict(
            host=config.host,
            access_code=config.access_code,
            timeout=1,
            operation="ls",
            path="/",
            size=0,
        )
    ).encode()
    stdin = SimpleNamespace(
        buffer=io.BytesIO(str(len(values)).encode() + b"\n" + values)
    )
    import sys

    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(signal, "alarm", lambda *args: None)
    namespace["remote_worker"]()
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is True
    assert config.access_code not in output


def test_ftps_low_level_write_gates(config, monkeypatch):
    from bambu_lan_cli import files

    network = Mock(side_effect=AssertionError("network before gate"))
    monkeypatch.setattr(files, "open_ftps", network)
    monkeypatch.setattr(files.subprocess, "run", network)
    for authorization in (
        None,
        Authorization(False, config.serial),
        Authorization(True, "wrong"),
    ):
        with pytest.raises(CliError):
            files.ftps_operation(
                config, 1, "upload", "/sample.gcode.3mf", b"123", authorization
            )
        with pytest.raises(CliError):
            files.ssh_ftps(
                config,
                SimpleNamespace(via="my-nas", timeout=1),
                "upload",
                "/sample.gcode.3mf",
                b"123",
                authorization,
            )
    network.assert_not_called()
