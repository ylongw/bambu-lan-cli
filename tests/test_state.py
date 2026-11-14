# SPDX-License-Identifier: AGPL-3.0-or-later
from bambu_lan_cli.hms import decode_code, decode_errors
from bambu_lan_cli.state import summarize_status


def test_p2s(config):
    state = {
        "gcode_state": "PAUSE",
        "device": {"ctc": {"info": {"temp": 33}}, "airduct": {"modeId": 1}},
        "vir_slot": [
            {"tray_type": "PLA", "tray_info_idx": "GFA00", "tray_color": "FFFFFFFF"}
        ],
        "ams": {
            "tray_now": "0",
            "tray_tar": "1",
            "ams": [
                {
                    "id": "0",
                    "temp": 29,
                    "humidity": "2",
                    "humidity_raw": "30",
                    "tray": [
                        {
                            "id": "0",
                            "tray_type": "PLA",
                            "tray_sub_brands": "Generic",
                            "tray_info_idx": "GFA00",
                            "remain": 70,
                            "nozzle_temp_min": 190,
                            "nozzle_temp_max": 230,
                        }
                    ],
                }
            ],
        },
        "print_error": 0x07008006,
        "hms": [],
        "lights_report": [{"node": "chamber_light", "mode": "on"}],
        "spd_lvl": 2,
    }
    out = summarize_status(config, state, {}, 3)
    assert out["chamber_temperature_c"] == 33 and out["external_spool"]["type"] == "PLA"
    assert (
        out["ams"]["units"][0]["humidity_percent"] == "30"
        and out["ams"]["tray_now"] == "0"
    )
    assert out["airduct"] == {"modeId": 1} and out["errors"][0]["en"] != "Unknown error"
    assert config.serial not in str(out) and config.access_code not in str(out)
    state["vt_tray"] = {"tray_type": "PETG"}
    assert summarize_status(config, state, {}, 1)["external_spool"]["type"] == "PETG"


def test_errors():
    for code in (0x07008006, 0x07008010):
        out = decode_code(code)
        assert out["zh"] != "未知错误" and out["en"] != "Unknown error"
    assert decode_code(0x07008010)["en"].lower().find("overload") >= 0
    assert decode_code(None)["code"] == "unknown"
    assert decode_errors({"hms": [{"code": 0x07008006}]})[0]["code"] == "07008006"


def test_full_hms_code():
    errors = decode_errors({"hms": [{"attr": 0x03000100, "code": 0x00010001}]})
    assert errors[0]["en"] != "Unknown HMS alarm"
