# SPDX-License-Identifier: AGPL-3.0-or-later
import ftplib
import json
import struct
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bambu_lan_cli import cli, files, ftps
from bambu_lan_cli.ams import prepare_drying
from bambu_lan_cli.commands import build_payload
from bambu_lan_cli.errors import CliError
from bambu_lan_cli.protocol import protocol_payload
from bambu_lan_cli.safety import Authorization
from bambu_lan_cli.state import summarize_ams
from bambu_lan_cli.tunnel import BambuTunnelLocal, TunnelError


def raw_state(material="PETG", info="03", **tray_fields):
    return {
        "print": {
            "gcode_state": "IDLE",
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "info": info,
                        "humidity": "2",
                        "humidity_raw": "43",
                        "temp": "26.5",
                        "dry_time": 120,
                        "tray": [{"id": "0", "tray_type": material, **tray_fields}],
                    }
                ]
            },
        }
    }


def dry_body(*flags):
    return build_payload(
        cli.build_parser().parse_args(["ams-drying", "start", *flags])
    )[1]


def test_detect_petg_and_rfid_priority():
    body = prepare_drying(dry_body(), raw_state())
    assert (body["filament"], body["temp"], body["duration"]) == ("PETG", 65, 12)
    body = prepare_drying(dry_body(), raw_state(drying_temp="60", drying_time="7"))
    assert (body["temp"], body["duration"]) == (60, 7)
    body = prepare_drying(
        dry_body("--temperature", "50", "--duration", "3"),
        raw_state(drying_temp="60", drying_time="7"),
    )
    assert (body["temp"], body["duration"]) == (50, 3)


@pytest.mark.parametrize("info,expected", [("03", 65), ("04", 85)])
def test_model_limits_rfid_and_explicit(info, expected):
    for body, state in [
        (dry_body(), raw_state(info=info, drying_temp="99", drying_time="999")),
        (
            dry_body(
                "--temperature", "99", "--duration", "999", "--cooling-temp", "99"
            ),
            raw_state(info=info),
        ),
    ]:
        result = prepare_drying(body, state)
        assert (result["temp"], result["duration"]) == (expected, 24)
        assert result["cooling_temp"] <= expected


def test_conflicting_materials_and_presence():
    raw = raw_state()
    ams = raw["print"]["ams"]
    ams["ams"][0]["tray"].append({"id": "1", "tray_type": "PLA"})
    with pytest.raises(CliError, match="冲突"):
        prepare_drying(dry_body(), raw)
    assert prepare_drying(dry_body("--filament", "PETG"), raw)["filament"] == "PETG"
    ams["tray_exist_bits"] = "1"  # stale PLA metadata in an empty slot
    assert prepare_drying(dry_body(), raw)["filament"] == "PETG"
    ams["ams"][0]["tray"].append({"id": "2"})
    ams["tray_exist_bits"] = "5"
    with pytest.raises(CliError, match="未知"):
        prepare_drying(dry_body(), raw)


def test_conflicting_hints_unknown_material_or_model():
    raw = raw_state(drying_temp="60")
    raw["print"]["ams"]["ams"][0]["tray"].append(
        {"id": "1", "tray_type": "PETG", "drying_temp": "55"}
    )
    with pytest.raises(CliError, match="RFID"):
        prepare_drying(dry_body(), raw)
    with pytest.raises(CliError, match="无可靠"):
        prepare_drying(dry_body(), raw_state("OTHER"))
    assert (
        prepare_drying(
            dry_body("--temperature", "55", "--duration", "8"), raw_state("OTHER")
        )["filament"]
        == "OTHER"
    )
    with pytest.raises(CliError, match="型号"):
        prepare_drying(dry_body(), raw_state(info="01"))


@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_printing_guard_on_both_paths(config, monkeypatch, advanced, force):
    conn = Mock()
    raw = raw_state()
    raw["print"]["gcode_state"] = "RUNNING"
    conn.query_status.return_value = raw
    conn.command.return_value = {"result": "SUCCESS"}

    @contextmanager
    def connect(*_):
        yield conn

    monkeypatch.setattr(cli, "status_connection", connect)
    words = ["ams-drying", "start"]
    if advanced:
        body = dry_body("--filament", "PETG", "--temperature", "65", "--duration", "12")
        body.pop("command")
        body.pop("param")
        words = ["mqtt", "print.ams_filament_drying", "--params", json.dumps(body)]
    args = cli.build_parser().parse_args(
        words
        + ["--execute", "--confirm-serial", config.serial]
        + (["--force"] if force else [])
    )
    if force:
        assert cli.dispatch(config, args)["result"] == "SUCCESS"
        assert conn.command.call_args.args[0]["filament"] == "PETG"
    else:
        with pytest.raises(CliError, match="打印中"):
            cli.dispatch(config, args)
        conn.command.assert_not_called()
    conn.query_status.assert_called_once_with(raw=True)


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--execute"],
        ["--confirm-serial", "01P00A000000000"],
        ["--execute", "--confirm-serial", "wrong"],
    ],
)
@pytest.mark.parametrize("command", ["ams-drying", "mqtt"])
def test_force_never_bypasses_authorization(config, monkeypatch, flags, command):
    network = Mock(side_effect=AssertionError("network before authorization"))
    monkeypatch.setattr(cli, "status_connection", network)
    words = (
        ["ams-drying", "start"]
        if command == "ams-drying"
        else ["mqtt", "print.ams_filament_drying", "--params", "{}"]
    )
    args = cli.build_parser().parse_args(words + ["--force", *flags])
    with pytest.raises(CliError):
        cli.dispatch(config, args)
    network.assert_not_called()


def test_mode_two_is_not_guessed():
    body = dry_body("--temperature", "65", "--duration", "12", "--filament", "PETG")
    body.pop("command")
    body.pop("param")
    body["mode"] = 2
    with pytest.raises(CliError, match="未确认"):
        protocol_payload("print.ams_filament_drying", body)


def test_ams_humidity_and_drying_semantics():
    unit = raw_state(info="23")["print"]["ams"]
    summary = summarize_ams(unit)["units"][0]
    assert summary["model"] == "AMS 2 Pro"
    assert summary["humidity_level_raw"] == "2"
    assert summary["humidity_level"] == 4
    assert "湿度 43%，等级 4/5（越高越湿" in summary["humidity_description"]
    assert summary["drying_state"] == "烘干中"
    assert summary["drying_remaining_minutes"] == 120
    unit["ams"][0]["humidity"] = "5"
    assert summarize_ams(unit)["units"][0]["humidity_level"] == 1
    assert summarize_ams({"ams": [{"id": "0"}]})["units"][0]["humidity_level"] is None


def test_ams_status_readonly(config, monkeypatch):
    conn = Mock()
    conn.query_status.return_value = {"ams": {"units": []}}

    @contextmanager
    def connect(*_):
        yield conn

    monkeypatch.setattr(cli, "status_connection", connect)
    assert cli.dispatch(config, cli.build_parser().parse_args(["ams-status"])) == {
        "units": []
    }
    conn.command.assert_not_called()


@pytest.mark.parametrize("retry_success", [True, False])
def test_upload_verification_retry_no_false_success(
    config, monkeypatch, tmp_path, retry_success
):
    source = tmp_path / "sample.gcode.3mf"
    source.write_bytes(b"123")
    upload, bad, retry = Mock(), Mock(), Mock()
    upload.upload.return_value = {"bytes": 3}
    bad.list_models.side_effect = TunnelError("TCP 6000 响应帧类型无效：0x0003013f")
    retry.list_models.return_value = [
        {"path": "/userdata/model/sample.gcode.3mf", "size": 3 if retry_success else 2}
    ]
    sessions = iter([upload, bad, retry])

    @contextmanager
    def connect(*_):
        yield next(sessions)

    monkeypatch.setattr(files, "internal_connection", connect)
    args = SimpleNamespace(source=str(source), name=None, internal=True)
    if retry_success:
        result = files.upload_file(config, args, Authorization(True, config.serial))
        assert (
            result["verified_fresh_connection"] and result["verification_attempts"] == 2
        )
        assert "0x0003013f" in result["verification_warnings"][0]
    else:
        with pytest.raises(CliError, match="0x0003013f.*expected=3, actual=2"):
            files.upload_file(config, args, Authorization(True, config.serial))
    upload.upload.assert_called_once()


def test_invalid_frame_has_numeric_type(config):
    tunnel = BambuTunnelLocal(config)
    tunnel._read_exact = Mock(return_value=struct.pack("<IIII", 0, 0x0003013F, 0, 0))
    with pytest.raises(TunnelError, match="0x0003013f"):
        tunnel._receive()


@pytest.mark.parametrize(
    "error,connected,message",
    [
        (TimeoutError(), False, "连接/认证超时"),
        (TimeoutError(), True, "连接认证成功，但列表超时"),
        (
            ftplib.error_perm("550 private-name"),
            True,
            "路径不存在、无访问权限或外部存储未挂载",
        ),
        (ftplib.error_perm("530 private-name"), False, "认证失败"),
        (ftplib.error_temp("425 private-name"), True, "数据通道/存储暂不可用"),
    ],
)
def test_external_listing_errors_private(
    config, monkeypatch, error, connected, message
):
    client = Mock()
    client.root_list.side_effect = error
    monkeypatch.setattr(
        ftps,
        "open_ftps",
        Mock(return_value=client) if connected else Mock(side_effect=error),
    )
    with pytest.raises(CliError, match=message) as caught:
        ftps.list_external(config, 12, "/timelapse", 45)
    assert "private-name" not in str(caught.value)
    assert "media-ability" in str(caught.value)
    if connected:
        client.close.assert_called_once()
        client.sock.settimeout.assert_called_once_with(45)


def test_external_listing_timeout_forwarded(config, monkeypatch):
    operation = Mock(return_value={"entries": []})
    monkeypatch.setattr(files, "ftps_operation", operation)
    args = cli.build_parser().parse_args(["ls", "/timelapse", "--list-timeout", "90"])
    assert files.list_files(config, args) == {"entries": []}
    assert operation.call_args.kwargs["list_timeout"] == 90


@pytest.mark.parametrize(
    "failure", [TimeoutError(), TunnelError("LIST_INFO 失败，result=2")]
)
def test_upload_failed_verification_is_cli_error(
    config, monkeypatch, tmp_path, failure
):
    source = tmp_path / "sample.gcode.3mf"
    source.write_bytes(b"123")
    upload, check = Mock(), Mock()
    upload.upload.return_value = {"bytes": 3}
    check.list_models.side_effect = failure
    calls = []

    @contextmanager
    def connect(*_):
        calls.append(True)
        yield upload if len(calls) == 1 else check

    monkeypatch.setattr(files, "internal_connection", connect)
    with pytest.raises(CliError, match="不能确认成功") as caught:
        files.upload_file(
            config,
            SimpleNamespace(source=str(source), name=None, internal=True),
            Authorization(True, config.serial),
        )
    assert "第 1 次" in str(caught.value) and "第 2 次" in str(caught.value)
    assert ("超时" if isinstance(failure, TimeoutError) else "result=2") in str(
        caught.value
    )
    assert len(calls) == 3
    upload.upload.assert_called_once()


def test_tunnel_error_details_reach_cli(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *_: config)
    monkeypatch.setattr(
        cli,
        "dispatch",
        Mock(side_effect=TunnelError("TCP 6000 响应帧类型无效：0x0003013f")),
    )
    assert cli.main(["ls", "--internal"]) == 1
    assert "0x0003013f" in capsys.readouterr().err


def test_ssh_list_error_preserved(config, monkeypatch):
    message = "FTPS 连接认证成功，但列表超时（90 秒）；可加大 --list-timeout"
    monkeypatch.setattr(
        files.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"ok": False, "list_error": message}).encode(),
            )
        ),
    )
    args = SimpleNamespace(via="my-nas", timeout=12, list_timeout=90)
    with pytest.raises(CliError, match="连接认证成功，但列表超时"):
        files.ssh_ftps(config, args, "ls", "/timelapse")


@pytest.mark.parametrize("duration", ["0", "181", "nan", "inf"])
def test_invalid_list_timeout_before_network(config, monkeypatch, duration):
    network = Mock(side_effect=AssertionError("unexpected network"))
    monkeypatch.setattr(files, "ftps_operation", network)
    args = cli.build_parser().parse_args(
        ["ls", "/timelapse", "--list-timeout", duration]
    )
    with pytest.raises(CliError, match="list-timeout"):
        files.list_files(config, args)
    network.assert_not_called()


@pytest.mark.parametrize("model,info", [("AMS 2 Pro", "03"), ("AMS HT", "04")])
@pytest.mark.parametrize(
    "material,pro,ht",
    [
        ("PLA", (45, 12), (45, 12)),
        ("ABS", (65, 12), (80, 8)),
        ("ASA", (65, 12), (80, 8)),
        ("PA", (65, 12), (85, 12)),
        ("PC", (65, 12), (80, 8)),
        ("PVA", (65, 12), (85, 18)),
        ("TPU", (65, 12), (75, 18)),
    ],
)
def test_material_presets_by_model(model, info, material, pro, ht):
    result = prepare_drying(dry_body(), raw_state(material, info=info))
    assert (result["temp"], result["duration"]) == (ht if model == "AMS HT" else pro)


def test_tls_handshake_timeout_is_actionable(config, monkeypatch):
    client = Mock()
    client.connection_stage = "TCP 已连接，但 TLS 握手"
    client.connect.side_effect = TimeoutError()
    monkeypatch.setattr(ftps, "ImplicitFTPS", Mock(return_value=client))
    with pytest.raises(CliError, match="TCP 已连接，但 TLS 握手超时") as caught:
        ftps.open_ftps(config, 12)
    assert "尚未进入目录列表" in str(caught.value)
    assert "--timeout" in str(caught.value)
    client.close.assert_called_once()
