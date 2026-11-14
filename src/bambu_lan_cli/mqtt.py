# SPDX-License-Identifier: AGPL-3.0-or-later
# MQTT state merge / commands: bpm (WTFPL-2.0) and ha-bambulab (MIT).
# Copyright (c) 2023 ha-bambulab contributors; see THIRD_PARTY_NOTICES.md.
import json
import threading
import time
import uuid
from contextlib import contextmanager

import paho.mqtt.client as mqtt

from .common import merge_dict, now_cst, tls_context
from .errors import AckTimeout, CliError
from .protocol import readonly_payload
from .relay import SshTunnel
from .state import summarize_status

_sequence = 0
_sequence_lock = threading.Lock()


def next_sequence():
    global _sequence
    with _sequence_lock:
        _sequence = max(_sequence + 1, time.time_ns() // 1_000_000)
        return str(_sequence)


class MqttConnection:
    def __init__(self, config, timeout, *, host=None, port=8883):
        self.config = config
        self.host, self.port = host or config.host, port
        self.timeout = timeout
        self.ready = False
        self.error = None
        self.reports = []
        self.status = {}
        self.version = {}
        self.report_count = 0
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="bambu-cli-" + uuid.uuid4().hex[:12],
            protocol=mqtt.MQTTv311,
            reconnect_on_failure=False,
        )
        self.client.connect_timeout = min(timeout, 10)
        self.client.username_pw_set("bblp", config.access_code)
        self.client.tls_set_context(tls_context())
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self.error = f"MQTT 连接被拒绝（reason_code={reason.value}）"
            return
        rc, _ = client.subscribe(f"device/{self.config.serial}/report", qos=0)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            self.error = f"MQTT 订阅失败（rc={int(rc)}）"

    def _on_subscribe(self, client, userdata, mid, reasons, properties):
        if any(reason.is_failure for reason in reasons):
            self.error = "MQTT 订阅被拒绝"
        else:
            self.ready = True

    def _on_message(self, client, userdata, message):
        if len(message.payload) > 2_000_000:
            return
        try:
            payload = json.loads(message.payload)
        except (ValueError, UnicodeError):
            return
        if not isinstance(payload, dict):
            return
        self.report_count += 1
        # 原始响应仅存在内存；只在最终输出时选择安全字段。
        self.reports.append(payload)
        self.reports = self.reports[-100:]
        if isinstance(payload.get("print"), dict):
            merge_dict(self.status, payload["print"])
        if (
            isinstance(payload.get("info"), dict)
            and payload["info"].get("command") == "get_version"
        ):
            merge_dict(self.version, payload["info"])

    def poll(self, seconds=0.2):
        rc = self.client.loop(timeout=seconds)
        if self.error:
            raise CliError(self.error)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            raise CliError(f"MQTT 网络循环失败（rc={int(rc)}）")

    def __enter__(self):
        try:
            self.client.connect(self.host, self.port, keepalive=30)
            deadline = time.monotonic() + self.timeout
            while not self.ready and time.monotonic() < deadline:
                self.poll()
            if not self.ready:
                raise CliError("MQTT 等待 CONNACK / SUBACK 超时")
            return self
        except Exception:
            self.client.disconnect()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.client.disconnect()

    def publish(self, payload, *, qos=0, authorization=None):
        readonly = readonly_payload(payload)
        if not readonly:
            if authorization is None:
                raise CliError("写操作缺少授权")
            authorization.check(self.config)
        message = self.client.publish(
            f"device/{self.config.serial}/request",
            json.dumps(payload, separators=(",", ":")),
            qos=qos,
            retain=False,
        )
        if message.rc != mqtt.MQTT_ERR_SUCCESS:
            raise CliError(f"MQTT 发布失败（rc={int(message.rc)}）")
        deadline = time.monotonic() + self.timeout
        while not message.is_published() and time.monotonic() < deadline:
            self.poll()
        if not message.is_published():
            stage = "PUBACK" if qos else "发送完成"
            raise CliError(f"MQTT 等待 {stage} 超时；不自动重发")

    def query_status(self, *, raw=False):
        # 只读查询使用 QoS 0；以 report 状态/版本响应验证应用层成功。
        self.publish(
            {"pushing": {"command": "pushall", "sequence_id": next_sequence()}}, qos=0
        )
        self.publish(
            {"info": {"command": "get_version", "sequence_id": next_sequence()}}, qos=0
        )
        deadline = time.monotonic() + self.timeout
        required = {"gcode_state", "nozzle_temper", "bed_temper"}
        while time.monotonic() < deadline:
            self.poll()
            if required.issubset(self.status) and self.version.get("module"):
                break
        if not self.status:
            raise CliError(
                "MQTT 已认证并订阅，但未收到打印状态；检查序列号和开发者模式"
            )
        if raw:
            return {"print": self.status, "info": self.version}
        return summarize_status(
            self.config, self.status, self.version, self.report_count
        )

    def query(self, section, body):
        sequence = next_sequence()
        body = {**body, "sequence_id": sequence}
        self.publish({section: body}, qos=0)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for report in self.reports:
                reply = report.get(section, {})
                if (
                    isinstance(reply, dict)
                    and reply.get("command") == body["command"]
                    and str(reply.get("sequence_id")) == sequence
                ):
                    if "result" in reply and str(reply["result"]).upper() not in (
                        "SUCCESS",
                        "0",
                    ):
                        raise CliError("打印机拒绝只读查询")
                    return reply
            self.poll()
        raise CliError("只读查询未收到匹配响应")

    def command(self, body, authorization, section="print"):
        authorization.check(self.config)
        sequence_id = next_sequence()
        body = {
            **({"param": ""} if section == "print" else {}),
            **body,
            "sequence_id": sequence_id,
        }
        self.publish({section: body}, qos=0, authorization=authorization)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for payload in self.reports:
                reply = payload.get(section, {})
                if (
                    isinstance(reply, dict)
                    and reply.get("command") == body.get("command")
                    and str(reply.get("sequence_id")) == sequence_id
                    and "result" in reply
                ):
                    if str(reply["result"]).upper() not in ("SUCCESS", "0"):
                        raise CliError("打印机拒绝命令；未自动重试，请检查打印机屏幕")
                    return {
                        "time_cst": now_cst(),
                        "command": body.get("command", "update.rename"),
                        "result": "SUCCESS",
                    }
            self.poll()
        raise AckTimeout("命令已发送但未收到匹配响应；结果未知，勿盲目重试")


@contextmanager
def status_connection(config, args):
    if args.via:
        with SshTunnel(config, args.via, args.timeout) as tunnel:
            with MqttConnection(
                config, args.timeout, host="127.0.0.1", port=tunnel.port
            ) as connection:
                yield connection
    else:
        with MqttConnection(config, args.timeout) as connection:
            yield connection
