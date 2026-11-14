# SPDX-License-Identifier: AGPL-3.0-or-later
# Error text: Copyright (c) 2023 ha-bambulab contributors, MIT.
# Full original MIT text and pinned source in THIRD_PARTY_NOTICES.md.
import json
from importlib.resources import files

_TABLE = json.loads(files(__package__).joinpath("hms_errors.json").read_text())


def decode_code(value, section="device_error"):
    try:
        code = f"{int(value, 0) if isinstance(value, str) and value.startswith('0x') else int(value):08X}"
    except (ValueError, TypeError):
        return {"code": "unknown", "zh": "未知错误", "en": "Unknown error"}
    result = _TABLE.get(section, {}).get(code, {})
    return {
        "code": code,
        "zh": result.get("zh", "未知错误"),
        "en": result.get("en", "Unknown error"),
    }


def decode_errors(state):
    out = []
    if state.get("print_error"):
        out.append(decode_code(state["print_error"]))
    for item in state.get("hms", []) or []:
        if not isinstance(item, dict):
            continue
        if "attr" in item and "code" in item:
            try:
                code = f"{int(item['attr']):08X}{int(item['code']):08X}"
            except (ValueError, TypeError):
                continue
            entry = _TABLE.get("device_hms", {}).get(code, {})
            out.append(
                {
                    "code": code,
                    "zh": entry.get("zh", "未知 HMS 告警"),
                    "en": entry.get("en", "Unknown HMS alarm"),
                }
            )
        else:
            out.append(decode_code(item.get("code")))
    return out
