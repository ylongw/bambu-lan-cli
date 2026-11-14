# SPDX-License-Identifier: AGPL-3.0-or-later
import ipaddress
import json
import re
from dataclasses import dataclass, field

from .errors import CliError


@dataclass(frozen=True)
class Authorization:
    execute: bool
    serial: str = field(repr=False)

    def check(self, config):
        if self.execute is not True or self.serial != config.serial:
            raise CliError("写操作必须同时提供 --execute 和匹配的 --confirm-serial")


def authorize(args, config):
    auth = Authorization(
        getattr(args, "execute", False), getattr(args, "confirm_serial", None)
    )
    auth.check(config)
    return auth


def private_fields(value):
    """Retain telemetry structure while suppressing credentials and identities."""
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if re.search(
                    r"access.?code|password|token|secret|(?:^|_)(?:ip|ip_addr|ip_address|mac|mac_address|ssid|bssid)(?:$|_)|(?:^|_)(?:sn|serial|serial_number|device_id|user_id|uid|uuid|tag_uid|tray_uuid|nozzle_sn)(?:$|_)",
                    k,
                    re.I,
                )
                else private_fields(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [private_fields(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"<>]+", "[REDACTED_URL]", value)
        value = re.sub(r"/(?:Users|home)/[^/\s]+", "[REDACTED_HOME]", value)

        def mask_ip(match):
            try:
                ipaddress.IPv4Address(match.group())
                return "[REDACTED_IP]"
            except ValueError:
                return match.group()

        value = re.sub(
            r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", mask_ip, value
        )
    return value


def redact(value, config=None):
    text = (
        json.dumps(private_fields(value), ensure_ascii=False)
        if not isinstance(value, str)
        else value
    )
    if config:
        for secret in (config.access_code, config.serial, config.host, config.ssh_via):
            if secret:
                # Handle both literal and JSON escaped forms.
                text = text.replace(
                    json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]"
                ).replace(secret, "[REDACTED]")
    return text


def remote_filename(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.gcode\.3mf", value)
        or ".." in value
    ):
        raise CliError("文件名必须为安全的 .gcode.3mf 基名")
    return value
