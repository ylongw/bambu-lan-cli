# SPDX-License-Identifier: AGPL-3.0-or-later
# State fields adapted from bpm (WTFPL-2.0) and ha-bambulab (MIT).
# Copyright (c) 2023 ha-bambulab contributors (MIT).
# See THIRD_PARTY_NOTICES.md for pinned revisions and notices.
import json
from datetime import datetime, timedelta
from importlib.resources import files
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .ams import decode_info, number
from .hms import decode_errors

STAGES = json.loads(files("bambu_lan_cli").joinpath("stages.json").read_text())


def summarize_tray(tray):
    if not isinstance(tray, dict):
        return None
    keys = {
        "id": "id",
        "tray_type": "type",
        "tray_sub_brands": "name",
        "tray_color": "color",
        "remain": "remain_percent",
        "nozzle_temp_min": "nozzle_min_c",
        "nozzle_temp_max": "nozzle_max_c",
        "tray_info_idx": "filament_id",
        "drying_temp": "drying_temperature_hint_c",
        "drying_time": "drying_duration_hint_hours",
    }
    out = {
        new: tray[old]
        for old, new in keys.items()
        if old in tray and tray[old] not in ("", None)
    }
    out["empty"] = not tray.get("tray_type")
    return out


def chamber_temperature(state):
    # P2S 腔温报告在 device.ctc.info.temp。
    if state.get("chamber_temper") is not None:
        return state.get("chamber_temper")
    try:
        return state["device"]["ctc"]["info"]["temp"]
    except (KeyError, TypeError):
        return None


def external_spool(state):
    # 旧固件为 vt_tray；P2S 新固件为 vir_slot 列表。
    if isinstance(state.get("vt_tray"), dict):
        return summarize_tray(state["vt_tray"])
    slots = state.get("vir_slot")
    if isinstance(slots, list) and slots and isinstance(slots[0], dict):
        return summarize_tray(slots[0])
    return None


def summarize_ams(ams):
    """AMS 只读摘要：每个单元的湿度/温度与各料槽耗材。"""
    if not isinstance(ams, dict):
        return None
    units = []
    for unit in ams.get("ams", []) or []:
        if not isinstance(unit, dict):
            continue
        raw_level = number(unit.get("humidity"))
        level = 6 - raw_level if raw_level in range(1, 6) else None
        percent = unit.get("humidity_raw")
        units.append(
            {
                "id": unit.get("id"),
                **decode_info(unit),
                "humidity_level_raw": unit.get("humidity"),
                "humidity_level": level,
                "humidity_percent": percent,
                "humidity_description": f"湿度 {percent if percent is not None else '未知'}%，等级 {level if level is not None else '未知'}/5（越高越湿；Studio/HA 约定）",
                "drying_remaining_minutes": unit.get("dry_time"),
                "temperature_c": unit.get("temp"),
                "drying": {
                    k: v for k, v in unit.items() if k.startswith(("dry", "heat"))
                },
                "trays": [
                    summarize_tray(t)
                    for t in unit.get("tray", []) or []
                    if isinstance(t, dict)
                ],
            }
        )
    return {
        "units": units,
        "tray_now": ams.get("tray_now"),
        "tray_tar": ams.get("tray_tar"),
    }


def camera_status(state):
    camera = state.get("ipcam") or {}
    out = {k: v for k, v in camera.items() if k != "rtsp_url"}
    # Do not forward a device-supplied URL: userinfo, tokens and query strings
    # can contain credentials. Only report the transport and availability.
    raw = camera.get("rtsp_url", "")
    try:
        scheme = urlsplit(raw).scheme
    except ValueError:
        scheme = ""
    out["rtsp"] = {"available": scheme in ("rtsp", "rtsps"), "scheme": scheme or None}
    out["storage"] = (state.get("device") or {}).get("cam")
    return out


def job_status(state, now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    remaining = state.get("mc_remaining_time", state.get("remain_time"))
    eta = None
    if state.get("gcode_state") in ("RUNNING", "PREPARE"):
        try:
            if 0 <= float(remaining) <= 525600:
                eta = (
                    (now + timedelta(minutes=float(remaining)))
                    .astimezone(ZoneInfo("Asia/Shanghai"))
                    .isoformat(timespec="seconds")
                )
        except (TypeError, ValueError, OverflowError):
            pass
    return {
        "name": state.get("subtask_name") or state.get("gcode_file"),
        "progress_percent": state.get("mc_percent", state.get("percent")),
        "current_layer": state.get("layer_num"),
        "total_layers": state.get("total_layer_num"),
        "remaining_minutes": remaining,
        "estimated_finish_cst": eta,
        "eta_note": "暂停或未打印时不推算完成时间" if eta is None else "按剩余分钟估计",
        "stage": state.get("stg_cur", state.get("mc_print_stage")),
        "stage_name": STAGES.get(
            str(state.get("stg_cur", state.get("mc_print_stage"))), "unknown"
        ),
        "stage_history": state.get("stg"),
        "mc_print_sub_stage": state.get("mc_print_sub_stage"),
    }


def summarize_status(config, state, version, count):
    device = state.get("device") or {}
    return {
        "mqtt": "ok",
        "model": config.model,
        "print_state": state.get("gcode_state"),
        "job": job_status(state),
        "ipcam": camera_status(state),
        "xcam": state.get("xcam"),
        "xcam_status": state.get("xcam_status"),
        "storage": {
            k: v for k, v in state.items() if k.startswith(("sdcard", "storage", "usb"))
        },
        "device": device,
        "speed_name": {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}.get(
            int(state.get("spd_lvl") or 0)
        )
        if str(state.get("spd_lvl") or 0).isdigit()
        else None,
        "speed_percent": state.get("spd_mag"),
        "nozzle_temperature_c": state.get("nozzle_temper"),
        "nozzle_target_c": state.get("nozzle_target_temper"),
        "bed_temperature_c": state.get("bed_temper"),
        "bed_target_c": state.get("bed_target_temper"),
        "chamber_temperature_c": chamber_temperature(state),
        "airduct": device.get("airduct"),
        "speed_level": state.get("spd_lvl"),
        "fans": {
            k: state.get(k)
            for k in (
                "cooling_fan_speed",
                "big_fan1_speed",
                "big_fan2_speed",
                "heatbreak_fan_speed",
            )
        },
        "lights": state.get("lights_report"),
        "external_spool": external_spool(state),
        "ams": summarize_ams(state.get("ams")),
        "print_error": state.get("print_error"),
        "errors": decode_errors(state),
        "report_count": count,
        "firmware": [
            {k: m[k] for k in ("name", "sw_ver", "hw_ver") if k in m}
            for m in version.get("module", [])
            if isinstance(m, dict)
        ],
    }
