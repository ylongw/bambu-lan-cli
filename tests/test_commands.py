# SPDX-License-Identifier: AGPL-3.0-or-later
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from bambu_lan_cli.cli import build_parser, dispatch, main
from bambu_lan_cli.commands import WRITE_COMMANDS, build_payload, speed_level
from bambu_lan_cli.errors import AckTimeout, CliError
from bambu_lan_cli.safety import redact

CASES = [
    ["print", "sample.gcode.3mf"],
    ["upload", "sample.gcode.3mf"],
    ["pause"],
    ["resume"],
    ["stop"],
    ["skip-objects", "1,3"],
    ["speed", "standard"],
    ["light", "chamber_light2", "on"],
    ["fan", "aux", "50"],
    ["temp", "nozzle", "220"],
    ["airduct", "1"],
    ["gcode", "G28"],
    ["ams-control", "resume"],
    ["ams-rfid"],
    ["ams-drying", "start"],
    [
        "ams-filament",
        "--material",
        "PLA",
        "--profile",
        "GFA00",
        "--color",
        "FFFFFFFF",
        "--min-temp",
        "190",
        "--max-temp",
        "230",
    ],
    ["load"],
    ["unload"],
    ["clear-error", "--error-code", "0x07008006"],
]


def test_all_write_commands_covered():
    assert {x[0] for x in CASES} == WRITE_COMMANDS


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--execute"],
        ["--confirm-serial", "01P00A000000000"],
        ["--execute", "--confirm-serial", "wrong"],
    ],
)
def test_all_write_gates(case, flags, config, monkeypatch):
    import bambu_lan_cli.cli as cli

    network = Mock(side_effect=AssertionError("network touched before gate"))
    monkeypatch.setattr(cli, "status_connection", network)
    monkeypatch.setattr(cli, "internal_connection", network)
    monkeypatch.setattr(cli, "upload_file", network)
    args = build_parser().parse_args(case + flags)
    with pytest.raises(CliError):
        dispatch(config, args)
    network.assert_not_called()


@pytest.mark.parametrize("case", [c for c in CASES if c[0] != "upload"])
def test_payloads(case):
    args = build_parser().parse_args(case)
    section, body = build_payload(args)
    assert section in ("print", "system") and "param" in body and "command" in body


@pytest.mark.parametrize(
    "value", [True, False, 1, 1.0, None, {}, "True", "1.0", "5", " 1"]
)
def test_speed_confusion(value):
    with pytest.raises(CliError):
        speed_level(value)


@pytest.mark.parametrize(
    "value,expected", [("silent", "1"), ("STANDARD", "2"), ("sport", "3"), ("4", "4")]
)
def test_speed(value, expected):
    assert speed_level(value) == expected


def test_exact_payloads():
    parser = build_parser()

    def payload(*words):
        return build_payload(parser.parse_args(words))[1]

    assert payload("resume") == {"command": "resume", "param": ""}
    assert payload("fan", "chamber", "100")["param"] == "M106 P3 S255\n"
    assert payload("skip-objects", "1,3")["obj_list"] == [1, 3]
    assert payload("clear-error")["command"] == "clean_print_error"
    assert payload("airduct", "1")["modeId"] == 1
    assert payload("light", "heatbed_light", "on")["led_node"] == "heatbed_light"
    dry = payload("ams-drying", "start", "--close-power-conflict")
    assert (
        dry["mode"] == 1
        and dry["close_power_conflict"] is True
        and dry["duration"] == 4
    )
    assert payload("ams-drying", "stop")["mode"] == 0
    mapped = payload(
        "print",
        "sample.gcode.3mf",
        "--ams-mapping",
        "0,5",
        "--plate",
        "2",
        "--timelapse",
        "--no-flow-cali",
    )
    assert mapped["ams_mapping2"] == [
        {"ams_id": 0, "slot_id": 0},
        {"ams_id": 1, "slot_id": 1},
    ]
    assert (
        mapped["param"] == "Metadata/plate_2.gcode"
        and mapped["timelapse"]
        and not mapped["flow_cali"]
    )


@pytest.mark.parametrize(
    "case",
    [
        ["temp", "bed", "111"],
        ["temp", "nozzle", "301"],
        ["temp", "nozzle", "-1"],
        ["fan", "aux", "101"],
        ["print", "../sample.gcode.3mf"],
        ["print", "sample.gcode.3mf", "--ams-mapping", "999"],
        ["skip-objects", "1,1"],
        ["load", "--ams-id", "136"],
        ["ams-drying", "start", "--temperature", "70"],
    ],
)
def test_reject_unsafe(case):
    with pytest.raises(CliError):
        build_payload(build_parser().parse_args(case))


def test_read_parsers():
    for words in [
        ["status"],
        ["hms"],
        ["ls"],
        ["ls", "/timelapse"],
        ["ls", "--internal"],
        ["discover"],
        ["media-ability"],
        ["config", "check"],
    ]:
        assert build_parser().parse_args(words).execute is False
    assert build_parser().parse_args(["--timeout", "3", "status"]).timeout == 3
    assert build_parser().parse_args(["status", "--timeout", "4"]).timeout == 4


def test_redact(config):
    for secret in (config.host, config.serial, config.access_code):
        assert secret not in redact({"error": secret}, config)
    assert config.access_code not in repr(config)


def test_no_ack_no_retry(config, monkeypatch):
    import bambu_lan_cli.cli as cli

    conn = Mock()
    conn.query_status.side_effect = [
        {"print_state": "PAUSE"},
        {"print_state": "RUNNING"},
    ]
    conn.command.side_effect = AckTimeout()

    @contextmanager
    def connect(*args):
        yield conn

    monkeypatch.setattr(cli, "status_connection", connect)
    args = build_parser().parse_args(
        ["resume", "--execute", "--confirm-serial", config.serial]
    )
    assert dispatch(config, args)["retried"] is False
    assert conn.command.call_count == 1 and conn.query_status.call_count == 2


def test_no_ack_unknown(config, monkeypatch):
    import bambu_lan_cli.cli as cli

    conn = Mock()
    conn.query_status.return_value = {"print_state": "PAUSE"}
    conn.command.side_effect = AckTimeout()

    @contextmanager
    def connect(*args):
        yield conn

    monkeypatch.setattr(cli, "status_connection", connect)
    with pytest.raises(CliError):
        dispatch(
            config,
            build_parser().parse_args(
                ["resume", "--execute", "--confirm-serial", config.serial]
            ),
        )
    assert conn.command.call_count == 1


def test_paused_print_refused(config, monkeypatch):
    import bambu_lan_cli.cli as cli

    conn = Mock()
    conn.query_status.return_value = {"print_state": "PAUSE"}

    @contextmanager
    def connect(*args):
        yield conn

    monkeypatch.setattr(cli, "status_connection", connect)
    with pytest.raises(CliError):
        dispatch(
            config,
            build_parser().parse_args(
                [
                    "print",
                    "sample.gcode.3mf",
                    "--execute",
                    "--confirm-serial",
                    config.serial,
                ]
            ),
        )
    conn.command.assert_not_called()


def test_cli_error_no_echo(capsys):
    assert main(["--invalid-private-marker"]) == 1
    assert "private-marker" not in capsys.readouterr().err
