# Arknights FBS → JSON

明日方舟游戏配置数据批量解码工具 | FlatBuffers / AES → JSON

## 介绍

本仓库提供一条 **Python 批处理管线**，将 `gamedata` 下的 `.bytes` 等二进制配置解码为结构化 JSON，并进行后处理（键值拍平、blackboard 还原、按模板补 `null` 等）。

### 实现的功能

1. 自动匹配 schema，调用 `flatc` 将 `.bytes` 解码为 JSON。
2. 无 schema 时支持 `chat_mask` AES 解密。
3. 可选：官网 APK 自动下载（纯 HTTP）、CDN 热更、内置 StudioCLI 导出。
4. 启动菜单；Release 为**单 exe**（内置 flatc / FBS / StudioCLI）。

### 相关项目

- [OpenArknightsFBS](https://github.com/MooncellWiki/OpenArknightsFBS) — FlatBuffers schema
- [ArknightsStudioCLI](https://github.com/aelurum/AssetStudio) — 从 APK / 热更导出 gamedata（打包时内置）

## 使用方法（Release 单 exe）

从 [Releases](https://github.com/Black-Star-Red/Ark-FbsDecoder/releases) 下载 **一个** `Ark-FbsDecoder.exe` 即可（Release 构建已内嵌混淆后的 `chat_mask`，无需再旁路写密钥）。

```
你的目录
├── Ark-FbsDecoder.exe     ← 仅此工具
├── data/ / output/        ← 运行后自动创建
└── apk_download/          ← 模式 3 缓存官网 APK（约 2GB）
```

双击菜单：


| 选项    | 说明                                 |
| ----- | ---------------------------------- |
| **1** | 解码 `data/` → `output/`             |
| **2** | 仅下 CDN anon                        |
| **3** | **全流程：官网 APK + CDN → 导出 → 解码**（推荐） |
| **4** | 本地 APK + 本地热更                      |
| **5** | 仅 CDN（无首包）                         |
| **6** | 自定义单源                              |
| **0** | 退出                                 |


模式 **3** 会从官方入口下载 Android APK（约 2GB）并拉 CDN 热更；这是**游戏资源**，不是额外工具依赖。已有 APK 时可用 `apk_skip_existing` 跳过。

```powershell
.\Ark-FbsDecoder.exe --mode full
```

## 使用方法（源码）

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.local.json
python fbs_to_json.py --menu
```

## 配置文件（可选）

### Release 单 exe

exe **可不配**即可运行（内置 flatc / FBS / StudioCLI / `chat_mask`）。

需要改行为时，在 exe **同目录**放置 `config.local.json`（也可用环境变量 `CONFIG_PATH` 指定路径）。相对路径均相对 exe 所在目录。未写的项使用内置默认值。

### 源码

复制 `config.example.json` 为 `config.local.json` 并填写本机路径（`fbs_dir`、`flatc`、`studio_cli` 等）。

### 常用字段


| 字段                                                              | 说明                                          |
| --------------------------------------------------------------- | ------------------------------------------- |
| `input_dir` / `out_dir`                                         | 输入 / 输出目录（exe 默认旁路 `data` / `output`）       |
| `defaults_json`                                                 | `flatc` 输出 schema 默认值                       |
| `flatten_kv`                                                    | 将 `[{key,value},…]` 拍平为字典并做相关后处理            |
| `use_template_pad`                                              | 按参考 JSON 补缺失字段（常补 `null`）                   |
| `template_dir`                                                  | 参考 gamedata 根目录（需与 `use_template_pad` 同时开启） |
| `skip_existing`                                                 | 输出已存在则跳过解码                                  |
| `postprocess_rules`                                             | 后处理规则 JSON（exe 默认用包内规则）                     |
| `chat_mask`                                                     | AES 回退解密密钥（Release 一般可留空）                   |
| `cdn_fetch_anon`                                                | Studio 前是否从 CDN 拉 `anon/`* 热更               |
| `cdn_platform` / `cdn_hu` / `cdn_hv`                            | CDN 平台与地址                                   |
| `cdn_anon_dir` / `cdn_skip_existing` / `cdn_workers`            | 热更目录、跳过已下载、并行数                              |
| `apk_fetch_official` / `apk_download_dir` / `apk_skip_existing` | 官网 APK 下载相关                                 |
| `run_studio_first`                                              | 解码前是否先跑 Studio 导出                           |
| `studio_cli` / `studio_input_apk` / `studio_input_hot`          | Studio 路径与本地 APK/热更（可空，走菜单/下载）              |
| `studio_hot_tmp` / `studio_args`                                | 热更临时目录与 CLI 参数                              |


`cdn_*` / `run_studio_*` / `apk_*` 主要影响菜单 2–6（下载/导出）；仅菜单 1 纯解码时通常不必改。

### 模板补全说明

源码示例：

```json
{
  "use_template_pad": true,
  "template_dir": "zh_CN/gamedata"
}

全流程另需本机 `flatc`、FBS、ArknightsStudioCLI。
```

