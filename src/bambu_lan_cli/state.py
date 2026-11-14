# SPDX-License-Identifier: AGPL-3.0-or-later
# State fields adapted from bpm (WTFPL-2.0) and ha-bambulab (MIT).
# Copyright (c) 2023 ha-bambulab contributors (MIT).
# See THIRD_PARTY_NOTICES.md for pinned revisions and notices.
from .hms import decode_errors


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
        units.append(
            {
                "id": unit.get("id"),
                "humidity_level": unit.get("humidity"),
                "humidity_percent": unit.get("humidity_raw"),
                "temperature_c": unit.get("temp"),
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


def summarize_status(config, state, version, count):
    device = state.get("device") or {}
    return {
        "mqtt": "ok",
        "model": config.model,
        "print_state": state.get("gcode_state"),
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
