# SPDX-License-Identifier: AGPL-3.0-or-later
import argparse
import sys
from pathlib import Path

from .commands import WRITE_COMMANDS, build_payload
from .common import now_cst
from .config import init_config, load_config, validate_via
from .discovery import discover
from .errors import AckTimeout, CliError
from .files import internal_connection, list_files, upload_file
from .mqtt import status_connection
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
        "hms",
        "discover",
        "media-ability",
        "pause",
        "resume",
        "stop",
        "unload",
    ):
        p = add(c)
        if c == "unload":
            p.add_argument("--ams-id", type=int, default=0)
            p.add_argument("--slot", type=int, default=0)
            p.add_argument("--current-temp", type=int, default=0)
            p.add_argument("--temperature", type=int, default=0)
    p = add("config")
    p.add_argument("action", choices=["init", "check"])
    p = add("ls")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("--internal", action="store_true")
    p.add_argument("--kind", choices=["model", "timelapse"], default="model")
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
    p.add_argument("action", choices=["on", "off"])
    p = add("fan")
    p.add_argument("fan", choices=["part", "aux", "chamber"])
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
    p.add_argument("action", choices=["resume", "pause", "reset", "done"])
    for c in ("ams-rfid", "ams-drying", "ams-filament", "load"):
        p = add(c)
        p.add_argument("--ams-id", type=int, default=0)
        p.add_argument("--slot", type=int, default=0)
        if c == "ams-drying":
            p.add_argument("action", choices=["start", "stop"])
            p.add_argument("--temperature", type=int, default=55)
            p.add_argument("--cooling-temp", type=int, default=45)
            p.add_argument("--duration", type=int, default=4, help="小时，1..24")
            p.add_argument("--rotate-tray", action="store_true")
            p.add_argument("--filament", default="PLA")
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
    if args.command in WRITE_COMMANDS:
        authorization = authorize(args, config)  # Gate before any file or network work.
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
            before = connection.query_status()
            if args.command == "print" and before.get("print_state") not in (
                "IDLE",
                "FINISH",
                "FAILED",
            ):
                raise CliError("存在任务或状态未知，拒绝启动打印")
            if args.command == "resume" and before.get("print_state") != "PAUSE":
                raise CliError("仅 PAUSE 状态允许恢复")
            try:
                return connection.command(body, authorization, section)
            except AckTimeout:
                after = connection.query_status()
                if observed_success(args.command, body, before, after):
                    return {
                        "command": body["command"],
                        "result": "verified_by_status",
                        "retried": False,
                    }
                raise CliError(
                    "命令已发送，未收到 ACK；状态未能确认结果，未重试，请检查设备"
                ) from None
    if args.command in ("status", "hms"):
        with status_connection(config, args) as connection:
            status = connection.query_status()
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
        if args.command == "config" and args.action == "init":
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
        print(redact({"time": now_cst(), "result": result}, config))
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
