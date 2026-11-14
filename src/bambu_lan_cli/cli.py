# SPDX-License-Identifier: AGPL-3.0-or-later
import argparse
import json
import sys
from pathlib import Path

from .ams import prepare_drying
from .commands import WRITE_COMMANDS, build_payload
from .common import now_cst
from .config import init_config, load_config, validate_via
from .discovery import discover
from .errors import AckTimeout, CliError
from .files import (
    delete_file,
    directory_operation,
    download_file,
    file_info,
    internal_connection,
    list_files,
    rename_file,
    upload_file,
)
from .mqtt import status_connection
from .protocol import CATALOG, PRINT_OPTIONS, XCAM_MODULES, protocol_payload
from .safety import authorize, redact, remote_filename
from .tunnel import find_verified_model


def build_parser():
    parser = argparse.ArgumentParser(
        description="Bambu LAN CLI：默认只读，私密 YAML 配置"
    )

    # Common options accepted before or after subcommand. Suppress subparser defaults.
    def common(p, sub=False):
        default = argparse.SUPPRESS if sub else None
        p.add_argument("--config", default=default)
        p.add_argument("--printer", default=argparse.SUPPRESS if sub else "p2s")
        p.add_argument("--via", default=default)
        p.add_argument(
            "--timeout", type=float, default=argparse.SUPPRESS if sub else 12
        )
        p.add_argument(
            "--execute",
            action="store_true",
            default=argparse.SUPPRESS if sub else False,
        )
        p.add_argument("--confirm-serial", default=default)

    common(parser)
    subs = parser.add_subparsers(dest="command", required=True)

    def add(name):
        p = subs.add_parser(name)
        common(p, True)
        return p

    for c in (
        "status",
        "ams-status",
        "hms",
        "discover",
        "media-ability",
        "camera-status",
        "get-version",
        "info",
        "get-accessories",
        "protocol-list",
        "pause",
        "resume",
        "stop",
        "unload",
    ):
        p = add(c)
        if c in ("status", "ams-status", "camera-status", "info", "protocol-list"):
            p.add_argument(
                "--json", action="store_true", help="原始结构（敏感字段脱敏）"
            )
        if c == "unload":
            p.add_argument("--ams-id", type=int, default=0)
            p.add_argument("--slot", type=int, default=0)
            p.add_argument("--current-temp", type=int, default=0)
            p.add_argument("--temperature", type=int, default=0)
    p = add("config")
    p.add_argument("action", choices=["init", "check"])
    p = add("ls")
    p.add_argument(
        "--list-timeout",
        type=float,
        default=30,
        help="外部 FTPS 列表超时秒数，默认 30，最大 180；连接超时用 --timeout",
    )
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("--internal", action="store_true")
    p.add_argument("--kind", choices=["model", "timelapse", "video"], default="model")
    for c in ("download", "delete", "rename"):
        p = add(c)
        p.add_argument("remote_path")
        if c == "download":
            p.add_argument("destination", help="本地目标；拒绝覆盖")
            p.add_argument("--max-mib", type=int, default=2048)
            p.add_argument("--member", help="experimental：TCP 6000 SUB_FILE 归档成员")
        if c == "rename":
            p.add_argument("new_path")
        p.add_argument("--internal", action="store_true")
        p.add_argument(
            "--kind", choices=["model", "timelapse", "video"], default="timelapse"
        )
    for c in ("mkdir", "rmdir", "file-info"):
        p = add(c)
        p.add_argument("remote_path")
    p = add("project-member")
    p.add_argument("member")
    p.add_argument("destination")
    p = add("upload")
    p.add_argument("source")
    p.add_argument("--name")
    p.add_argument("--internal", action="store_true")
    p = add("print")
    p.add_argument("remote_file")
    p.add_argument("--plate", type=int, default=1)
    p.add_argument("--internal", action="store_true")
    p.add_argument("--timelapse", action="store_true")
    p.add_argument("--ams-mapping")
    for option in ("bed-leveling", "flow-cali", "vibration-cali"):
        p.add_argument(
            "--" + option, action=argparse.BooleanOptionalAction, default=True
        )
    p = add("speed")
    p.add_argument("level")
    p = add("light")
    p.add_argument("node", choices=["chamber_light", "chamber_light2", "heatbed_light"])
    p.add_argument("action", choices=["on", "off", "flashing"])
    for flag, default in (
        ("on-time", 500),
        ("off-time", 500),
        ("loops", 0),
        ("interval", 0),
    ):
        p.add_argument("--" + flag, type=int, default=default)
    p = add("fan")
    p.add_argument("fan", choices=["part", "aux", "chamber", "enhanced"])
    p.add_argument("percent", type=int)
    p = add("temp")
    p.add_argument("target", choices=["nozzle", "bed"])
    p.add_argument("temperature", type=int)
    p = add("airduct")
    p.add_argument("mode", type=int, choices=[0, 1, 2])
    p = add("gcode")
    p.add_argument("code")
    p = add("skip-objects")
    p.add_argument("ids")
    p = add("ams-control")
    p.add_argument("action", choices=["resume", "pause", "reset", "done", "abort"])
    for c in ("ams-rfid", "ams-drying", "ams-filament", "load"):
        p = add(c)
        p.add_argument("--ams-id", type=int, default=0)
        p.add_argument("--slot", type=int, default=0)
        if c == "ams-drying":
            p.add_argument("action", choices=["start", "stop"])
            p.add_argument(
                "--temperature", type=int, help="默认 RFID 提示或耗材预设；按型号钳位"
            )
            p.add_argument("--cooling-temp", type=int, default=45)
            p.add_argument(
                "--duration", type=int, help="小时，默认 RFID 提示或耗材预设；最大 24"
            )
            p.add_argument("--rotate-tray", action="store_true")
            p.add_argument("--filament", help="默认从该 AMS 已装载托盘检测")
            p.add_argument(
                "--force", action="store_true", help="显式允许打印中启动；仍需双重授权"
            )
            p.add_argument(
                "--close-power-conflict",
                action="store_true",
                help="显式同意关闭冲突电源",
            )
        elif c == "ams-filament":
            p.add_argument("--material", required=True)
            p.add_argument("--profile", required=True)
            p.add_argument("--color", required=True)
            p.add_argument("--min-temp", type=int, required=True)
            p.add_argument("--max-temp", type=int, required=True)
        elif c == "load":
            p.add_argument("--current-temp", type=int, default=0)
            p.add_argument("--temperature", type=int, default=220)
    p = add("clear-error")
    p.add_argument("--error-code", type=lambda s: int(s, 0), default=0)
    p.add_argument("--subtask-id", default="0")
    for c in ("camera-record", "camera-timelapse", "camera-capture"):
        p = add(c)
        p.add_argument("action", choices=["enable", "disable"])
    p = add("camera-resolution")
    p.add_argument("resolution", help="设备报告的 resolution_supported 值，例如 1080p")
    p = add("camera-delete-oldest")
    p.add_argument("--storage", choices=["internal", "sdcard", "usb"], required=True)
    p.add_argument("--total-layer", type=int, required=True)
    p = add("camera-snapshot")
    p.add_argument("destination")
    p.add_argument("--transport", choices=["auto", "jpeg", "rtsp"], default="auto")
    p = add("xcam")
    p.add_argument("module", choices=XCAM_MODULES)
    p.add_argument("action", choices=["enable", "disable"])
    p.add_argument("--sensitivity", choices=["never_halt", "low", "medium", "high"])
    p = add("print-option")
    p.add_argument("option", choices=PRINT_OPTIONS)
    p.add_argument("value", choices=["enable", "disable", "0", "1", "2"])
    p = add("calibration")
    p.add_argument(
        "--option", type=lambda s: int(s, 0), required=True, help="校准位掩码 0..127"
    )
    p = add("nozzle")
    p.add_argument(
        "--protocol", choices=["accessories", "set-nozzle"], default="accessories"
    )
    p.add_argument(
        "--diameter", type=float, choices=[0.2, 0.4, 0.6, 0.8], required=True
    )
    p.add_argument(
        "--material",
        choices=["hardened_steel", "stainless_steel"],
        default="hardened_steel",
    )
    p.add_argument("--id", type=int, default=0)
    p.add_argument("--type", default="HH01")
    p.add_argument("--wear", type=int, default=0)
    p = add("chamber")
    p.add_argument("temperature", type=int)
    p.add_argument(
        "--temper-check", action=argparse.BooleanOptionalAction, default=True
    )
    p = add("door")
    p.add_argument("mode", type=int, choices=[0, 1, 2])
    p = add("ams-settings")
    p.add_argument("--ams-id", type=int, default=0)
    for flag in ("calibrate-remain", "startup-read", "tray-read"):
        p.add_argument("--" + flag, action=argparse.BooleanOptionalAction, default=True)
    p = add("select-tool")
    p.add_argument("id", type=int, choices=[0, 1])
    p = add("mqtt")
    p.add_argument(
        "--force", action="store_true", help="显式允许打印中启动烘干；仍需双重授权"
    )
    p.add_argument(
        "name", choices=sorted(CATALOG), help="experimental；来源和字段见 protocol-list"
    )
    p.add_argument(
        "--params", required=True, help="来源 schema 内的 JSON 参数对象；无参数用 {}"
    )
    return parser


def observed_success(command, body, before, after):
    # Only explicit, relevant transitions can substitute for an absent ACK.
    if command == "speed":
        return str(after.get("speed_level")) == body["param"]
    expected = {"pause": "PAUSE", "resume": "RUNNING", "stop": "IDLE"}
    if command in expected:
        return after.get("print_state") == expected[command] and before.get(
            "print_state"
        ) != after.get("print_state")
    if command == "clear-error":
        return bool(before.get("print_error")) and after.get("print_error") == 0
    if command == "temp":
        key = "nozzle_target_c" if body["param"].startswith("M104") else "bed_target_c"
        return after.get(key) == int(body["param"].split(" S")[1])
    return False


def dispatch(config, args):
    if args.command == "protocol-list":
        return CATALOG
    advanced_write = args.command == "mqtt" and not CATALOG[args.name]["readonly"]
    if args.command in WRITE_COMMANDS or advanced_write:
        authorization = authorize(args, config)  # Gate before any file or network work.
        if args.command in ("mkdir", "rmdir"):
            return directory_operation(config, args, authorization)
        if args.command == "delete":
            return delete_file(config, args, authorization)
        if args.command == "rename":
            return rename_file(config, args, authorization)
        if args.command == "upload":
            return upload_file(config, args, authorization)
        section, body = build_payload(args)
        if args.command == "print" and args.internal:
            with internal_connection(config, args) as tunnel:
                verified = find_verified_model(
                    tunnel.list_models(), remote_filename(args.remote_file)
                )
            section, body = build_payload(args, url=verified["url"])
        with status_connection(config, args) as connection:
            if body.get("command") == "ams_filament_drying" and body.get("mode") != 0:
                raw = connection.query_status(raw=True)
                body = prepare_drying(body, raw, force=args.force)
                return connection.command(body, authorization, section)
            before = connection.query_status()
            if (
                args.command == "print"
                or body.get("command")
                in (
                    "project_file",
                    "gcode_file",
                    "calibration",
                    "extrusion_cali",
                    "flowrate_cali",
                )
            ) and before.get("print_state") not in (
                "IDLE",
                "FINISH",
                "FAILED",
            ):
                raise CliError("存在任务或状态未知，拒绝启动打印")
            if body.get("command") == "resume" and before.get("print_state") != "PAUSE":
                raise CliError("仅 PAUSE 状态允许恢复")
            try:
                return connection.command(body, authorization, section)
            except AckTimeout:
                after = connection.query_status()
                if observed_success(args.command, body, before, after):
                    return {
                        "command": body.get("command", "update.rename"),
                        "result": "verified_by_status",
                        "retried": False,
                    }
                raise CliError(
                    "命令已发送，未收到 ACK；状态未能确认结果，未重试，请检查设备"
                ) from None
    if args.command == "mqtt":
        section, body = protocol_payload(args.name, args.params)
        with status_connection(config, args) as connection:
            return connection.query(section, body)
    if args.command in ("get-version", "info", "get-accessories"):
        with status_connection(config, args) as connection:
            return (
                connection.query(
                    "system", {"command": "get_accessories", "accessory_type": "none"}
                )
                if args.command == "get-accessories"
                else connection.query("info", {"command": "get_version"})
            )
    if args.command == "file-info":
        return file_info(config, args)
    if args.command == "project-member":
        from .files import save_download

        with internal_connection(config, args) as tunnel:
            return save_download(
                args.destination, lambda sink: tunnel.project_member(args.member, sink)
            )
    if args.command == "camera-snapshot":
        from .camera import snapshot

        return snapshot(config, args)
    if args.command == "download":
        return download_file(config, args)
    if args.command in ("status", "ams-status", "hms", "camera-status"):
        with status_connection(config, args) as connection:
            if getattr(args, "json", False):
                raw = connection.query_status(raw=True)
                return (
                    raw
                    if args.command == "status"
                    else raw["print"].get(
                        "ams" if args.command == "ams-status" else "ipcam", {}
                    )
                )
            status = connection.query_status()
            if args.command == "camera-status":
                return status["ipcam"]
            if args.command == "ams-status":
                return status["ams"]
            return status if args.command == "status" else {"errors": status["errors"]}
    if args.command == "ls":
        return list_files(config, args)
    if args.command == "media-ability":
        with internal_connection(config, args) as tunnel:
            return tunnel.media_ability()
    if args.command == "discover":
        return discover(args.timeout, config)
    raise CliError("不支持的命令")


def main(argv=None):
    config = None

    # Parse errors use generic messages so an invalid argument cannot echo a secret.
    class PrivateParser(argparse.ArgumentParser):
        def error(self, message):
            raise CliError("参数无效，请使用 --help 查看用法")

    try:
        parser = build_parser()
        # Override all parser errors including subcommands.
        for p in [
            parser,
            *next(
                a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
            ).choices.values(),
        ]:
            p.error = PrivateParser.error.__get__(p)
        args = parser.parse_args(argv)
        if not 0 < args.timeout <= 60:
            raise CliError("timeout 必须在 0–60 秒之间")
        if args.command == "protocol-list":
            result = CATALOG
        elif args.command == "config" and args.action == "init":
            result = init_config(
                args.config or Path.home() / ".config/bambu/config.yaml"
            )
        else:
            config = load_config(args.config, args.printer)
            args.via = args.via if args.via is not None else config.ssh_via
            validate_via(args.via)
            result = (
                {"config": "valid", "permissions": "0600"}
                if args.command == "config"
                else dispatch(config, args)
            )
        output = {"time": now_cst(), "result": result}
        # This trusted, local schema contains type names, not device values.
        print(
            json.dumps(output, ensure_ascii=False)
            if args.command == "protocol-list"
            else redact(output, config)
        )
        return 0
    except KeyboardInterrupt:
        print('{"error":"已中断；写入结果可能未知，勿自动重试"}', file=sys.stderr)
        return 130
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, CliError)
            else type(exc).__name__ + "；检查网络、开发者模式与本地配置"
        )
        print(redact({"error": message}, config), file=sys.stderr)
        return 1
