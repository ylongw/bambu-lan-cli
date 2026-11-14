# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import io
import json
import struct
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from bambu_lan_cli.cli import build_parser, dispatch, main
from bambu_lan_cli.commands import build_payload
from bambu_lan_cli.errors import CliError
from bambu_lan_cli.mqtt import MqttConnection
from bambu_lan_cli.protocol import (
    CATALOG,
    XCAM_MODULES,
    protocol_payload,
    readonly_payload,
)
from bambu_lan_cli.safety import Authorization, redact
from bambu_lan_cli.state import camera_status, job_status
from bambu_lan_cli.tunnel import BambuTunnelLocal, TunnelError


def args(*words):
    return build_parser().parse_args(words)


@pytest.mark.parametrize("name", [k for k, v in CATALOG.items() if not v["readonly"]])
@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--execute"],
        ["--confirm-serial", "wrong"],
        ["--execute", "--confirm-serial", "wrong"],
    ],
)
def test_every_advanced_write_gated(name, flags, config, monkeypatch):
    import bambu_lan_cli.cli as cli

    network = Mock(side_effect=AssertionError("network before gate"))
    monkeypatch.setattr(cli, "status_connection", network)
    with pytest.raises(CliError):
        dispatch(config, args("mqtt", name, "--params", "{}", *flags))
    network.assert_not_called()


@pytest.mark.parametrize("name", CATALOG)
def test_catalog_schemas_reject_injection(name):
    assert CATALOG[name]["sources"]
    for params in ({"command": "stop"}, {"sequence_id": "0"}, {"unknown": True}, []):
        with pytest.raises(CliError):
            protocol_payload(name, params)


@pytest.mark.parametrize("name", [k for k, v in CATALOG.items() if v["readonly"]])
def test_readonly_whitelist_is_exact(name):
    spec = CATALOG[name]
    params = {
        k: {"str": "", "int": 1, "list": []}[spec["fields"][k][0]]
        for k in spec["required"]
    }
    if name == "camera.ipcam_get_media_info":
        params = {
            "sub_command": "is_timelapse_storage_enough",
            "storage": "internal",
            "total_layer": 1,
        }
    section, body = protocol_payload(name, params)
    assert readonly_payload({section: body})
    assert not readonly_payload({section: {**body, "enable": True}})
    assert not readonly_payload({section: body, "print": {"command": "stop"}})


@pytest.mark.parametrize("module", XCAM_MODULES)
def test_xcam_schemas(module):
    section, body = build_payload(
        args("xcam", module, "disable", "--sensitivity", "never_halt")
    )
    assert section == "xcam"
    assert body == {
        "command": "xcam_control_set",
        "module_name": module,
        "control": False,
        "enable": False,
        "print_halt": True,
        "halt_print_sensitivity": "never_halt",
    }


@pytest.mark.parametrize(
    "command,wire",
    [
        ("camera-record", "ipcam_record_set"),
        ("camera-timelapse", "ipcam_timelapse"),
        ("camera-capture", "ipcam_cap_pic_set"),
    ],
)
def test_camera_control_exact(command, wire):
    assert build_payload(args(command, "disable")) == (
        "camera",
        {"command": wire, "control": "disable"},
    )


def test_more_payloads():
    assert build_payload(args("camera-resolution", "720p")) == (
        "camera",
        {"command": "ipcam_resolution_set", "resolution": "720p"},
    )
    assert (
        build_payload(args("nozzle", "--diameter", "0.6", "--protocol", "set-nozzle"))[
            1
        ]["diameter"]
        == 0.6
    )
    assert (
        build_payload(args("print-option", "auto_recovery", "enable"))[1]["option"] == 1
    )
    assert (
        build_payload(args("light", "chamber_light", "flashing", "--on-time", "100"))[
            1
        ]["led_on_time"]
        == 100
    )
    assert build_payload(args("ams-control", "abort"))[1]["param"] == "abort"


def test_job_eta():
    now = datetime(2026, 1, 1, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    state = {
        "gcode_state": "RUNNING",
        "subtask_name": "sample",
        "mc_percent": 50,
        "mc_remaining_time": 90,
        "layer_num": 5,
        "total_layer_num": 10,
        "stg_cur": 0,
    }
    result = job_status(state, now)
    assert result["estimated_finish_cst"] == "2026-01-02T01:00:00+08:00"
    assert result["progress_percent"] == 50 and result["stage"] == 0
    state["gcode_state"] = "PAUSE"
    assert job_status(state, now)["estimated_finish_cst"] is None
    state.update(gcode_state="RUNNING", mc_remaining_time="bad")
    assert job_status(state, now)["estimated_finish_cst"] is None


def test_camera_and_raw_privacy(config):
    state = {
        "ipcam": {
            "rtsp_url": "rtsps://other:private-password@example.invalid/live?token=private-token",
            "ipcam_record": "disable",
            "tl_internal_free_kb": 100,
        }
    }
    assert camera_status(state)["rtsp"]["available"]
    raw = {
        "print": state,
        "info": {
            "module": [
                {
                    "sn": "unknown-device",
                    "access_code": "unknown-code",
                    "tag_uid": "unknown-rfid",
                }
            ]
        },
    }
    text = redact(raw, config)
    for secret in (
        "private-password",
        "private-token",
        "unknown-device",
        "unknown-code",
        "unknown-rfid",
    ):
        assert secret not in text
    assert "ipcam_record" in text and "tl_internal_free_kb" in text


def test_query_matching_and_lowercase_ack(config):
    connection = MqttConnection(config, 0.1)

    def reply(payload, **kwargs):
        section, body = next(iter(payload.items()))
        connection.reports.extend(
            [
                {section: {**body, "sequence_id": "old", "result": "fail"}},
                {section: {**body, "result": "success"}},
            ]
        )

    connection.publish = Mock(side_effect=reply)
    assert connection.query("info", {"command": "get_version"})["result"] == "success"
    assert (
        connection.command(
            {"command": "ipcam_record_set", "control": "disable"},
            Authorization(True, config.serial),
            "camera",
        )["result"]
        == "SUCCESS"
    )
    assert "param" not in connection.publish.call_args.args[0]["camera"]


def test_raw_status(config):
    connection = MqttConnection(config, 0.1)
    connection.status = {
        "gcode_state": "RUNNING",
        "nozzle_temper": 10,
        "bed_temper": 10,
        "unrecognized_field": {"future": 1},
    }
    connection.version = {"module": [{"name": "ota"}]}
    connection.publish = Mock()
    connection.poll = Mock()
    assert connection.query_status(raw=True)["print"]["unrecognized_field"] == {
        "future": 1
    }


def test_snapshot_jpeg_partial_reads(config, monkeypatch, tmp_path):
    from bambu_lan_cli import camera

    data = b"\xff\xd8test\xff\xd9"
    source = io.BytesIO(struct.pack("<IIII", len(data), 0, 1, 0) + data)
    sock = Mock()
    sock.recv.side_effect = lambda size: source.read(min(size, 2))
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock()
    monkeypatch.setattr(camera.socket, "create_connection", Mock(return_value=sock))
    context = Mock()
    context.wrap_socket.return_value = sock
    monkeypatch.setattr(camera, "tls_context", lambda: context)
    target = tmp_path / "frame.jpg"
    result = camera.snapshot(
        config,
        SimpleNamespace(transport="jpeg", via=None, timeout=1, destination=str(target)),
    )
    assert result["bytes"] == len(data) and target.read_bytes() == data
    assert len(sock.sendall.call_args.args[0]) == 80


def test_download_atomic_and_limits(config, monkeypatch, tmp_path):
    from bambu_lan_cli import files

    ftp = Mock()
    ftp.size.return_value = 3
    ftp.retrbinary.side_effect = lambda command, sink, **kw: sink(b"abc")

    @contextmanager
    def connect(*a):
        yield ftp

    monkeypatch.setattr(files, "open_ftps", connect)
    target = tmp_path / "clip.mp4"
    a = args("download", "/timelapse/sample.mp4", str(target))
    a.via = None
    assert files.download_file(config, a)["bytes"] == 3
    assert target.read_bytes() == b"abc" and target.stat().st_mode & 0o777 == 0o600
    ftp.retrbinary.reset_mock()
    with pytest.raises(CliError):
        files.download_file(config, a)
    ftp.retrbinary.assert_not_called()
    target.unlink()
    ftp.size.return_value = 4
    with pytest.raises(CliError):
        files.download_file(config, a)
    assert not target.exists() and not list(tmp_path.glob(".bambu-*"))


def test_tunnel_download_and_checksum(config):
    tunnel = BambuTunnelLocal(config)
    tunnel.resolve_file = Mock(
        return_value={"path": "/userdata/media/timelapse/sample.mp4", "size": 6}
    )
    tunnel._send_json = Mock()
    final = {"offset": 3, "size": 3, "file_md5": hashlib.md5(b"abcdef").hexdigest()}
    replies = [
        ({"cmdtype": 4, "sequence": 999, "result": 0}, b"ignore"),
        (
            {
                "cmdtype": 4,
                "sequence": 1,
                "result": 1,
                "reply": {"offset": 0, "size": 3},
            },
            b"abc",
        ),
        ({"cmdtype": 4, "sequence": 1, "result": 0, "reply": final}, b"def"),
    ]
    tunnel._packet_receive = Mock(side_effect=replies)
    sink = io.BytesIO()
    assert tunnel.download("sample.mp4", "timelapse", sink.write, 100)["md5_verified"]
    assert sink.getvalue() == b"abcdef"
    assert tunnel._send_json.call_args.args[0]["req"]["path"].endswith("/sample.mp4")
    tunnel._packet_receive = Mock(
        return_value=(
            {"cmdtype": 4, "sequence": 2, "result": 0, "reply": {"file_md5": "bad"}},
            b"abcdef",
        )
    )
    with pytest.raises(TunnelError):
        tunnel.download("sample.mp4", "timelapse", lambda data: None, 100)


def test_internal_timelapse_storage(config):
    tunnel = BambuTunnelLocal(config)
    tunnel._request = Mock(return_value=(1, {"result": 0, "reply": {"file_lists": []}}))
    tunnel.list_models(kind="timelapse")
    assert tunnel._request.call_args.args[1]["storage"] == "internal"
    tunnel.list_models()
    assert tunnel._request.call_args.args[1]["storage"] == "emmc"


def test_delete_gate_and_verification(config):
    tunnel = BambuTunnelLocal(config)
    tunnel.resolve_file = Mock(
        return_value={"path": "/userdata/media/timelapse/sample.mp4"}
    )
    tunnel._request = Mock(return_value=(1, {"result": 0}))
    tunnel.list_models = Mock(return_value=[])
    with pytest.raises(CliError):
        tunnel.delete("sample.mp4", "timelapse", Authorization(False, config.serial))
    tunnel.resolve_file.assert_not_called()
    assert tunnel.delete("sample.mp4", "timelapse", Authorization(True, config.serial))[
        "verified_by_listing"
    ]
    assert tunnel._request.call_args.args == (
        3,
        {"paths": ["/userdata/media/timelapse/sample.mp4"]},
    )


@pytest.mark.parametrize("op", ["delete", "rename"])
def test_ftps_lowlevel_new_write_gates(config, monkeypatch, op):
    from bambu_lan_cli import files

    network = Mock(side_effect=AssertionError("network before gate"))
    monkeypatch.setattr(files, "open_ftps", network)
    monkeypatch.setattr(files.subprocess, "run", network)
    for auth in (
        None,
        Authorization(False, config.serial),
        Authorization(True, "wrong"),
    ):
        with pytest.raises(CliError):
            files.ftps_operation(config, 1, op, "/sample.mp4", "/other.mp4", auth)
        with pytest.raises(CliError):
            files.ssh_ftps(
                config,
                SimpleNamespace(via="my-nas", timeout=1),
                op,
                "/sample.mp4",
                "/other.mp4",
                auth,
            )
    network.assert_not_called()


def test_protocol_list_without_config(capsys):
    assert main(["protocol-list", "--json"]) == 0
    assert "camera.ipcam_record_set" in json.loads(capsys.readouterr().out)["result"]


def sample_params(spec):
    defaults = {
        "str": "sample",
        "int": 1,
        "float": 0.4,
        "bool": True,
        "list": [],
        "dict": {},
    }
    params = {k: defaults[kinds[0]] for k, kinds in spec["fields"].items()}
    for k in ("nozzle_temp", "nozzle_temp_min", "nozzle_temp_max"):
        if k in params:
            params[k] = 200
    if "bed_temp" in params:
        params["bed_temp"] = 60
    if "cooling_temp" in params:
        params["cooling_temp"] = 45
    if "ctt_val" in params:
        params["ctt_val"] = 40
    if spec["command"] == "ams_filament_drying":
        params.update(temp=55, duration=4)
    if spec["command"] == "ipcam_get_media_info":
        params["sub_command"] = "is_timelapse_storage_enough"
    return params


@pytest.mark.parametrize("name", CATALOG)
def test_each_catalog_command_mock_mqtt(config, name):
    spec = CATALOG[name]
    params = sample_params(spec)
    section, body = protocol_payload(name, params)
    connection = MqttConnection(config, 0.1)

    def response(payload, **kw):
        envelope, request = next(iter(payload.items()))
        connection.reports.append({envelope: {**request, "result": "SUCCESS"}})

    connection.publish = Mock(side_effect=response)
    if spec["readonly"]:
        result = connection.query(section, body)
    else:
        result = connection.command(body, Authorization(True, config.serial), section)
    assert result["result"] == "SUCCESS"
    sent = connection.publish.call_args.args[0][section]
    assert sent.get("command") == spec["command"] and "sequence_id" in sent


def test_ftps_operations(config, monkeypatch):
    from bambu_lan_cli import files

    ftp = Mock()
    ftp.nlst.return_value = ["existing.mp4"]
    ftp.size.return_value = 3
    ftp.retrbinary.side_effect = lambda command, sink, **kw: sink(b"abc")

    @contextmanager
    def connect(*a):
        yield ftp

    monkeypatch.setattr(files, "open_ftps", connect)
    auth = Authorization(True, config.serial)
    for operation in ("mkdir", "rmdir", "delete"):
        result = files.ftps_operation(
            config, 1, operation, "/sample", authorization=auth
        )
        assert result["acknowledged"]
    with pytest.raises(CliError):
        files.ftps_operation(config, 1, "rmdir", "//./", authorization=auth)
    with pytest.raises(CliError):
        files.ftps_operation(config, 1, "rename", "/sample.mp4", "/existing.mp4", auth)
    files.ftps_operation(config, 1, "rename", "/sample.mp4", "/new.mp4", auth)
    ftp.rename.assert_called_once_with("/sample.mp4", "/new.mp4")
    assert files.ftps_operation(config, 1, "file-info", "/sample.mp4")["size"] == 3
    assert (
        files.ftps_operation(config, 1, "download", "/sample.mp4")["data_b64"] == "YWJj"
    )


def test_project_member_and_archive(config):
    tunnel = BambuTunnelLocal(config)
    tunnel._send_json = Mock()
    header = json.dumps({"err_no": 0}).encode()
    tunnel._packet_receive = Mock(
        side_effect=[
            (
                {
                    "cmdtype": 4,
                    "sequence": 1,
                    "result": 1,
                    "reply": {"mem_dl_param_size": len(header)},
                },
                header,
            ),
            (
                {
                    "cmdtype": 4,
                    "sequence": 1,
                    "result": 0,
                    "reply": {"file_md5": hashlib.md5(b"abc").hexdigest()},
                },
                b"abc",
            ),
        ]
    )
    sink = io.BytesIO()
    assert tunnel.project_member("Metadata/plate_1.png", sink.write)["bytes"] == 3
    assert sink.getvalue() == b"abc"
    request, parameter = tunnel._send_json.call_args.args
    assert (
        request["req"]["path"] == "mem:/16"
        and json.loads(parameter)["command"] == "get_project_file"
    )
    tunnel.resolve_file = Mock(
        return_value={"path": "/userdata/model/sample.gcode.3mf"}
    )
    tunnel._packet_receive = Mock(
        return_value=(
            {"cmdtype": 2, "sequence": 2, "result": 0, "reply": {"size": 3}},
            b"abc",
        )
    )
    assert (
        tunnel.archive_member(
            "sample.gcode.3mf", "Metadata/plate_1.png", lambda data: None, 100
        )["bytes"]
        == 3
    )


def test_rtsp_snapshot_mock(config, monkeypatch):
    import sys

    from bambu_lan_cli import camera

    frame = Mock()
    frame.to_image.return_value.save.side_effect = lambda output, **kw: output.write(
        b"jpeg"
    )
    container = Mock()
    container.__enter__ = Mock(return_value=container)
    container.__exit__ = Mock()
    container.decode.return_value = iter([frame])
    av = Mock()
    av.open.return_value = container
    av.logging.Capture.return_value.__enter__ = Mock()
    av.logging.Capture.return_value.__exit__ = Mock()
    monkeypatch.setitem(sys.modules, "av", av)
    a = SimpleNamespace(via=None, timeout=1)
    assert camera.rtsp_snapshot(config, a) == b"jpeg"
    assert config.access_code in av.open.call_args.args[0]
    av.open.side_effect = RuntimeError(config.access_code)
    with pytest.raises(CliError) as exc:
        camera.rtsp_snapshot(config, a)
    assert config.access_code not in str(exc.value)


def test_no_cross_section_readonly_injection(config):
    connection = MqttConnection(config, 0.01)
    for payload in (
        {"info": {"command": "get_version", "stop": True}},
        {
            "info": {"command": "get_version"},
            "camera": {"command": "ipcam_record_set", "control": "disable"},
        },
    ):
        with pytest.raises(CliError):
            connection.publish(payload)


def test_ftps_retr_without_tls_unwrap():
    from bambu_lan_cli.ftps import ImplicitFTPS

    ftp = ImplicitFTPS()
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock()
    connection.recv.side_effect = [b"abc", b"def", b""]
    ftp.transfercmd = Mock(return_value=connection)
    ftp.voidcmd = Mock()
    ftp.voidresp = Mock(return_value="226 done")
    chunks = []
    assert ftp.retrbinary("RETR /sample.mp4", chunks.append) == "226 done"
    assert b"".join(chunks) == b"abcdef"
    connection.unwrap.assert_not_called()
