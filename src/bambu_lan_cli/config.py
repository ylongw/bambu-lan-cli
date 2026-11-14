# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private YAML or legacy dotenv configuration; no shell evaluation."""

import ipaddress
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import CliError


@dataclass(frozen=True)
class Config:
    host: str = field(repr=False)
    serial: str = field(repr=False)
    access_code: str = field(repr=False)
    model: str = "p2s"
    ssh_via: str | None = field(default=None, repr=False)


def validate_via(value):
    if value is not None and (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", value)
    ):
        raise CliError("SSH 主机别名格式无效")


def validate(values):
    if not isinstance(values, dict):
        raise CliError("配置必须为 YAML 对象")
    try:
        address = ipaddress.IPv4Address(values.get("host"))
    except (ValueError, TypeError):
        raise CliError("host 必须为 RFC1918 私网 IPv4") from None
    if not any(
        address in ipaddress.ip_network(n)
        for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise CliError("host 必须为 RFC1918 私网 IPv4")
    serial = values.get("serial")
    if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9]{10,20}", serial):
        raise CliError("serial 必须为 10–20 位字母数字")
    code = values.get("access_code")
    if (
        not isinstance(code, str)
        or not code.strip()
        or len(code) > 128
        or any(ord(c) < 32 for c in code)
    ):
        raise CliError("access_code 必须为非空字符串，请在 YAML 中使用引号")
    via = values.get("ssh_via")
    validate_via(via)
    model = values.get("model", "p2s")
    if not isinstance(model, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", model):
        raise CliError("model 格式无效")
    return Config(str(address), serial, code, model, via)


def secure_read(path):
    # Enforce for every config, before reading any secret. O_NOFOLLOW + fstat
    # closes the final-component symlink/permission race.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise CliError("配置不可读取或为符号链接") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
        ):
            raise CliError("配置必须由当前用户拥有、为普通文件且权限为 0600")
        if info.st_size > 65536:
            raise CliError("配置文件过大")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            return stream.read(65537)
    finally:
        if fd is not None:
            os.close(fd)


def parse_env(text):
    result = {}
    keys = {
        "BAMBU_HOST": "host",
        "BAMBU_SERIAL": "serial",
        "BAMBU_ACCESS_CODE": "access_code",
        "BAMBU_SSH_VIA": "ssh_via",
    }
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*", line)
        if match and match[1] in keys:
            value = match[2]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[keys[match[1]]] = value
    return result


def load_config(path=None, printer="p2s", *, cwd=None, home=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", printer):
        raise CliError("printer 名称无效")
    cwd, home = Path(cwd or Path.cwd()), Path(home or Path.home())
    candidates = (
        [Path(path).expanduser()]
        if path
        else [
            cwd / "config.local.yaml",
            cwd / "config.yaml",
            home / ".config/bambu/config.yaml",
            home / f".config/bambu/{printer}.env",
        ]
    )
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            text = secure_read(candidate)
            try:
                values = (
                    parse_env(text)
                    if candidate.suffix == ".env"
                    else yaml.safe_load(text)
                )
            except yaml.YAMLError:
                raise CliError("YAML 格式错误；未输出可能含密钥的原文") from None
            return validate(values)
    raise CliError("未找到配置，请执行 config init 或使用 --config")


def init_config(path):
    path = Path(path).expanduser()
    if path.exists() or path.is_symlink():
        load_config(path)
        return {"config": "existing configuration valid", "permissions": "0600"}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(
            'host: 192.168.1.100\nserial: 01P00A000000000\naccess_code: "00000000"\nmodel: p2s\n'
        )
    return {
        "config": "created placeholder; edit privately before connecting",
        "permissions": "0600",
    }
