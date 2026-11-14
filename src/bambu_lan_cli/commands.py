# SPDX-License-Identifier: AGPL-3.0-or-later
# Adapted command schemas: synman/bambu-printer-manager (WTFPL-2.0)
# and Copyright (c) 2023 ha-bambulab contributors (MIT).
# See THIRD_PARTY_NOTICES.md for full notices and pinned revisions.
from .errors import CliError
from .protocol import protocol_payload, token
from .safety import remote_filename

WRITE_COMMANDS = {
    "print",
    "upload",
    "pause",
    "resume",
    "stop",
    "skip-objects",
    "speed",
    "light",
    "fan",
    "temp",
    "airduct",
    "gcode",
    "ams-control",
    "ams-rfid",
    "ams-drying",
    "ams-filament",
    "load",
    "unload",
    "clear-error",
    "camera-record",
    "camera-timelapse",
    "camera-resolution",
    "camera-capture",
    "camera-delete-oldest",
    "xcam",
    "print-option",
    "calibration",
    "nozzle",
    "chamber",
    "door",
    "ams-settings",
    "select-tool",
    "delete",
    "rename",
    "mkdir",
    "rmdir",
}


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise CliError("整数参数超出安全范围")
    return value


def speed_level(value):
    levels = {
        "silent": "1",
        "standard": "2",
        "sport": "3",
        "ludicrous": "4",
        **{str(i): str(i) for i in range(1, 5)},
    }
    if type(value) is not str or value.lower() not in levels:
        raise CliError("速度须为 silent/standard/sport/ludicrous 或 1..4")
    return levels[value.lower()]


def ams_mapping(value):
    if not value:
        return {
            "use_ams": False,
            "ams_mapping": [-1],
            "ams_mapping2": [{"ams_id": 255, "slot_id": 0}],
        }
    try:
        if not isinstance(value, str):
            raise ValueError
        mapping = [int(v.strip()) for v in value.split(",")]
    except ValueError:
        raise CliError("AMS 映射须为逗号分隔的整数") from None
    if not 1 <= len(mapping) <= 64 or any(
        not (0 <= v <= 103 or v in (-1, 254, 255) or 128 <= v <= 135) for v in mapping
    ):
        raise CliError("AMS 映射编号无效")

    def decode(v):
        if v == -1:
            return {"ams_id": 255, "slot_id": 255}
        if v >= 128:
            return {"ams_id": v, "slot_id": 0}
        return {"ams_id": v // 4, "slot_id": v % 4}

    return {
        "use_ams": any(v not in (-1, 254, 255) for v in mapping),
        "ams_mapping": mapping,
        "ams_mapping2": [decode(v) for v in mapping],
    }


def build_payload(a, *, url=None):
    c = a.command
    section, body = "print", {"command": c.replace("-", "_"), "param": ""}
    if c in ("camera-record", "camera-timelapse", "camera-capture"):
        section = "camera"
        body = {
            "command": {
                "camera-record": "ipcam_record_set",
                "camera-timelapse": "ipcam_timelapse",
                "camera-capture": "ipcam_cap_pic_set",
            }[c],
            "control": a.action,
        }
    elif c == "camera-resolution":
        section, body = (
            "camera",
            {"command": "ipcam_resolution_set", "resolution": token(a.resolution)},
        )
    elif c == "camera-delete-oldest":
        section, body = (
            "camera",
            {
                "command": "ipcam_delete_oldest_timelapse",
                "storage": a.storage,
                "total_layer": integer(a.total_layer, 1, 100000),
            },
        )
    elif c == "xcam":
        section, body = (
            "xcam",
            {
                "command": "xcam_control_set",
                "module_name": a.module,
                "control": a.action == "enable",
                "enable": a.action == "enable",
                "print_halt": True,
            },
        )
        if a.sensitivity:
            body["halt_print_sensitivity"] = a.sensitivity
    elif c == "print-option":
        if (
            a.option in ("air_purification", "nozzle_blob_detect_v2")
            and a.value not in ("0", "1", "2")
        ) or (
            a.option not in ("air_purification", "nozzle_blob_detect_v2")
            and a.value == "2"
        ):
            raise CliError("布尔选项只接受 enable/disable/0/1；枚举选项接受 0/1/2")
        enabled = a.value in ("enable", "1")
        body = {
            "command": "print_option",
            a.option: integer(int(a.value), 0, 2)
            if a.option in ("air_purification", "nozzle_blob_detect_v2")
            else enabled,
        }
        if a.option == "auto_recovery":
            body["option"] = int(enabled)
    elif c == "calibration":
        body = {"command": "calibration", "option": integer(a.option, 0, 127)}
    elif c == "nozzle":
        if a.protocol == "accessories":
            section, body = (
                "system",
                {
                    "command": "set_accessories",
                    "accessory_type": "nozzle",
                    "nozzle_diameter": a.diameter,
                    "nozzle_type": a.material,
                },
            )
        else:
            body = {
                "command": "set_nozzle",
                "id": integer(a.id, 0, 255),
                "diameter": a.diameter,
                "type": token(a.type),
                "wear": integer(a.wear, 0, 255),
            }
    elif c == "chamber":
        body = {
            "command": "set_ctt",
            "ctt_val": integer(a.temperature, 0, 60),
            "temper_check": a.temper_check,
        }
    elif c == "door":
        section, body = (
            "system",
            {"command": "set_door_stat", "config": integer(a.mode, 0, 2)},
        )
    elif c == "ams-settings":
        body = {
            "command": "ams_user_setting",
            "ams_id": integer(a.ams_id, -1, 135),
            "calibrate_remain_flag": a.calibrate_remain,
            "startup_read_option": a.startup_read,
            "tray_read_option": a.tray_read,
        }
    elif c == "select-tool":
        body = {"command": "select_extruder", "extruder_index": integer(a.id, 0, 1)}
    elif c == "mqtt":
        return protocol_payload(a.name, a.params)
    elif c in ("pause", "resume", "stop"):
        pass
    elif c == "print":
        name = remote_filename(a.remote_file)
        body.update(
            command="project_file",
            param=f"Metadata/plate_{integer(a.plate, 1, 99)}.gcode",
            url=url or "ftp:///" + name,
            subtask_name=name.removesuffix(".gcode.3mf"),
            md5="from_sd_card" if a.internal else "",
            project_id="0",
            profile_id="0",
            task_id="0",
            subtask_id="0",
            bed_type="auto",
            timelapse=a.timelapse,
            bed_leveling=a.bed_leveling,
            flow_cali=a.flow_cali,
            vibration_cali=a.vibration_cali,
            layer_inspect=False,
            **ams_mapping(a.ams_mapping),
        )
        if a.internal:
            body["file"] = name
    elif c == "speed":
        body.update(command="print_speed", param=speed_level(a.level))
    elif c == "light":
        if a.node not in (
            "chamber_light",
            "chamber_light2",
            "heatbed_light",
        ) or a.action not in ("on", "off", "flashing"):
            raise CliError("灯光参数无效")
        section = "system"
        body.update(
            command="ledctrl",
            led_node=a.node,
            led_mode=a.action,
            led_on_time=integer(
                a.on_time
                if a.on_time is not None
                else (0 if a.node == "heatbed_light" else 500),
                0,
                60000,
            ),
            led_off_time=integer(
                a.off_time
                if a.off_time is not None
                else (0 if a.node == "heatbed_light" else 500),
                0,
                60000,
            ),
            loop_times=integer(a.loops, 0, 10000),
            interval_time=integer(a.interval, 0, 60000),
        )
    elif c == "fan":
        if a.fan not in ("part", "aux", "chamber", "enhanced"):
            raise CliError("风扇参数无效")
        body.update(
            command="gcode_line",
            param=f"M106 P{dict(part=1, aux=2, chamber=3, enhanced=9)[a.fan]} S{round(integer(a.percent, 0, 100) * 255 / 100)}\n",
        )
    elif c == "temp":
        if a.target not in ("nozzle", "bed"):
            raise CliError("温度目标无效")
        body.update(
            command="gcode_line",
            param=f"{'M104' if a.target == 'nozzle' else 'M140'} S{integer(a.temperature, 0, 300 if a.target == 'nozzle' else 110)}\n",
        )
    elif c == "airduct":
        body.update(command="set_airduct", modeId=integer(a.mode, 0, 2), submode=-1)
    elif c == "gcode":
        if (
            not isinstance(a.code, str)
            or not a.code.strip()
            or len(a.code) > 8192
            or "\x00" in a.code
        ):
            raise CliError("G-code 为空、过长或含非法字符")
        body.update(command="gcode_line", param=a.code.rstrip() + "\n")
    elif c == "skip-objects":
        try:
            ids = [integer(int(v), 0, 65535) for v in a.ids.split(",")]
        except ValueError:
            raise CliError("对象编号无效") from None
        if len(ids) > 256 or len(set(ids)) != len(ids):
            raise CliError("对象编号重复或过多")
        body.update(obj_list=ids)
    elif c == "ams-control":
        if a.action not in ("resume", "pause", "reset", "done", "abort"):
            raise CliError("AMS 控制参数无效")
        body.update(param=a.action)
    elif c in ("ams-rfid", "ams-filament", "ams-drying", "load", "unload"):
        ams_id = integer(a.ams_id, 0, 135)
        slot = integer(a.slot, 0, 3)
        if c == "ams-rfid":
            body.update(command="ams_get_rfid", ams_id=ams_id, slot_id=slot)
        elif c == "ams-filament":
            import re

            if (
                not re.fullmatch(r"[0-9A-Fa-f]{8}", a.color)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", a.material)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", a.profile)
            ):
                raise CliError("耗材类型、配置编号或颜色无效")
            low, high = integer(a.min_temp, 0, 300), integer(a.max_temp, 0, 300)
            if low > high:
                raise CliError("耗材温度范围无效")
            body.update(
                command="ams_filament_setting",
                ams_id=ams_id,
                tray_id=slot,
                tray_info_idx=a.profile,
                tray_color=a.color.upper(),
                tray_type=a.material,
                nozzle_temp_min=low,
                nozzle_temp_max=high,
            )
        elif c == "ams-drying":
            if a.action not in ("start", "stop"):
                raise CliError("烘干操作无效")
            body.update(
                command="ams_filament_drying",
                ams_id=ams_id,
                temp=integer(a.temperature, 45, 65) if a.action == "start" else 0,
                cooling_temp=integer(a.cooling_temp, 45, 65),
                duration=integer(a.duration, 1, 24) if a.action == "start" else 0,
                humidity=0,
                mode=1 if a.action == "start" else 0,
                rotate_tray=a.rotate_tray,
                filament=a.filament,
                close_power_conflict=a.close_power_conflict,
            )
        else:
            body.update(
                command="ams_change_filament",
                ams_id=ams_id if c == "load" else 255,
                slot_id=slot if c == "load" else 0,
                target=ams_id * 4 + slot if c == "load" else 255,
                curr_temp=integer(a.current_temp, 0, 300),
                tar_temp=integer(a.temperature, 0, 300),
            )
    elif c == "clear-error":
        body.update(
            command="clean_print_error",
            subtask_id=a.subtask_id,
            print_error=integer(a.error_code, 0, 0xFFFFFFFF),
        )
    else:
        raise CliError("没有此控制命令")
    return section, body
