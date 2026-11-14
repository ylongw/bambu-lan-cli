# 第三方许可证与来源

本项目整体以 AGPL-3.0-or-later 发布。各原始许可继续适用。

## synman/bambu-printer-manager

https://github.com/synman/bambu-printer-manager

固定 commit: `9eb424da884fc77909ede0b9353d0dfd03cb6fbc`

WTFPL v2；命令结构、AMS 映射、状态与发现设计。

```text
            DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
                    Version 2, December 2004

 Copyright (C) 2004 Sam Hocevar <sam@hocevar.net>

 Everyone is permitted to copy and distribute verbatim or modified
 copies of this license document, and changing it is allowed as long
 as the name is changed.

            DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
   TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION

  0. You just DO WHAT THE FUCK YOU WANT TO.

```

## greghesp/ha-bambulab

https://github.com/greghesp/ha-bambulab

固定 commit: `6466a6c45f861d9373779c7c2aae82d3afc1d1f3`

MIT；pybambu 命令模板、状态字段、HMS 中英文错误表；不引入 Home Assistant 依赖。

```text
MIT License

Copyright (c) 2023 ha-bambulab contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

```

## ClusterM/open-bamboo-networking

https://github.com/ClusterM/open-bamboo-networking

固定 commit: `77de55a8c696c53125ca297bdf3fdf1258772335`

AGPL-3.0-or-later；TCP 6000 帧、认证、LIST_INFO、能力与流水分块上传协议，Python 实现改编自本地已验证实现。

TCP 6000 参考 `research/06.04-port-6000.md`、`src/tunnel_local.cpp`、`src/tunnel_upload.cpp`；完整 AGPL 文本见根目录 LICENSE。


## 本轮协议扩展

以上三个上游的固定版本保持不变。新增 `protocol_catalog.json` 的 MQTT 字段形状、`camera.py` 的 LAN 相机帧和 `tunnel.py` 的下载/删除/SUB_FILE 参照相同上游；`stages.json` 的阶段名称来自 ha-bambulab `pybambu/const.py` 的 CURRENT_STAGE_IDS，适用上面的完整 MIT 声明。

另核对 [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI)，固定 commit `cc383a2c96576a9f53391c879acfb8dca5c534e4`，主要为 `mqtt.md`、`video.md`、`ftp.md`。该文档以 GFDL-1.3 发布；本项目只独立记录协议事实与来源链接，不复制该文档的文字或代码，不将其许可证改为本项目许可证。其 `lan-file-tunnel.md` 的认证/帧头描述与已有真机验证的 OBN 实现存在冲突，本实现继续采用 OBN。详细来源链接见 [PROTOCOL_COVERAGE.md](docs/PROTOCOL_COVERAGE.md)。

可选 PyAV/Pillow 通过独立可选依赖安装，不内嵌第三方二进制。CLI 只在内存中向 PyAV 提供认证 URL，不打印 URL、解码器错误或日志。
