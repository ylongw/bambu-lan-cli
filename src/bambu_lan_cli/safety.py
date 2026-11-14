# SPDX-License-Identifier: AGPL-3.0-or-later
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


def redact(value, config=None):
    text = (
        json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
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
