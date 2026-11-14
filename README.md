# bambu-lan-cli

面向 Bambu Lab 打印机的纯 LAN Python CLI，重点兼容 P2S / AMS 2 Pro。支持 MQTT 状态与控制、USB/TF 隐式 FTPS、内部 eMMC TCP 6000 文件隧道、RTSPS/A1/P1 JPEG 截图，以及可选 SSH 字节中继；无需云账号或 Home Assistant。

**默认只读。所有打印机变更操作都必须同时提供 `--execute --confirm-serial YOUR_SERIAL`。缺少任一参数或序列号不匹配，接触网络前立即拒绝。** 本次真机验证仅执行状态、相机状态、延时摄影目录列表和 media-ability。

## 安装与配置

```sh
python3.14 -m venv .venv
.venv/bin/pip install -e '.[test]'
source .venv/bin/activate
bambu-lan --help
bambu-lan config init
# 在仓库外私下编辑 ~/.config/bambu/config.yaml，权限保持 0600
bambu-lan config check
# 需要 RTSPS 截图时安装可选依赖
pip install -e '.[camera]'
```

Python 3.14 已验证，最低 Python 3.11。`config.example.yaml` 只保留占位符，绝不填入真实配置。配置依次读取：`--config PATH`、当前目录 `config.local.yaml` / `config.yaml`、`~/.config/bambu/config.yaml`、兼容旧格式的 `~/.config/bambu/<printer>.env`。指定配置无效时不静默回退。YAML 使用 safe_load，文件必须属于当前用户、为普通文件且权限恰为 0600；拒绝末级符号链接。

公共参数可放在子命令前后：`--config`、`--printer`（默认 p2s）、`--via`、`--timeout`（0–60 秒）、`--execute`、`--confirm-serial`。配置中的 `ssh_via` 可以用 `--via my-nas` 覆盖。SSH 仅使用已有密钥认证和已确认的主机密钥；MQTT/TCP/相机在本地完成 TLS。FTPS 的被动数据通道通过远程内存 Python worker 处理，凭证只经加密 stdin 传输，不写远程文件，不进入命令行参数，禁用 core dump，并设置生命周期上限。

## 只读与状态

| 子命令 | 示例与用途 |
|---|---|
| `config init/check` | `bambu-lan config check`：本地配置初始化/校验 |
| `status` | `bambu-lan status`：任务名、进度、层数、剩余分钟、上海时区预计完成时间、阶段/名称、速度、温度、风扇、灯、AMS 耗材/余量/湿度/烘干、HMS、print_error、存储、相机与 AI |
| `status --json` | `bambu-lan status --json`：保留 pushall 原始字段结构与版本响应，敏感值脱敏 |
| `ams-status` | `bambu-lan ams-status`：AMS 型号、湿度百分比/等级、温度、烘干状态、剩余分钟和托盘 RFID 烘干提示；`--json` 返回脱敏原始 AMS |
| `hms` | `bambu-lan hms`：中英文错误说明；未知编码保留数值 |
| `camera-status` | `bambu-lan camera-status`：ipcam_record、timelapse、resolution、tl_*、RTSP 可用性；`--json` 返回脱敏原始 ipcam |
| `get-version` / `info` | `bambu-lan get-version`：独立 `info.get_version` 查询 |
| `get-accessories` | `bambu-lan get-accessories`：查询喷嘴等配件，机型可能不支持 |
| `discover` | `bambu-lan discover`：本地 SSDP 发现，设备身份脱敏 |
| `media-ability` | `bambu-lan media-ability`：TCP 6000 能力、storage/upload_storage |
| `protocol-list` | `bambu-lan protocol-list --json`：离线查看全部 MQTT schema、读写属性、字段和来源，无需私有配置 |

`status` 仅发送 `pushall` / `get_version`，合并增量报告；未知状态字段可通过 `--json` 获取。完成时间按设备剩余分钟估计，仅在 RUNNING/PREPARE 时计算，暂停/空闲不推算。阶段和能力位因固件不同而不同，未知值不猜测。P1 不应频繁 pushall；不要高频循环调用。

`status` 和 `ams-status` 按 AMS `info` 的低 4 位映射型号：1=AMS、2=AMS Lite、3=AMS 2 Pro、4=AMS HT；缺失/未知不猜型号。烘干状态来自 `info` 的 4–7 位，剩余时间 `dry_time` 单位为分钟。

湿度百分比来自 `humidity_raw`，等级**不由百分比自行推算**。打印机原始 `humidity` 是 **5 最干、1 最湿**；[ha-bambulab 的显示约定](https://github.com/greghesp/ha-bambulab/blob/6466a6c45f861d9373779c7c2aae82d3afc1d1f3/custom_components/bambu_lab/definitions.py#L869-L875)与当前 Studio/Handy 一样，显示 `6 - 原始等级`，即 **1 最干、5 最湿**。摘要的 `humidity_level` 使用显示等级，`humidity_level_raw` 保留设备值，例如 `湿度 43%，等级 4/5（越高越湿；Studio/HA 约定）`；缺失值显示未知。`--json` 保留原始字段方向。

输出中的 access code、序列号、其他设备身份、RFID/UUID、网络信息、URL 和家目录身份统一脱敏。RTSP 地址不输出账号、密码、token 或完整地址。任务名和文件名按需求显示，请将实际运行输出作为私有数据处理，勿加入 Git。`discover` 即使指定 `--via` 仍在本地 LAN 发现；路由网络发现为空不代表设备离线。

## 文件与相机读取

| 子命令 | 示例 |
|---|---|
| `ls` | `bambu-lan ls /timelapse`；`bambu-lan ls --kind timelapse`；`bambu-lan ls --internal --kind timelapse`；`bambu-lan ls --internal --kind video`；`bambu-lan ls --internal`（模型） |
| `file-info` | `bambu-lan file-info /cache/sample.gcode.3mf`：FTPS SIZE |
| `download` | `bambu-lan download /timelapse/sample.mp4 ./sample.mp4`；`bambu-lan download sample.mp4 ./sample.mp4 --internal --kind timelapse` |
| `download --member` | `bambu-lan download sample.gcode.3mf ./plate.png --internal --kind model --member Metadata/plate_1.png`：experimental SUB_FILE 单成员/`thumbnail` |
| `project-member` | `bambu-lan project-member Metadata/plate_1.png ./plate.png`：experimental，从当前/最近项目的 mem:/16 读取成员 |
| `camera-snapshot` | `bambu-lan camera-snapshot ./frame.jpg`；`bambu-lan camera-snapshot ./frame.jpg --transport jpeg`；`--transport rtsp`：RTSPS 单帧 |

下载和截图拒绝覆盖，使用 0600 临时文件并在成功后排他发布；失败清理残片。FTPS 下载检查大小，TCP 下载检查分块偏移/大小、MD5 和列表大小。`download --max-mib 2048` 指定上限（默认 2048，允许 1..16384）。直连下载流式写盘；SSH FTPS 使用内存 worker，上限为 256 MiB，大文件使用直连或 TCP 6000。

外部 USB/TF 的延时摄影在 FTPS `/timelapse`；内部延时摄影通过 TCP 6000 `storage=internal`，模型维持 `emmc`。这两个存储不是同一目录。P2S/H2 的 TCP 6000 是文件服务，A1/P1 同端口是 JPEG 相机，不能互换。RTSPS 默认用于其余机型，需要本机启用 LAN Liveview；PyAV/Pillow 为可选依赖，认证只在内存中，不传给外部进程。当前仅提供单帧截图。

外部 FTPS 连接/认证由 `--timeout` 控制（默认 12 秒，最大 60）；目录列表单独使用 `--list-timeout`（默认 30 秒，最大 180），直连和 SSH 均生效。例如：

```sh
bambu-lan ls /timelapse --timeout 30 --list-timeout 90
bambu-lan media-ability
```

错误会说明超时发生在 TCP 连接、TLS 握手、欢迎响应、登录认证/保护协商，还是连接认证成功后的列表阶段；425/426 等表示数据通道/存储暂不可用，530 表示认证失败。550 只能说明路径不存在、权限不足或存储未挂载/不可访问，设备未区分原因，CLI 不会断言一定没插卡。先检查打印机是否识别 U 盘/TF 卡、`status` 存储字段和 `media-ability`；能力包含 udisk 不等于当前已挂载，也不保证 FTPS 可达。服务器原始错误中的私人路径不回显。

固件可能允许列出内部模型却拒绝读取/删除（result 2，受 fun2 bit 17 等能力限制）；CLI 如实报错，不伪造支持。FTPS 超时或未挂载存储时可尝试 `ls --internal --kind timelapse`，但这不保证外部存储可访问。

## 带双重授权的变更命令

下表每一条命令都需要追加 `--execute --confirm-serial YOUR_SERIAL`。以下只展示语法，**不会自动执行，也不是测试步骤**。

| 类别 | 子命令与示例（均需追加授权） |
|---|---|
| 打印/文件 | `print sample.gcode.3mf --plate 1 --timelapse --ams-mapping 0,2 --no-flow-cali`；`print sample.gcode.3mf --internal`；`upload sample.gcode.3mf --internal`；`delete sample.mp4 --internal --kind timelapse`；`delete /timelapse/sample.mp4`；`rename /sample.gcode.3mf /renamed.gcode.3mf`；`mkdir /archive`；`rmdir /archive`（仅空目录） |
| 任务 | `pause`；`resume`；`stop`；`skip-objects 1,3`；`speed standard`；`clear-error --error-code 0x07008006 --subtask-id 0` |
| 相机 | `camera-record disable`；`camera-timelapse enable`；`camera-resolution 1080p`；`camera-capture disable`；`camera-delete-oldest --storage internal --total-layer 500`（可能删除多个最旧文件） |
| AI | `xcam spaghetti_detector enable --sensitivity medium`；`xcam first_layer_inspector enable`；`xcam printing_monitor disable`；`xcam buildplate_marker_detector enable` |
| 灯光/环境 | `light chamber_light on`；`light chamber_light2 flashing --on-time 500 --off-time 500 --loops 3 --interval 0`；`light heatbed_light off`；`fan part 50`；`fan aux 50`；`fan chamber 50`；`fan enhanced 50`（P9） |
| 温度/工具 | `temp nozzle 220`；`temp bed 60`；`chamber 40 --temper-check`；`airduct 0`；`door 1`（0 关闭、1 警告、2 暂停）；`select-tool 0` |
| 喷嘴/校准 | `nozzle --diameter 0.4 --material hardened_steel`（set_accessories）；`nozzle --protocol set-nozzle --diameter 0.4 --id 0 --type HH01 --wear 0`；`calibration --option 0x0f` |
| 打印选项 | `print-option sound_enable disable`；`print-option auto_recovery enable`；`print-option auto_switch_filament enable`；`print-option filament_tangle_detect enable`；`print-option air_print_detect enable`；`print-option nozzle_blob_detect enable`；`print-option air_purification 1`；`print-option nozzle_blob_detect_v2 2` |
| AMS | `ams-control resume`（也支持 pause/reset/done/abort）；`ams-rfid --ams-id 0 --slot 0`；`ams-settings --ams-id 0 --no-startup-read --tray-read --calibrate-remain`；`ams-drying start --ams-id 0`；`ams-drying start --filament PETG --temperature 60 --duration 8`；`ams-drying stop` |
| 耗材 | `ams-filament --ams-id 0 --slot 0 --material PLA --profile GFA00 --color FFFFFFFF --min-temp 190 --max-temp 230`；`load --ams-id 0 --slot 0 --temperature 220`；`unload` |
| G-code | `gcode 'M400'`：原始 G-code，仍需双重授权 |

完整 AI 模块：`printing_monitor`、`spaghetti_detector`（炒面）、`buildplate_marker_detector`、`first_layer_inspector`、`pileup_detector`、`clump_detector`、`airprint_detector`、`plate_offset_switch`、`fod_check`、`model_movement_check`。`--sensitivity` 支持 never_halt/low/medium/high，省略时不发送该字段；各机型不一定支持每个模块。

`ams-drying start --ams-id 0 --execute --confirm-serial YOUR_SERIAL` 默认检测该 AMS **所有已装载托盘**的 `tray_type`，不再固定 PLA。有 `tray_exist_bits` 时优先按物理在位位图过滤空槽的旧信息。多托盘类型冲突、为空或未知时拒绝，要求显式 `--filament`；指定耗材意味着用户已检查整箱耗材适用性，不会改变托盘设置。

温度/时长优先级：显式 `--temperature` / `--duration` > 同耗材托盘的 RFID `drying_temp` / `drying_time`（正值，小时）> 耗材预设。多个 RFID 提示冲突时要求显式参数。缺省预设采用 [Bambu Studio PLA](https://github.com/bambulab/BambuStudio/blob/930fe93851afbc76b1c64dd37d8728b6a9afc89c/resources/profiles/BBL/filament/fdm_filament_pla.json) 的 45°C/12 小时及 [PETG](https://github.com/bambulab/BambuStudio/blob/930fe93851afbc76b1c64dd37d8728b6a9afc89c/resources/profiles/BBL/filament/fdm_filament_pet.json) 的 65°C/12 小时（空闲烘干预设）；其他已知类型的型号对应预设如下（温度°C / 小时）；均来自同一 Studio 版本的 `fdm_filament_*` 空闲烘干预设。未知、复合/特殊耗材无 RFID 提示时要求显式温度和时长，不按名称前缀套用预设。

| 耗材 | AMS 2 Pro | AMS HT |
|---|---|---|
| PLA | 45 / 12 | 45 / 12 |
| PETG | 65 / 12 | 65 / 12 |
| ABS、ASA、PC | 65 / 12 | 80 / 8 |
| PA | 65 / 12 | 85 / 12 |
| PVA | 65 / 12 | 85 / 18 |
| TPU | 65 / 12 | 75 / 18 |

所有启动路径（含高级 `mqtt print.ams_filament_drying`）先查询状态，按已识别型号钳位：AMS 2 Pro 45–65°C、AMS HT 45–85°C，时长 1–24 小时；冷却温度默认 45°C，钳位在 45°C 与实际烘干温度之间。未知型号或无已确认烘干能力的 AMS 拒绝启动。参数不会突破这些上限，但低于耗材要求的温度可能无法充分干燥。

打印中、有任务或状态未知时默认拒绝启动烘干。只有显式 `--force` 才放行，且**仍需 `--execute` 和精确 `--confirm-serial`**；此规则同样覆盖高级 MQTT 入口。`--force` 不改变耗材检测或型号上限。停止烘干也始终需要双重授权。

当前只发送确认的定时模式 `mode=1` 与停止 `mode=0`。`mode=2`（目标湿度）在 OBN 仅确认 experimental `OnHumidity` 枚举，未确认完整请求与湿度目标语义；CLI 拒绝发送，不编造字段。来源与限制见 [协议覆盖清单](docs/PROTOCOL_COVERAGE.md)。本轮正在打印的真机未执行任何烘干命令，烘干逻辑仅由 mock 验证。

内部上传只有收到完成应答、并在新连接 LIST 中确认唯一文件、内部路径和大小一致后才报告成功。首轮验证异常时最多再建一次连接做只读 LIST，**不重传文件**。成功结果中的 `verification_warnings` 保留首轮原因；两次校验失败则返回失败并列出帧类型/状态码/超时/大小差异，文件可能已到达也不声称成功。

喷嘴最高 300°C、热床 110°C、腔温 60°C。`--close-power-conflict` 必须显式指定。G-code 是高级入口，其任意代码不能靠这些温度限制约束。Airduct 模式 0/1/2 按固件能力选择，2 可能涉及激光机型。

AMS 映射接受绝对槽位编号 0–103、128–135、-1、254/255，生成 ams_mapping 和 ams_mapping2。默认无 AMS；使用者需核对 plate 的耗材数。上传只允许安全 ASCII `.gcode.3mf` 基名，最大 200 MiB，拒绝覆盖；上传不会自动打印。内部上传流水发送分块，检查 MD5 并用独立连接复查。FTPS 改名/上传的覆盖检查不能完全排除其他客户端的竞争，应使用唯一文件名。

打印、校准和直接运行 gcode_file 拒绝活跃或未知任务状态；恢复仅允许 PAUSE。MQTT 变更只发送一次，不重连/重试，ACK 必须匹配 section、command、sequence_id；接受大小写不敏感的 success。无 ACK 时仅特定速度/任务/温度/错误状态可确认成功，其他情况返回结果未知，不要盲目重复。`refresh_nozzle` 可能触发识别，按变更处理。`chamber` 只发送 set_ctt，不隐式连发 airduct。

## 全部高级 MQTT 命令

`mqtt SECTION.COMMAND --params JSON` 为 experimental，参数来自真实上游 schema，不允许任意命令/未知字段或调用者覆盖 sequence_id。无参数也要显式提供 `'{}'`。先用 `protocol-list --json` 或 [协议覆盖清单](docs/PROTOCOL_COVERAGE.md) 查看字段、来源、权限和机型限制。

```sh
bambu-lan mqtt info.get_version --params '{}'
bambu-lan mqtt camera.ipcam_get_media_info --params '{"sub_command":"is_timelapse_storage_enough","storage":"internal","total_layer":500}'
# 下面是受保护变更的语法示例
bambu-lan mqtt print.ams_reset --params '{}' --execute --confirm-serial YOUR_SERIAL
bambu-lan mqtt system.print_cache_set --params '{"config":false}' --execute --confirm-serial YOUR_SERIAL
```

下表中的名称均加 `mqtt` 前缀，使用 `--params`；其中只有目录标为 readonly 的查询免授权。

| 类别 | 全部协议命令 |
|---|---|
| info | `info.get_version` |
| pushing | `pushing.pushall`, `pushing.start` |
| camera | `camera.ipcam_cap_pic_set`, `camera.ipcam_delete_oldest_timelapse`, `camera.ipcam_get_media_info`, `camera.ipcam_record_set`, `camera.ipcam_resolution_set`, `camera.ipcam_timelapse` |
| xcam | `xcam.xcam_control_set` |
| system | `system.get_access_code`, `system.get_accessories`, `system.ledctrl`, `system.print_cache_set`, `system.set_accessories`, `system.set_door_stat`, `system.uiop` |
| update | `update.rename` |
| upgrade | `upgrade.consistency_confirm`, `upgrade.get_history`, `upgrade.mc_for_ams_firmware_upgrade`, `upgrade.start`, `upgrade.upgrade_confirm`, `upgrade.wtm_upgrade` |
| 任务与运动 | `print.pause`, `print.resume`, `print.stop`, `print.project_file`, `print.gcode_file`, `print.gcode_line`, `print.print_speed`, `print.skip_objects`, `print.clean_print_error`, `print.idle_ignore`, `print.ignore`, `print.close_air_filt`, `print.buzzer_ctrl`, `print.xyz_ctrl`, `print.back_to_center` |
| 温控、风扇、喷嘴 | `print.set_bed_temp`, `print.set_nozzle_temp`, `print.set_ctt`, `print.set_airduct`, `print.set_fan`, `print.select_extruder`, `print.set_nozzle`, `print.refresh_nozzle`, `print.set_extrusion_length`, `print.set_against_continued_heating_mode`, `print.nozzle_holder_ctrl`, `print.nozzle_info_confirm`, `print.holder_nozzle_refresh`, `print.get_auto_nozzle_mapping` |
| AMS | `print.ams_change_filament`, `print.ams_control`, `print.ams_filament_setting`, `print.ams_filament_drying`, `print.ams_get_rfid`, `print.ams_reset`, `print.ams_user_setting`, `print.auto_stop_ams_dry`, `print.manual_ams_bind` |
| 选项与校准 | `print.print_option`, `print.calibration`, `print.extrusion_cali`, `print.extrusion_cali_get`, `print.extrusion_cali_get_result`, `print.extrusion_cali_set`, `print.extrusion_cali_sel`, `print.extrusion_cali_del`, `print.flowrate_cali`, `print.flowrate_get_result` |

`system.get_access_code` 的响应值始终脱敏。`update.rename` 按上游发送不含 command 的 update envelope，可能没有标准 ACK。升级入口仅发送 LAN 请求，不提供云签名、固件下载、降级编排或兼容保证；缺少明确格式的 sleep/wake 不实现。TCP 6000、云摄像头、未知字段等剩余限制逐条见覆盖清单。

## 安全、验证与许可

打印机 LAN TLS 为自签名证书，本工具延续可信 LAN 上不验证证书的策略；仅在可信 LAN/SSH 主机使用。尚未实现证书固定。SSH 保持 StrictHostKeyChecking=yes。

```sh
pytest -q
ruff check .
ruff format --check .
python scripts/privacy_audit.py
```

测试自动阻断真机网络，MQTT/FTPS/TCP/相机全部 mock。隐私审计从仓库外的私有旧格式配置读取对照值，只输出计数；扫描工作区、索引、暂存差异、所有可达提交树与完整 Git 日志，缺少输入即失败。私有配置、环境文件、日志和备份不能入库。

整体许可 **AGPL-3.0-or-later**，上游 MIT/WTFPL 许可继续保留，阶段表沿用 HA MIT 归属。[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 列出固定版本与许可证。[PROTOCOL_COVERAGE.md](docs/PROTOCOL_COVERAGE.md) 提供逐命令/字段来源。这是独立社区项目，与 Bambu Lab 无隶属关系。
