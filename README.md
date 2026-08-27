# Arknights FBS → JSON

明日方舟游戏配置数据批量解码工具 | FlatBuffers / AES → JSON

## 介绍

本仓库提供一条 **Python 批处理管线**，将 `gamedata` 下的 `.bytes` 等二进制配置解码为结构化 JSON，并进行后处理（键值拍平、blackboard 还原、按模板补 `null` 等）。

### 实现的功能

1. 自动匹配 schema，调用 `flatc` 将 `.bytes` 解码为 JSON。
2. 无 schema 时支持 `chat_mask` AES 解密。
3. 可选：CDN 拉取热更、StudioCLI 导出、对照参考 JSON 补字段。

### 相关项目

- [OpenArknightsFBS](https://github.com/MooncellWiki/OpenArknightsFBS) — FlatBuffers schema
- [ArknightsStudioCLI](https://github.com/aelurum/AssetStudio) — 从 APK / 热更导出 gamedata（可选）

本仓库不包含游戏资源、schema 或参考 JSON，需自行准备。

## 使用方法

### 1. 资源准备

需本机已有 `gamedata` 下的 `.bytes`（可放入 `data/`，或配置 `input_dir`）、`flatc`、OpenArknightsFBS 的 `FBS` 目录；可选参考 JSON 与 `chat_mask`。

### 2. 安装依赖

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 配置与运行

```powershell
copy config.example.json config.local.json
```

编辑 `config.local.json` 中的路径与选项，然后：

```powershell
python fbs_to_json.py
```

解码结果默认写入 `output/`。各配置字段说明见 `config.example.json`。

环境变量：

| 变量 | 说明 |
|------|------|
| `CONFIG_PATH` | 配置文件路径，默认 `./config.local.json` |
| `CHAT_MASK` | AES 密钥（32 字符），优先于配置文件中的 `chat_mask` |
