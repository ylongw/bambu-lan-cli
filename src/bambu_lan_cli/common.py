# SPDX-License-Identifier: AGPL-3.0-or-later
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo


def now_cst():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def merge_dict(target, update):
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_dict(target[key], value)
        else:
            target[key] = value
