# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-indexed MQTT schemas; see protocol_catalog.json and THIRD_PARTY_NOTICES.

The advanced interface deliberately requires explicit parameters. Examples from
upstream are schemas, never executable defaults. No automatic firmware fallback.
"""

import json
import math
import re
from importlib.resources import files

from .errors import CliError

CATALOG = json.loads(
    files("bambu_lan_cli").joinpath("protocol_catalog.json").read_text()
)
XCAM_MODULES = (
    "printing_monitor",
    "spaghetti_detector",
    "buildplate_marker_detector",
    "first_layer_inspector",
    "pileup_detector",
    "clump_detector",
    "airprint_detector",
    "plate_offset_switch",
    "fod_check",
    "model_movement_check",
)
PRINT_OPTIONS = tuple(
    k for k in CATALOG["print.print_option"]["fields"] if k != "option"
)


def protocol_payload(name, params):
    if name not in CATALOG:
        raise CliError("未收录的协议命令；请查看 protocol-list")
    spec = CATALOG[name]
    try:
        body = json.loads(params) if isinstance(params, str) else params
    except ValueError:
        raise CliError("params 必须为 JSON 对象") from None
    if not isinstance(body, dict) or len(json.dumps(body)) > 65536:
        raise CliError("params 须为不超过 64 KiB 的 JSON 对象")
    if set(body) - spec["fields"].keys() or not set(spec["required"]).issubset(body):
        raise CliError("参数字段缺失或不在来源 schema 中；请查看 protocol-list --json")
    for key, value in body.items():
        if type(value).__name__ not in spec["fields"][key]:
            raise CliError("协议字段类型不匹配")
    validate_values(body)
    limits = {
        "print.set_bed_temp": {"temp": (0, 110)},
        "print.calibration": {"option": (0, 127)},
    }
    for key, (low, high) in limits.get(name, {}).items():
        if key in body and not low <= body[key] <= high:
            raise CliError("协议数值超出安全范围")
    if name == "print.ams_filament_drying":
        if body.get("mode") == 1 and (
            not 45 <= body.get("temp", 0) <= 65
            or not 1 <= body.get("duration", 0) <= 24
        ):
            raise CliError("烘干温度或时长超出安全范围")
    if (
        name == "camera.ipcam_get_media_info"
        and body["sub_command"] != "is_timelapse_storage_enough"
    ):
        raise CliError("不支持的媒体只读查询")
    return spec["section"], {
        **body,
        **({"command": spec["command"]} if spec["command"] else {}),
    }


def validate_values(value, key=""):
    """Reject control characters, invalid floats and unsafe thermal values recursively."""
    if isinstance(value, dict):
        for k, v in value.items():
            validate_values(v, k)
    elif isinstance(value, list):
        if len(value) > 256:
            raise CliError("协议列表过长")
        for v in value:
            validate_values(v, key)
    elif isinstance(value, str):
        if "\x00" in value or len(value) > 8192:
            raise CliError("协议字符串过长或含 NUL")
    elif isinstance(value, float) and not math.isfinite(value):
        raise CliError("协议数字必须为有限值")
    bounds = {
        "target_temp": (0, 300),
        "nozzle_temp": (0, 300),
        "nozzle_temp_min": (0, 300),
        "nozzle_temp_max": (0, 300),
        "bed_temp": (0, 110),
        "ctt_val": (0, 60),
        "total_layer": (1, 100000),
        "cooling_temp": (45, 65),
    }
    if key in bounds:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise CliError("协议数值无效") from None
        if not bounds[key][0] <= number <= bounds[key][1]:
            raise CliError("协议数值超出安全范围")


def readonly_payload(payload):
    if not isinstance(payload, dict) or len(payload) != 1:
        return False
    section, body = next(iter(payload.items()))
    if not isinstance(body, dict):
        return False
    name = section + "." + str(body.get("command", ""))
    if name not in CATALOG or not CATALOG[name]["readonly"]:
        return False
    params = {k: v for k, v in body.items() if k not in ("command", "sequence_id")}
    try:
        protocol_payload(name, params)
    except CliError:
        return False
    return True


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise CliError("参数须为安全的协议标识符")
    return value
