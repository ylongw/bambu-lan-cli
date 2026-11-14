# bambu-lan-cli

LAN-only Python CLI and library for Bambu Lab printers, focused on P2S / AMS 2 Pro. MQTT status/control, USB implicit FTPS with TLS session reuse, internal eMMC TCP 6000, and optional SSH stdio relay. No cloud account or Home Assistant runtime.

**Default is read-only. Every device write requires both `--execute` and `--confirm-serial`. Never test write commands while a printer has a paused task or unresolved hardware problem.**

## Install

```sh
python3.14 -m venv .venv
.venv/bin/pip install -e '.[test]'
source .venv/bin/activate
bambu-lan --help
python -m bambu_lan_cli --help
```

Python 3.14 is the tested runtime; package metadata allows Python 3.11 and newer. Runtime dependencies: paho-mqtt >= 2.1 and PyYAML >= 6.0.2. No connections occur on import.

## Private local YAML

```sh
bambu-lan config init
# Edit ~/.config/bambu/config.yaml privately; keep its permissions 0600.
bambu-lan config check
```

`config.example.yaml` contains placeholders only. Never replace its placeholders with real values. Actual configuration belongs outside Git. Lookup order:

1. `--config PATH` (no silent fallback if missing or invalid).
2. Current working directory `config.local.yaml`, then `config.yaml`.
3. `~/.config/bambu/config.yaml`.
4. Legacy `~/.config/bambu/<printer>.env` (`--printer p2s` by default).

YAML uses `yaml.safe_load`. All config files must be regular files owned by the current user with exactly `0600` permissions; final-component symlinks are rejected before reading. Host must be an RFC1918 IPv4 address, serial must be 10–20 alphanumeric characters, and access code must be a nonempty quoted string. Legacy dotenv is parsed as data, never sourced into a shell.

Optional `ssh_via` selects an existing SSH host alias. `--via my-nas` overrides it. No host alias is embedded in code. MQTT and TCP 6000 send only TLS bytes through SSH; their credentials remain local. FTPS passive data channels require a remote Python worker: credentials/data travel through encrypted stdin, stay in memory, core dumps are disabled, and the worker has a 180-second lifetime. Remote host needs Python 3, key authentication and a pre-approved host key. No remote files are created. SSH stdout/stderr are captured, and raw failures are suppressed.

## Read-only commands

```sh
bambu-lan status
bambu-lan hms
bambu-lan ls
bambu-lan ls /timelapse
bambu-lan ls --internal
bambu-lan ls --internal --kind timelapse
bambu-lan discover
bambu-lan media-ability
bambu-lan --via my-nas status
bambu-lan --via my-nas ls --internal
```

`status` sends only `pushall` / `get_version` queries, merging partial reports. Output includes P2S chamber temperature (`device.ctc.info.temp`), airduct, raw fan-speed telemetry, speed, lights, external spool (`vt_tray` / `vir_slot`), AMS temperature/humidity/tray material/color/remaining/profile and tray mapping, print error and bilingual HMS descriptions. Unknown error codes remain visible as codes with an explicit unknown message. Some source strings are model-specific alternatives; check the printer screen for the exact diagnosis.

Access code, configured host, serial and SSH alias are redacted from JSON/errors. Status deliberately omits task names, file names and RFID identities. File listings still contain filenames; treat their output as private. Discovery masks device identities and names, listens on UDP 2021 and probes multicast port 1990 plus the configured host. Discovery runs on the local LAN even when `--via` is selected. Zero discovered devices is a valid result on routed networks.

If USB FTPS fails or no USB drive is present, the error suggests `ls --internal`. TCP 6000 listings and uploads require compatible firmware. Internal download/delete is intentionally not implemented.

## Guarded controls

Every following command needs `--execute --confirm-serial YOUR_SERIAL`. Without both, it fails before network access. These examples show syntax only:

```sh
bambu-lan print sample.gcode.3mf --plate 1 --timelapse --ams-mapping 0,2 --no-flow-cali --execute --confirm-serial YOUR_SERIAL
bambu-lan upload sample.gcode.3mf --internal --execute --confirm-serial YOUR_SERIAL
bambu-lan print sample.gcode.3mf --internal --execute --confirm-serial YOUR_SERIAL
bambu-lan speed standard --execute --confirm-serial YOUR_SERIAL
```

Other commands (use `COMMAND --help` for parameters):

| Group | Commands |
|---|---|
| Task | `pause`, `resume`, `stop`, `skip-objects ID,ID` |
| Environment | `light NODE on/off`, `fan part/aux/chamber PERCENT`, `temp nozzle/bed CELSIUS`, `airduct MODE`, `gcode CODE` |
| AMS | `ams-control resume/pause/reset/done`, `ams-rfid`, `ams-drying start/stop`, `ams-filament`, `load`, `unload`, `clear-error` |

Nozzle is capped at 300°C and bed at 110°C. Raw G-code remains a powerful escape hatch; the double lock is mandatory and temperature restrictions cannot constrain arbitrary raw G-code. Fan percentages produce `M106 P1/P2/P3` with 0–255 PWM. Airduct mode IDs follow firmware (0 cooling, 1 heating, 2 laser on capable models); choose only supported modes.

AMS 2 Pro drying is capped at 45–65°C, duration **1–24 hours**, cooling temperature at least 45°C; optional `--close-power-conflict` is explicit. AMS mapping accepts comma-separated absolute tray IDs (0–103, 128–135, -1, 254/255), producing both `ams_mapping` and `ams_mapping2`. Default external-spool mapping is `[-1]` plus `{ams_id:255, slot_id:0}` for single-nozzle P2S. Check plate filament count yourself; automatic 3MF/dual-nozzle mapping is outside this version.

Each MQTT request gets an increasing `sequence_id`; controls include an explicit `param`. Writes use QoS 0 and are sent once, without reconnect/retry. ACKs must match section, command and sequence. Missing ACK triggers a read-only status query; only specific speed/task/temperature/error transitions can confirm success. Other outcomes are reported as unknown, even if hardware may have changed. Never blindly repeat an uncertain write. `print` refuses active/unknown states and `resume` requires `PAUSE`.

Internal upload refuses an existing filename, pipelines chunks per the upstream protocol, checks MD5 and then opens an independent TLS session to verify name and size. FTPS also checks for existing names and verifies size; concurrent clients can still race, so use unique names. Filenames are conservative ASCII `.gcode.3mf` basenames. No upload starts a print automatically.

## Security, validation and license

LAN printer TLS certificates are self-signed; this client preserves the existing trusted-LAN policy without certificate verification. Use only a trusted LAN or trusted SSH host. Certificate pinning is not implemented. SSH uses `StrictHostKeyChecking=yes` and never disables host-key verification.

```sh
pytest -q
python scripts/privacy_audit.py
```

Tests block live network connections and mock every control. The privacy audit requires a private legacy config as an in-memory comparison source; it scans publishable workspace files, index blobs, staged diff, every reachable commit tree, and `git log -p`, plus path/token patterns and required license files. It prints counts only and fails closed if inputs cannot be read. `.gitignore` excludes private configs, env files, logs, backups and build artifacts. Do not commit private test evidence.

Overall license: **AGPL-3.0-or-later**. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for pinned upstream revisions, complete MIT/WTFPL notices and module attribution. This is an independent community tool, unaffiliated with Bambu Lab.
