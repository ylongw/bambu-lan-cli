# SPDX-License-Identifier: AGPL-3.0-or-later
"""AMS info decoding and conservative drying policy (OBN/HA/Bambu Studio).

See docs/PROTOCOL_COVERAGE.md for pinned sources. No mode=2 payload is inferred
from the OnHumidity enum alone. Only known dryer models may be started.
"""

from .errors import CliError

MODELS = {1: "AMS", 2: "AMS Lite", 3: "AMS 2 Pro", 4: "AMS HT"}
# HA services.yaml uses 1..24 hours, AMS 2 Pro <=65 C, AMS HT <=85 C.
LIMITS = {"AMS 2 Pro": (65, 24), "AMS HT": (85, 24)}
# Studio BBL fdm_filament_pla/pet presets: idle drying, first two entries.
DEFAULTS = {
    # (AMS 2 Pro, AMS HT), each (temperature C, duration hours).
    "PLA": ((45, 12), (45, 12)),
    "PETG": ((65, 12), (65, 12)),
    "ABS": ((65, 12), (80, 8)),
    "ASA": ((65, 12), (80, 8)),
    "PA": ((65, 12), (85, 12)),
    "PC": ((65, 12), (80, 8)),
    "PVA": ((65, 12), (85, 18)),
    "TPU": ((65, 12), (75, 18)),
}
DRY_STATES = {
    0: "关闭",
    1: "检查中",
    2: "烘干中",
    3: "冷却中",
    4: "停止中",
    5: "错误",
    6: "加热失控",
    7: "产品测试",
}


def number(value, base=10):
    try:
        if isinstance(value, bool) or value is None:
            return None
        return int(value, base) if isinstance(value, str) else int(value)
    except (ValueError, TypeError, OverflowError):
        return None


def decode_info(unit):
    info = number(unit.get("info"), 16)
    if info is None or info < 0:
        return {"model": "未知", "drying_state": "未知", "drying_state_code": None}
    code = (info >> 4) & 15
    return {
        "model": MODELS.get(info & 15, "未知"),
        "drying_state": DRY_STATES.get(code, "未知"),
        "drying_state_code": code,
    }


def loaded_trays(ams, unit):
    """Prefer physical presence bits over stale tray material metadata."""
    bits = number(ams.get("tray_exist_bits"), 16)
    unit_id = number(unit.get("id"))
    result = []
    for tray in unit.get("tray", []) or []:
        if not isinstance(tray, dict):
            continue
        slot = number(tray.get("id"))
        if bits is not None and unit_id is not None and 0 <= unit_id < 128:
            if (
                slot is None
                or not 0 <= slot <= 3
                or not bits & (1 << (unit_id * 4 + slot))
            ):
                continue
        elif not tray.get("tray_type"):
            continue
        result.append(tray)
    return result


def prepare_drying(body, raw, *, force=False):
    """Resolve defaults and clamp all thermal/time fields before any publish."""
    body = dict(body)
    if body.get("mode") == 0:
        return body
    if body.get("mode") != 1:
        raise CliError("mode=2 仅确认实验性 OnHumidity 枚举，完整请求未确认；拒绝发送")
    state = raw.get("print", {})
    if not force and state.get("gcode_state") not in ("IDLE", "FINISH", "FAILED"):
        raise CliError("打印中、存在任务或状态未知，拒绝启动烘干；需显式 --force")
    ams = state.get("ams") or {}
    units = [
        u
        for u in ams.get("ams", []) or []
        if isinstance(u, dict) and str(u.get("id")) == str(body["ams_id"])
    ]
    if len(units) != 1:
        raise CliError("未找到唯一 AMS 单元；先用 ams-status 检查 --ams-id")
    unit = units[0]
    model = decode_info(unit)["model"]
    if model not in LIMITS:
        raise CliError("AMS 型号未知或无已确认烘干能力，拒绝启动")
    trays = loaded_trays(ams, unit)
    filament = body.get("filament")
    if not filament:
        materials = {str(t.get("tray_type") or "").strip().upper() for t in trays}
        if len(materials) != 1 or "" in materials:
            raise CliError("已装载托盘耗材为空、未知或类型冲突；请显式 --filament 指定")
        filament = next(iter(materials))
    filament = filament.strip().upper()
    if not filament:
        raise CliError("耗材类型为空；请指定 --filament")
    body["filament"] = filament
    selected = [t for t in trays if str(t.get("tray_type", "")).upper() == filament]
    presets = DEFAULTS.get(filament)
    defaults = presets[model == "AMS HT"] if presets else (None, None)
    # Different RFID hints require explicit choice; do not arbitrarily heat all
    # spools to the hottest hint or silently shorten another spool's duration.
    for key, hint, fallback in (
        ("temp", "drying_temp", defaults[0]),
        ("duration", "drying_time", defaults[1]),
    ):
        if body.get(key) is None:
            hints = {
                v for t in selected if (v := number(t.get(hint))) is not None and v > 0
            }
            if len(hints) > 1:
                raise CliError(f"托盘 RFID {hint} 提示冲突；请显式指定温度/时长")
            body[key] = next(iter(hints), fallback)
        if type(body[key]) is not int or body[key] <= 0:
            raise CliError("无可靠耗材默认值；请显式指定 --temperature 和 --duration")
    max_temp, max_hours = LIMITS[model]
    body["temp"] = max(45, min(max_temp, body["temp"]))
    body["duration"] = max(1, min(max_hours, body["duration"]))
    body["cooling_temp"] = max(45, min(body["temp"], body["cooling_temp"]))
    return body
