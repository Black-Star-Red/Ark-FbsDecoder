from __future__ import annotations

import base64
import io
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import time
import os
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    """数据目录：data/、output/、可选 config.local.json。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """打包进 exe 的资源：flatc、FBS、后处理规则等。"""
    if is_frozen():
        return Path(sys._MEIPASS)
    return app_dir()


def config_path() -> Path:
    env = os.environ.get("CONFIG_PATH", "").strip()
    if env:
        return Path(env)
    return app_dir() / "config.local.json"


def ensure_runtime_layout() -> None:
    base = app_dir()
    (base / "data").mkdir(exist_ok=True)
    (base / "output").mkdir(exist_ok=True)


def _resolve_path(value: object, *, base: Path) -> Path | None:
    if value is None or value == "":
        return None
    p = Path(str(value))
    return p if p.is_absolute() else (base / p).resolve()


def _resolve_flatc(raw_flatc: object | None, *, base: Path, bundle: Path) -> str:
    if raw_flatc:
        flatc = str(raw_flatc)
        p = Path(flatc)
        if p.is_absolute():
            return str(p)
        if (base / p).is_file():
            return str((base / p).resolve())
        if (bundle / p).is_file():
            return str((bundle / p).resolve())
        return flatc
    for name in ("flatc.exe", "flatc"):
        cand = bundle / name
        if cand.is_file():
            return str(cand.resolve())
    return "flatc"


def _frozen_defaults() -> dict:
    base = app_dir()
    bundle = bundle_dir()
    rules = bundle / "k_postprocess_rules.json"
    return {
        "input_dir": base / "data",
        "out_dir": base / "output",
        "fbs_dir": bundle / "FBS",
        "flatc": _resolve_flatc(None, base=base, bundle=bundle),
        "template_dir" : bundle_dir() / "template" / "gamedata",
        "defaults_json": True,
        "flatten_kv": True,
        "postprocess_rules": rules if rules.is_file() else None,
        "use_template_pad": True,
        "skip_existing": False,
        "chat_mask": "",
        "cdn_fetch_anon": False,
        "cdn_platform": "Windows",
        "cdn_hu": "https://ak.hycdn.cn/assetbundle/official",
        "cdn_hv": "https://ak-conf.hypergryph.com/config/prod/official/{platform}/version",
        "cdn_anon_dir": base / "cdn_anon",
        "cdn_skip_existing": True,
        "cdn_workers": 8,
        "cdn_keep_markers": ["gamedata"],
        "cleanup_intermediates": False,
        "apk_fetch_official": False,
        "apk_download_dir": base / "apk_download",
        "apk_android_url": _DEFAULT_ANDROID_APK_URL,
        "apk_skip_existing": True,
        "apk_fetch_timeout_ms": 600_000,
        "run_studio_first": False,
        "studio_cli": None,
        "studio_input_apk": None,
        "studio_input_hot": None,
        "studio_hot_tmp": base / "studio_hot_tmp",
        "studio_args": [
            "-t",
            "textAsset",
            "-g",
            "container",
            "--filter-by-container",
            "gamedata",
            "-r"
        ],
    }


def _apply_raw_config(cfg: dict, raw: dict) -> dict:
    base = app_dir()
    bundle = bundle_dir()
    out = dict(cfg)
    for key, value in raw.items():
        if key not in (
            "input_dir",
            "out_dir",
            "fbs_dir",
            "template_dir",
            "postprocess_rules",
            "cdn_anon_dir",
            "apk_download_dir",
            "studio_hot_tmp",
            "studio_input_apk",
            "studio_input_hot",
            "studio_cli",
            "flatc",
        ):
            out[key] = value

    for key in (
        "input_dir",
        "out_dir",
        "fbs_dir",
        "template_dir",
        "postprocess_rules",
        "cdn_anon_dir",
        "apk_download_dir",
        "studio_hot_tmp",
        "studio_input_apk",
        "studio_input_hot",
    ):
        if key in raw:
            out[key] = _resolve_path(raw.get(key), base=base)

    if "flatc" in raw:
        out["flatc"] = _resolve_flatc(raw.get("flatc"), base=base, bundle=bundle)
    elif "flatc" not in out or not out["flatc"]:
        out["flatc"] = _resolve_flatc(None, base=base, bundle=bundle)

    if "studio_cli" in raw:
        out["studio_cli"] = _resolve_path(raw.get("studio_cli"), base=base)

    return out


def _bundled_chat_mask() -> str:
    """打包时由 ark_fbs_decoder.spec 生成 _pkg_secrets（XOR 分段混淆，非明文）。"""
    if not is_frozen():
        return ""
    try:
        import _pkg_secrets  # type: ignore

        reveal = getattr(_pkg_secrets, "reveal", None)
        if callable(reveal):
            return str(reveal() or "").strip()
        return str(getattr(_pkg_secrets, "CHAT_MASK", "") or "").strip()
    except Exception:
        return ""


def load_config() -> dict:
    if is_frozen():
        ensure_runtime_layout()
        cfg = _frozen_defaults()
        cp = config_path()
        if cp.is_file():
            raw = json.loads(cp.read_text(encoding="utf-8"))
            raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
            cfg = _apply_raw_config(cfg, raw)
    else:
        cp = config_path()
        if not cp.is_file():
            raise FileNotFoundError(
                f"配置文件不存在: {cp}\n"
                "请复制 config.example.json 为 config.local.json 并填写本机路径。"
            )
        raw = json.loads(cp.read_text(encoding="utf-8"))
        raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        cfg = _apply_raw_config({}, raw)

    # 优先级：环境变量 > config.local.json > 打包内嵌
    cfg["chat_mask"] = (
        os.environ.get("CHAT_MASK", "").strip()
        or str(cfg.get("chat_mask") or "").strip()
        or _bundled_chat_mask()
    )
    return cfg


RSA_HEADER = 128
BINARY_SUFFIXES = {".bytes", ".bin", ".dat"}
TEXT_SUFFIXES = {".json", ".txt", ".lua"}
_HEX6_SUFFIX_RE = re.compile(r"^(.+)([0-9a-fA-F]{6})$")
_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
# 官网下载页在 Android UA 下跳转的入口
_DEFAULT_ANDROID_APK_URL = "https://ak.hypergryph.com/downloads/android_lastest"


def build_schema_index(fbs_dir: Path) -> dict[str, Path]:
    """schema 名 -> .fbs 路径；名越长优先匹配。"""
    index: dict[str, Path] = {}
    for p in fbs_dir.rglob("*.fbs"):
        index[p.stem] = p
    return index


def resolve_schema(
    bin_path: Path,
    *,
    input_root: Path,
    schemas: dict[str, Path],
) -> tuple[str, Path] | None:
    """按文件名 / 相对路径匹配 OpenArknightsFBS schema。"""
    stem = bin_path.stem
    # 1) 精确名
    if stem in schemas:
        return stem, schemas[stem]

    # 2) 最长前缀 + 可选哈希后缀（修复 character_table + e 被当成 hex 的问题）
    for name in sorted(schemas, key=len, reverse=True):
        if stem == name:
            return name, schemas[name]
        if stem.startswith(name):
            rest = stem[len(name) :]
            if rest == "" or re.fullmatch(r"[0-9a-fA-F]{4,}", rest):
                return name, schemas[name]

    # 3) levels 下的关卡二进制
    try:
        rel = bin_path.resolve().relative_to(input_root.resolve()).as_posix()
    except ValueError:
        rel = bin_path.as_posix()
    if "/levels/" in f"/{rel}/" or rel.startswith("levels/"):
        if stem == "enemy_database" and "enemy_database" in schemas:
            return "enemy_database", schemas["enemy_database"]
        if "prts___levels" in schemas:
            return "prts___levels", schemas["prts___levels"]

    return None


def strip_header(data: bytes) -> bytes:
    """去掉 128 字节 RSA 签名。勿用首字节 '{'/'[' 判断明文 JSON——签名块也可能以 0x7b 开头。"""
    if not data:
        raise ValueError("空文件")
    if len(data) <= RSA_HEADER:
        raise ValueError(f"文件过短: {len(data)} bytes")
    return data[RSA_HEADER:]


def try_parse_json_bytes(data: bytes) -> object | None:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def try_aes_chatmask_plain(raw: bytes, chat_mask: str) -> bytes | None:
    """跳过 128B RSA 后，用 CHAT_MASK 做 AES-128-CBC，成功则返回去垫明文。"""
    mask = chat_mask.encode("ascii", errors="ignore")
    if len(mask) != 32:
        return None
    if len(raw) <= RSA_HEADER + 32:
        return None
    body = raw[RSA_HEADER:]
    key, xor_mask = mask[:16], mask[16:]
    iv = bytes(a ^ b for a, b in zip(body[:16], xor_mask))
    try:
        pt = AES.new(key, AES.MODE_CBC, iv).decrypt(body[16:])
        return unpad(pt, 16)
    except Exception:
        return None


def try_aes_chatmask_json(raw: bytes, chat_mask: str) -> object | None:
    """AES 解密后若为 UTF-8 JSON 则返回对象。"""
    plain = try_aes_chatmask_plain(raw, chat_mask)
    if plain is None:
        return None
    return try_parse_json_bytes(plain)


def _is_kv_pair_list(data: object) -> bool:
    """仅 [{key, value}, ...]（恰好两字段，且 key 可作 dict 键）才视为可拍平的 map。"""
    if not isinstance(data, list) or not data:
        return False
    for x in data:
        if not isinstance(x, dict) or set(x.keys()) != {"key", "value"}:
            return False
        try:
            hash(x["key"])
        except TypeError:
            return False
    return True


def _norm_map_key(k: object) -> object:
    """dict__int__* 拍平后 key 常为 int；与参考 JSON 字符串键对齐。"""
    if type(k) is int:
        return str(k)
    return k


def stringify_int_keys(data: object) -> object:
    """递归将 dict 的 int 键转为 str（避免 \"1\" vs 1 导致反包误套一层）。"""
    if isinstance(data, dict):
        return {_norm_map_key(k): stringify_int_keys(v) for k, v in data.items()}
    if isinstance(data, list):
        return [stringify_int_keys(x) for x in data]
    return data


def maybe_flatten_kv(data: object, *, _allow_unwrap: bool = True) -> object:
    """递归将 [{key,value}, ...] 拍成 {key: value}。

    仅在根层做「单键 dict 解包」（对齐 character_table 的 characters 外壳）。
    嵌套层不解包，避免 unlockCond / attributes 等被摊到父级。
    int 键统一成 str。
    """
    if _is_kv_pair_list(data):
        return {
            _norm_map_key(x["key"]): maybe_flatten_kv(x["value"], _allow_unwrap=False)
            for x in data
        }
    if isinstance(data, list):
        return [maybe_flatten_kv(x, _allow_unwrap=False) for x in data]
    if isinstance(data, dict):
        out = {
            _norm_map_key(k): maybe_flatten_kv(v, _allow_unwrap=False)
            for k, v in data.items()
        }
        if _allow_unwrap and len(out) == 1:
            only = next(iter(out.values()))
            if isinstance(only, dict):
                return only
        return out
    return data


def unwrap_list_values(data: object) -> object:
    """FlatBuffers list_* 外壳 {\"values\": [...]} → 内层 list（对齐参考格式）。

    仅拆「唯一键为 values 且值为 list」的对象；带其它键或 values 非 list 不动。
    """
    if isinstance(data, list):
        return [unwrap_list_values(x) for x in data]
    if isinstance(data, dict):
        if set(data.keys()) == {"values"} and isinstance(data["values"], list):
            return unwrap_list_values(data["values"])
        return {k: unwrap_list_values(v) for k, v in data.items()}
    return data

def _bson_cstring(buf: bytes, i: int) -> tuple[str, int]:
    j = buf.index(0, i)
    return buf[i:j].decode("utf-8"), j + 1


def _bson_document(buf: bytes, i: int, *, as_array: bool) -> tuple[object, int]:
    """解析 BSON document/array；i 指向 size 字段。返回 (value, 下一字节下标)。"""
    size = struct.unpack_from("<i", buf, i)[0]
    end = i + size
    i += 4
    items: list[tuple[str, object]] = []
    while i < end - 1:
        t = buf[i]
        i += 1
        if t == 0:
            break
        key, i = _bson_cstring(buf, i)
        if t == 0x01:  # double
            (val,) = struct.unpack_from("<d", buf, i)
            i += 8
        elif t == 0x02:  # string
            (slen,) = struct.unpack_from("<i", buf, i)
            i += 4
            val = buf[i : i + slen - 1].decode("utf-8")
            i += slen
        elif t == 0x03:  # document
            val, i = _bson_document(buf, i, as_array=False)
        elif t == 0x04:  # array
            val, i = _bson_document(buf, i, as_array=True)
        elif t == 0x08:  # bool
            val = buf[i] != 0
            i += 1
        elif t == 0x0A:  # null
            val = None
        elif t == 0x10:  # int32
            (val,) = struct.unpack_from("<i", buf, i)
            i += 4
        elif t == 0x12:  # int64
            (val,) = struct.unpack_from("<q", buf, i)
            i += 8
        elif t == 0x05:  # binary
            (blen,) = struct.unpack_from("<i", buf, i)
            i += 5 + blen  # subtype + data
            val = None
        else:
            raise ValueError(f"unsupported BSON type 0x{t:02x} key={key!r}")
        items.append((key, val))
    if as_array:
        out_list: list[object] = []
        for k, v in items:
            idx = int(k)
            while len(out_list) <= idx:
                out_list.append(None)
            out_list[idx] = v
        return out_list, end
    return dict(items), end


def decode_jobject_bson(b64: str) -> object:
    """解码 hg__internal__JObject.base64（BSON document）。"""
    raw = base64.b64decode(b64)
    doc, _ = _bson_document(raw, 0, as_array=False)
    return doc


def expand_jobject_base64(data: object) -> object:
    """将 {\"base64\": \"...\"}（可带 pad 出的 null 兄弟键）展开为 BSON 内容。"""
    if isinstance(data, list):
        return [expand_jobject_base64(x) for x in data]
    if isinstance(data, dict):
        b64 = data.get("base64")
        if isinstance(b64, str) and b64:
            # 仅当像 JObject 包装：有 base64，且其它值全是 null/缺失语义
            others = {k: v for k, v in data.items() if k != "base64"}
            if not others or all(v is None for v in others.values()):
                try:
                    decoded = decode_jobject_bson(b64)
                except Exception:
                    decoded = None
                if isinstance(decoded, dict):
                    return expand_jobject_base64(decoded)
        return {k: expand_jobject_base64(v) for k, v in data.items()}
    return data


# flatten_kv 会把 blackboard 也拍成 dict；无模板时按字段名后缀还原。
# 有参考模板时由 pad_from_template 按节点形状还原（含 valueStr 等）。
TOKEN_BB_KEY = "tokenAttributeBlackboard"


def _is_blackboard_field(name: object) -> bool:
    """blackboard / talentBlackboard / attributeBlackboard / blackBoard 等。"""
    if not isinstance(name, str):
        return False
    return name == "blackBoard" or name.endswith("Blackboard")


def _dict_to_kv_list(d: dict, sample: dict | None = None) -> list:
    """{k: v} → [{key, value, ...}]；字段集对齐 sample（默认含 valueStr）。"""
    if sample is None:
        sample = {"key": "", "value": 0.0, "valueStr": None}
    fields = [f for f in sample.keys() if f != "key"]
    if "value" not in fields:
        fields = ["value", *fields]
    out: list = []
    for k, v in d.items():
        item: dict = {"key": k}
        for f in fields:
            if f == "value":
                item[f] = v
            else:
                item[f] = None
        out.append(item)
    return out


def _looks_like_kv_list_shape(tmpl: object) -> bool:
    """参考 JSON blackboard 节点：[{key, value, ...}, ...]（允许空列表）。"""
    if not isinstance(tmpl, list):
        return False
    if not tmpl:
        return True
    x = tmpl[0]
    return isinstance(x, dict) and "key" in x and "value" in x


def _kv_list_sample(tmpl: list) -> dict:
    if tmpl and isinstance(tmpl[0], dict) and "key" in tmpl[0]:
        return tmpl[0]
    return {"key": "", "value": 0.0, "valueStr": None}


def _unflatten_blackboard_using_tmpl(data: dict, tmpl: list) -> list:
    """拍平后的 {k: v} 按模板样例还原成 [{key, value, valueStr}, ...]。"""
    return _dict_to_kv_list(data, _kv_list_sample(tmpl))


def _normalize_token_attribute_blackboard(v: object) -> object:
    """tokenAttributeBlackboard: {tokenId: [{key,value}, ...]}；空为 {}。"""
    if v == [] or v == {}:
        return {}
    if isinstance(v, dict):
        out: dict = {}
        for tid, bb in v.items():
            if isinstance(bb, dict):
                out[tid] = _dict_to_kv_list(bb)
            else:
                out[tid] = bb
        return out
    # flatten 前/误还原后的 [{key: tokenId, value: dict|list}, ...]
    if _is_kv_pair_list(v):
        out = {}
        for x in v:
            bb = x["value"]
            out[x["key"]] = _dict_to_kv_list(bb) if isinstance(bb, dict) else bb
        return out
    return v


def restore_blackboard_lists(data: object) -> object:
    """*Blackboard / blackBoard → [{key,value,...}]；
    tokenAttributeBlackboard → {tokenId: [{key,value}, ...]}。

    仅作无规则表时的兜底；有 postprocess_rules 时由 apply_named_postprocess_rules 点名处理。
    """
    if isinstance(data, list):
        return [restore_blackboard_lists(x) for x in data]
    if isinstance(data, dict):
        out: dict = {}
        for k, v in data.items():
            v2 = restore_blackboard_lists(v)
            if k == TOKEN_BB_KEY:
                out[k] = _normalize_token_attribute_blackboard(v2)
            elif _is_blackboard_field(k) and isinstance(v2, dict):
                out[k] = _dict_to_kv_list(v2)
            else:
                out[k] = v2
        return out
    return data


def flat_matrix_to_rows(data: object) -> object:
    """{row_size, column_size, matrix_data} → 二维 list（对齐参考 map）。"""
    if not isinstance(data, dict):
        return data
    if not all(k in data for k in ("row_size", "column_size", "matrix_data")):
        return data
    flat = data["matrix_data"]
    if not isinstance(flat, list):
        return data
    try:
        rows = int(data["row_size"])
        cols = int(data["column_size"])
    except (TypeError, ValueError):
        return data
    if rows < 0 or cols <= 0 or len(flat) < rows * cols:
        return data
    return [flat[i * cols : (i + 1) * cols] for i in range(rows)]


def load_postprocess_rules(path: Path | None) -> dict:
    if path is None:
        return {"by_field": {}, "by_path": {}}
    p = Path(path)
    if not p.is_file():
        return {"by_field": {}, "by_path": {}}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {
        "by_field": dict(raw.get("by_field") or {}),
        "by_path": dict(raw.get("by_path") or {}),
    }


def _apply_postprocess_op(val: object, op: str) -> object:
    if op == "dict_to_kv_list":
        return _dict_to_kv_list(val) if isinstance(val, dict) else val
    if op == "token_bb_map":
        return _normalize_token_attribute_blackboard(val)
    if op == "flat_matrix_to_rows":
        return flat_matrix_to_rows(val)
    return val


def apply_named_postprocess_rules(data: object, rules: dict) -> object:
    """按规则表点名处理：by_field（字段名）/ by_path（根到节点的路径）。"""
    by_field: dict = rules.get("by_field") or {}
    by_path: dict = rules.get("by_path") or {}

    def walk(node: object, path: list[str]) -> object:
        path_key = ".".join(path)
        if path_key and path_key in by_path:
            node = _apply_postprocess_op(node, by_path[path_key])

        if isinstance(node, dict):
            out: dict = {}
            for k, v in node.items():
                v2 = walk(v, path + [str(k)])
                if k in by_field:
                    v2 = _apply_postprocess_op(v2, by_field[k])
                out[k] = v2
            return out
        if isinstance(node, list):
            # 列表下标不写入 path，避免打散 by_path
            return [walk(x, path) for x in node]
        return node

    return walk(data, [])


# ---------- 参考 JSON 路径对齐补 null（不跨样本 merge 字段）----------

_MISSING = object()


def _keyset_similarity(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dicts_are_homogeneous(dicts: list[dict]) -> bool:
    if len(dicts) < 2:
        return False
    key_sets = [set(d) for d in dicts]
    ref = max(key_sets, key=len)
    if not ref:
        return False
    similar = sum(1 for ks in key_sets if _keyset_similarity(ks, ref) >= 0.5)
    return similar * 10 >= len(dicts) * 9


def _is_index_key(k: object) -> bool:
    """纯数字键 → 下标 map 的 entry，不是 schema 字段名。"""
    return str(k).isdigit()


def _looks_like_id_map(d: dict) -> bool:
    """开放 id→record 表：至少 2 条、值多为同构 dict。

    单条（如 number.\"1\"、只有 unlockCond 的 SkillLevelCost）不当 map，
    默认按参考 tmpl 做固定 object 补字段。
    """
    if not d:
        return False
    values = list(d.values())
    dict_vals = [v for v in values if isinstance(v, dict)]
    if len(dict_vals) < 2 or len(dict_vals) * 10 < len(values) * 9:
        return False
    return _dicts_are_homogeneous(dict_vals)


def _looks_like_entries(d: dict) -> bool:
    if not d:
        return True
    vals = list(d.values())
    dict_vals = [v for v in vals if isinstance(v, dict)]
    return bool(dict_vals) and len(dict_vals) * 10 >= len(vals) * 9


def _pick_tmpl_sample(tmpl: dict) -> object | None:
    """id map 里挑一条作 value 结构样板（优先字段最多的 dict）。"""
    best = None
    best_n = -1
    for v in tmpl.values():
        if isinstance(v, dict) and len(v) > best_n:
            best, best_n = v, len(v)
    if best is not None:
        return best
    return next(iter(tmpl.values()), None)


def _maybe_rewrap_using_tmpl(data: dict, tmpl: dict) -> dict:
    """按参考模板把单键解包摊到父级的字段收回嵌套 object。

    覆盖：
    - tradingRoomInfoData → tradingRoomSpecialOrderData（整表当 map 内容）
    - unlockCond / unlockCondition：phase、level 回到嵌套里
    - attributes：attributeModifiers 等回到 attributes 里
    """
    data = dict(data)

    # 1) 单字段 wrapper：整包是内层 map/object
    #    跳过数字键（number."1" 是 map 条目，不是 wrapper 字段名）
    if len(tmpl) == 1:
        f, tv = next(iter(tmpl.items()))
        if isinstance(tv, dict) and not _is_index_key(f):
            cur = data.get(f, _MISSING)
            extras = {k: v for k, v in data.items() if k != f}
            if cur is _MISSING and _looks_like_entries(data):
                return {f: data}
            if (cur is None or cur == []) and extras and _looks_like_entries(extras):
                return {f: extras}
            if (cur is None or cur == []) and not extras:
                return {f: {}}

    # 2) 多字段 object：把属于某个嵌套 object 的兄弟键收回去
    for f, tv in tmpl.items():
        if not isinstance(tv, dict) or not tv:
            continue
        # id map / 下标 map 不用「字段名集合」往回 scoop
        if _looks_like_id_map(tv) or _is_index_key(f):
            continue
        nested_keys = set(tv.keys())
        # 当前层模板已有的键是合法同级字段（如根 missionData vs
        # fifthAnnivExploreData.missionData），不能当误摊 scoop 掉
        stolen = {
            k: data[k]
            for k in nested_keys
            if k in data and k != f and k not in tmpl
        }
        if not stolen:
            continue
        cur = data.get(f, _MISSING)
        if cur is _MISSING or cur is None or cur == []:
            data[f] = stolen
            for k in stolen:
                del data[k]
        elif isinstance(cur, dict):
            merged = dict(cur)
            for k, v in stolen.items():
                if k not in merged or merged[k] is None:
                    merged[k] = v
                del data[k]
            data[f] = merged
    return data


def _sync_main_power_fields(data: dict) -> dict:
    """顶层 nationId/groupId/teamId 与 mainPower 对齐（参考 JSON 始终镜像；FBS 顶层常省略）。"""
    mp = data.get("mainPower")
    if not isinstance(mp, dict):
        return data
    if not any(k in data for k in ("nationId", "groupId", "teamId")):
        return data
    out = data
    for k in ("nationId", "groupId", "teamId"):
        if k in data and data.get(k) is None and k in mp:
            if out is data:
                out = dict(data)
            out[k] = mp[k]
    return out


def pad_from_template(data: object, tmpl: object) -> object:
    """按参考 JSON 同路径节点补显式 null；不跨条目合并字段。

    - 固定 object：以 tmpl 的 key 为准补缺（值为 null），保留 data 多出的新字段
    - id→record map：以 data 的 id 为准，用 tmpl[id] 或样板条目对齐 value 结构
    - 空 map：tmpl 为 {} 且 data 为 [] → {}
    - 有 mainPower 时：顶层 nationId/groupId/teamId 为 null 则从 mainPower 拷贝
    - 入口处将 data 的 int 键转为 str，避免与参考 JSON 字符串键错位
    """
    return _pad_from_template_impl(stringify_int_keys(data), tmpl)


def _unwrap_false_index_nest(data: dict, tmpl: dict) -> dict:
    """拆掉 \"1\" vs 1 误反包产生的套层：{type:null,..., \"1\": CondItem} → CondItem。"""
    if _looks_like_id_map(tmpl):
        return data
    index_children = [
        (k, v) for k, v in data.items() if _is_index_key(k) and isinstance(v, dict)
    ]
    if len(index_children) != 1:
        return data
    _ik, iv = index_children[0]
    parent_hit = sum(
        1 for k in tmpl if not _is_index_key(k) and k in data and data.get(k) is not None
    )
    child_hit = sum(1 for k in tmpl if k in iv)
    if child_hit > parent_hit:
        return iv
    return data


def _pad_from_template_impl(data: object, tmpl: object) -> object:
    if data is None:
        return None
    if tmpl is None:
        return data

    # 空 map：flatc/flatten 留下 []
    if tmpl == {} and data == []:
        return {}
    if isinstance(tmpl, dict) and not tmpl and isinstance(data, dict):
        return data

    # flatten_kv 后的 blackboard：{k: v} → 按参考格式 [{key,value,valueStr}, ...]
    if isinstance(data, dict) and _looks_like_kv_list_shape(tmpl):
        # 空模板列表时，仅当 data 像拍平 kv（值非 dict/list）才还原，避免误伤
        if tmpl or all(not isinstance(v, (dict, list)) for v in data.values()):
            data = _unflatten_blackboard_using_tmpl(data, tmpl)

    if isinstance(tmpl, dict) and isinstance(data, dict):
        data = _unwrap_false_index_nest(data, tmpl)
        data = _maybe_rewrap_using_tmpl(data, tmpl)

        # 任一侧像 id map → 不按 tmpl 灌 id，只对齐已有条目的 value
        if _looks_like_id_map(tmpl) or _looks_like_id_map(data):
            sample = _pick_tmpl_sample(tmpl) if tmpl else None
            out: dict = {}
            for k, v in data.items():
                t = tmpl[k] if k in tmpl else sample
                out[k] = _pad_from_template_impl(v, t) if t is not None else v
            return out

        # 固定字段 object：严格按本条 tmpl 的 key（不会并入其它活动的 rewards id）
        out = {}
        for k, tv in tmpl.items():
            if k in data:
                out[k] = _pad_from_template_impl(data[k], tv)
            else:
                out[k] = None
        for k, v in data.items():
            if k in out:
                continue
            # 丢掉「模板没有的 null」（旧 merge 污染）；有值的新字段仍保留
            if v is None:
                continue
            out[k] = v
        return _sync_main_power_fields(out)

    if isinstance(tmpl, list) and isinstance(data, list):
        if not tmpl:
            return data
        sample = tmpl[0]
        return [
            _pad_from_template_impl(x, tmpl[i] if i < len(tmpl) else sample)
            for i, x in enumerate(data)
        ]

    # tmpl 是 {}（空 map）但 data 仍是非空 list：保持 data（不应出现）
    if tmpl == {} and isinstance(data, list):
        return {} if not data else data

    return data


_template_json_cache: dict[str, object] = {}


def load_template_json(template_path: Path) -> object:
    key = str(template_path.resolve())
    if key not in _template_json_cache:
        _template_json_cache[key] = json.loads(
            template_path.read_text(encoding="utf-8")
        )
    return _template_json_cache[key]


def find_template_file(
    dst: Path,
    *,
    out_root: Path,
    template_dir: Path,
) -> Path | None:
    """按输出相对路径在 template_dir 参考 gamedata 下找同名 JSON。"""
    try:
        rel = dst.resolve().relative_to(out_root.resolve())
    except ValueError:
        rel = Path(dst.name)
    cand = template_dir / rel
    if cand.is_file():
        return cand
    matches = list(template_dir.rglob(dst.name))
    if len(matches) == 1:
        return matches[0]
    return None


def out_path_for(bin_path: Path, *, input_root: Path, out_root: Path, table: str | None) -> Path:
    rel = bin_path.resolve().relative_to(input_root.resolve())
    # 关卡共用 prts___levels schema，输出保持源名：level_main_00-01.json（对齐参考命名）
    if table == "prts___levels":
        return out_root / rel.parent / f"{bin_path.stem}.json"
    # excel/character_table9fc534.bytes -> excel/character_table.json
    if table:
        return out_root / rel.parent / f"{table}.json"
    return out_root / rel.with_suffix(".json")


def _flatc_payload_to_json(
    payload: bytes,
    *,
    bin_path: Path,
    schema_name: str,
    schema_path: Path,
    flatc: str,
    defaults_json: bool,
) -> object:
    """对已去 RSA（或已 AES）的 payload 调 flatc，返回 JSON 对象。"""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        bin_no_rsa = td_path / f"{schema_name}.bin"
        bin_no_rsa.write_bytes(payload)
        # flatc 对含非 ASCII 的 schema 路径偶发 Access Violation；拷到临时目录再调
        schema_local = td_path / f"{schema_name}.fbs"
        schema_local.write_bytes(schema_path.read_bytes())

        cmd = [
            flatc,
            "--json",
            "--raw-binary",
            "--strict-json",
            "--allow-non-utf8",
            "--no-warnings",
            "-o",
            str(td_path),
            str(schema_local),
            "--",
            str(bin_no_rsa),
        ]
        if defaults_json:
            cmd.insert(1, "--defaults-json")

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "flatc 失败（可能是 AES 或 schema 不匹配）\n"
                f"file={bin_path}\ncmd={' '.join(cmd)}\n"
                f"rc={proc.returncode}\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )

        produced = next(td_path.glob("*.json"), None)
        if produced is None:
            raise FileNotFoundError(f"flatc 未产出 json: {bin_path}")
        return json.loads(produced.read_text(encoding="utf-8"))


def decode_one(
    bin_path: Path,
    *,
    schema_name: str,
    schema_path: Path,
    flatc: str,
    defaults_json: bool,
    chat_mask: str = "",
) -> object:
    raw = bin_path.read_bytes()
    # 明文 JSON（无签名 / 已解签）优先；签名块首字节也可能是 '{'，不能只看文件头
    for candidate in (raw, strip_header(raw) if len(raw) > RSA_HEADER else None):
        if candidate is None:
            continue
        parsed = try_parse_json_bytes(candidate)
        if parsed is not None:
            return parsed

    payload = strip_header(raw)
    try:
        return _flatc_payload_to_json(
            payload,
            bin_path=bin_path,
            schema_name=schema_name,
            schema_path=schema_path,
            flatc=flatc,
            defaults_json=defaults_json,
        )
    except Exception as flatc_err:
        if not chat_mask:
            raise
        # flatc 失败：AES → 若是 JSON 直接用；否则再 flatc 一次
        plain = try_aes_chatmask_plain(raw, chat_mask)
        if plain is None:
            raise flatc_err
        parsed = try_parse_json_bytes(plain)
        if parsed is not None:
            return parsed
        return _flatc_payload_to_json(
            plain,
            bin_path=bin_path,
            schema_name=schema_name,
            schema_path=schema_path,
            flatc=flatc,
            defaults_json=defaults_json,
        )


def iter_input_files(input_root: Path):
    for p in sorted(input_root.rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in TEXT_SUFFIXES or suf in BINARY_SUFFIXES or suf == "":
            # 跳过明显非数据
            if p.name.startswith("."):
                continue
            yield p


def process_text_file(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".json":
        data = json.loads(src.read_text(encoding="utf-8"))
        dst.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return "copy-json"
    dst.write_bytes(src.read_bytes())
    return "copy"


def logical_stem(stem: str, known_prefixes: frozenset[str] | None = None) -> str:
    """
    去掉末尾可选 hex 指纹，得到逻辑名。
    优先用 schema 最长前缀（与 resolve_schema 一致）；否则仅剥末尾 6 位 hex。
    """
    if known_prefixes:
        for name in sorted(known_prefixes, key=len, reverse=True):
            if stem == name:
                return name
            if stem.startswith(name):
                rest = stem[len(name) :]
                if rest == "" or re.fullmatch(r"[0-9a-fA-F]{4,}", rest):
                    return name
    m = _HEX6_SUFFIX_RE.fullmatch(stem)
    if m:
        return m.group(1)
    return stem


def _http_get(url: str, *, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_json(url: str, *, timeout: float = 120) -> object:
    return json.loads(_http_get(url, timeout=timeout).decode("utf-8"))


def resolve_official_apk_url(
    *,
    page_url: str | None = None,
    apk_url: str | None = None,
    timeout: float = 60,
) -> tuple[str, str]:
    """
    解析官方 Android APK 直链。
    优先 apk_url；否则跟 android_lastest 重定向（无需浏览器）。
    返回 (最终 URL, 建议文件名)。
    """
    start = (apk_url or page_url or _DEFAULT_ANDROID_APK_URL).strip()
    # 兼容仍配置 download 页的情况：从 HTML 抽出 android_lastest
    if "hypergryph.com/download" in start and "android_lastest" not in start:
        req = urllib.request.Request(start, headers={"User-Agent": _ANDROID_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
        m = re.search(
            r"https://ak\.hypergryph\.com/downloads/android_lastest", html
        )
        if not m:
            raise RuntimeError(f"无法从下载页解析 APK 入口: {start}")
        start = m.group(0)

    req = urllib.request.Request(start, headers={"User-Agent": _ANDROID_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final = resp.geturl()
        cd = resp.headers.get("Content-Disposition") or ""
        # 不读 body，避免把整包 APK 拉进内存
    name = "arknights.apk"
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
    if m:
        name = m.group(1).strip()
    else:
        name = Path(urllib.parse.urlparse(final).path).name or name
    if not name.lower().endswith(".apk"):
        name += ".apk"
    return final, name


def _http_download_file(
    url: str,
    dest: Path,
    *,
    timeout: float = 600,
    user_agent: str = _ANDROID_UA,
) -> None:
    """流式下载大文件（APK 约 2GB），带简单进度。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -1
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total > 0:
                pct = done * 100 // total
                if pct != last_pct and pct % 5 == 0:
                    print(
                        f"apk download {pct}% ({done / 1e6:.0f}/{total / 1e6:.0f} MB)",
                        flush=True,
                    )
                    last_pct = pct
    tmp.replace(dest)


def download_official_apk(cfg: dict) -> Path:
    """
    从官网下载 Android APK 到 apk_download_dir（纯 HTTP，无需 Playwright）。
    apk_skip_existing=True 时复用目录内已有 .apk。
    """
    dest_dir = Path(cfg.get("apk_download_dir") or (app_dir() / "apk_download"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    skip = bool(cfg.get("apk_skip_existing", True))
    if skip:
        existing = sorted(
            dest_dir.glob("*.apk"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if existing:
            print(f"apk reuse: {existing[0]}", flush=True)
            return existing[0]

    page_url = cfg.get("apk_download_page")
    apk_entry = cfg.get("apk_android_url") or _DEFAULT_ANDROID_APK_URL
    print(f"apk: resolve {apk_entry}", flush=True)
    final_url, name = resolve_official_apk_url(
        page_url=str(page_url) if page_url else None,
        apk_url=str(apk_entry),
        timeout=float(cfg.get("apk_resolve_timeout") or 60),
    )
    out = dest_dir / name
    print(f"apk: url={final_url}", flush=True)
    print(f"apk: saving -> {out}", flush=True)
    _http_download_file(
        final_url,
        out,
        timeout=float(cfg.get("apk_fetch_timeout_ms") or 600_000) / 1000.0,
    )
    if not out.is_file() or out.stat().st_size < 1_000_000:
        raise RuntimeError(f"APK 下载异常或过小: {out}")
    print(f"apk done: {out} ({out.stat().st_size / 1e6:.1f} MB)", flush=True)
    return out


def ensure_studio_input_apk(cfg: dict) -> Path:
    """已有路径优先；否则 apk_fetch_official 时从官网下载。"""
    cur = cfg.get("studio_input_apk")
    if cur:
        p = Path(cur)
        if p.exists():
            return p
        raise FileNotFoundError(f"studio_input_apk 不存在: {p}")
    if bool(cfg.get("apk_fetch_official")):
        apk = download_official_apk(cfg)
        cfg["studio_input_apk"] = apk
        return apk
    raise FileNotFoundError(
        "未配置 studio_input_apk，且未开启 apk_fetch_official"
    )


def flatten_ab_cdn_name(ab_name: str) -> str:
    """hot_update_list name → CDN 文件名（无路径，.dat）。"""
    flat = ab_name.replace("\\", "/").replace("/", "_").replace("#", "__")
    if "." in flat:
        flat = ".".join(flat.split(".")[:-1]) + ".dat"
    else:
        flat += ".dat"
    return flat


def fetch_res_version(cfg: dict) -> str:
    platform = str(cfg.get("cdn_platform") or "Windows")
    hv = str(cfg.get("cdn_hv") or "").format(platform=platform)
    data = _http_get_json(hv)
    if not isinstance(data, dict) or not data.get("resVersion"):
        raise RuntimeError(f"version API 无 resVersion: {hv} -> {data!r}")
    res_version = str(data["resVersion"])
    print(
        f"cdn version: platform={platform} resVersion={res_version} "
        f"clientVersion={data.get('clientVersion')}",
        flush=True,
    )
    return res_version


def fetch_hot_update_list(cfg: dict, res_version: str) -> dict:
    platform = str(cfg.get("cdn_platform") or "Windows")
    hu = str(cfg.get("cdn_hu") or "https://ak.hycdn.cn/assetbundle/official").rstrip("/")
    url = f"{hu}/{platform}/assets/{res_version}/hot_update_list.json"
    print(f"cdn hot_update_list: {url}", flush=True)
    data = _http_get_json(url, timeout=300)
    if not isinstance(data, dict):
        raise RuntimeError(f"hot_update_list 非 JSON object: {type(data)}")
    return data


def _extract_anon_bin_from_cdn_dat(dat: bytes, *, ab_name: str) -> bytes:
    """CDN anon .dat = ZIP，内含 UnityFS .bin。"""
    if dat[:2] != b"PK":
        # 少数情况下可能是裸 AB；有 RSA 头则剥掉
        if len(dat) > RSA_HEADER and dat[RSA_HEADER : RSA_HEADER + 7] == b"UnityFS":
            return dat[RSA_HEADER:]
        if dat[:7] == b"UnityFS":
            return dat
        raise ValueError(f"非 ZIP/UnityFS: magic={dat[:16]!r}")

    with zipfile.ZipFile(io.BytesIO(dat)) as zf:
        names = zf.namelist()
        prefer = ab_name.replace("\\", "/")
        member = prefer if prefer in names else names[0]
        return zf.read(member)


def _cdn_keep_markers(cfg: dict) -> list[bytes]:
    raw = cfg.get("cdn_keep_markers")
    if raw is None:
        raw = ["gamedata"]
    if isinstance(raw, str):
        raw = [raw]
    out: list[bytes] = []
    seen: set[bytes] = set()
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        b = s.encode("utf-8")
        if b not in seen:
            seen.add(b)
            out.append(b)
        low = s.lower().encode("utf-8")
        if low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _blob_has_markers(data: bytes, markers: list[bytes]) -> bool:
    if not markers:
        return True
    if len(data) <= 16 * 1024 * 1024:
        return any(m in data for m in markers)
    head = data[: 1024 * 1024]
    tail = data[-262144:] if len(data) > 262144 else b""
    return any(m in head or m in tail for m in markers)


def _path_has_markers(path: Path, markers: list[bytes]) -> bool:
    if not markers:
        return True
    size = path.stat().st_size
    with path.open("rb") as f:
        if size <= 16 * 1024 * 1024:
            return _blob_has_markers(f.read(), markers)
        head = f.read(1024 * 1024)
        f.seek(max(0, size - 262144))
        tail = f.read(262144)
    return _blob_has_markers(head, markers) or _blob_has_markers(tail, markers)


def _ab_info_has_markers(ab: dict, markers: list[bytes]) -> bool:
    if not markers:
        return True
    chunks: list[str] = []
    for v in ab.values():
        if isinstance(v, str):
            chunks.append(v)
        elif isinstance(v, (int, float)):
            continue
        else:
            chunks.append(str(v))
    blob = "\n".join(chunks).encode("utf-8", "replace")
    return any(m in blob for m in markers)


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _cleanup_path(path: Path | None, *, reason: str) -> None:
    if path is None:
        return
    p = Path(path)
    if p.is_file():
        print(f"cleanup file ({reason}): {p}", flush=True)
        p.unlink(missing_ok=True)
        return
    if p.is_dir():
        print(f"cleanup dir ({reason}): {p}", flush=True)
        shutil.rmtree(p, ignore_errors=True)


def _download_one_anon(
    *,
    ab: dict,
    dest_dir: Path,
    cdn_base: str,
    skip_existing: bool,
    markers: list[bytes],
) -> str:
    name = str(ab.get("name") or "")
    ab_size = int(ab.get("abSize") or 0)
    out_name = Path(name.replace("\\", "/")).name  # e.g. xxx.bin
    dest = dest_dir / out_name
    info_hit = _ab_info_has_markers(ab, markers) if markers else True

    if skip_existing and dest.is_file() and (ab_size <= 0 or dest.stat().st_size == ab_size):
        if not markers or info_hit or _path_has_markers(dest, markers):
            return "skip"
        dest.unlink(missing_ok=True)
        return "drop"

    url = f"{cdn_base}/{flatten_ab_cdn_name(name)}"
    try:
        raw = _http_get(url, timeout=300)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"下载失败 HTTP {e.code}: {url}") from e

    inner = _extract_anon_bin_from_cdn_dat(raw, ab_name=name)
    if ab_size > 0 and len(inner) != ab_size:
        # 仍写入，但警告（偶发 list 与包不一致）
        print(
            f"cdn warn size mismatch {out_name}: got={len(inner)} abSize={ab_size}",
            flush=True,
        )
    if markers and not info_hit and not _blob_has_markers(inner, markers):
        dest.unlink(missing_ok=True)
        return "drop"
    dest.write_bytes(inner)
    return "ok"


def download_cdn_anon(cfg: dict) -> Path:
    """
    从官方 CDN 拉取 hot_update_list 中 anon/*，解出 UnityFS .bin 到 cdn_anon_dir。
    cdn_keep_markers 非空时只保留内容/条目像 gamedata 的 bundle。
    返回目录路径（供 Studio 作热更输入）。
    """
    dest_dir = Path(cfg.get("cdn_anon_dir") or Path(cfg["input_dir"]).parent / "cdn_anon")
    dest_dir.mkdir(parents=True, exist_ok=True)

    res_version = fetch_res_version(cfg)
    hot = fetch_hot_update_list(cfg, res_version)
    version_id = str(hot.get("versionId") or res_version)
    if version_id != res_version:
        print(f"cdn note: list.versionId={version_id} != resVersion={res_version}", flush=True)

    ab_infos = hot.get("abInfos") or []
    anon_abs = [
        a
        for a in ab_infos
        if isinstance(a, dict) and str(a.get("name") or "").replace("\\", "/").startswith("anon/")
    ]
    if not anon_abs:
        raise RuntimeError("hot_update_list 中没有任何 anon/ 条目")

    platform = str(cfg.get("cdn_platform") or "Windows")
    hu = str(cfg.get("cdn_hu") or "https://ak.hycdn.cn/assetbundle/official").rstrip("/")
    cdn_base = f"{hu}/{platform}/assets/{version_id}"
    skip_existing = bool(cfg.get("cdn_skip_existing", True))
    workers = max(1, int(cfg.get("cdn_workers") or 8))
    markers = _cdn_keep_markers(cfg)
    marker_labels = [m.decode("utf-8", "replace") for m in markers]

    print(
        f"cdn anon: count={len(anon_abs)} -> {dest_dir} workers={workers} "
        f"skip_existing={skip_existing} keep_markers={marker_labels or 'ALL'}",
        flush=True,
    )

    ok = skip = drop = fail = 0
    errors: list[str] = []

    def _job(ab: dict) -> tuple[str, str]:
        try:
            status = _download_one_anon(
                ab=ab,
                dest_dir=dest_dir,
                cdn_base=cdn_base,
                skip_existing=skip_existing,
                markers=markers,
            )
            return status, str(ab.get("name") or "")
        except Exception as e:
            return "fail", f"{ab.get('name')}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_job, ab) for ab in anon_abs]
        done = 0
        for fut in as_completed(futs):
            status, detail = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            elif status == "drop":
                drop += 1
            else:
                fail += 1
                errors.append(detail)
            if done % 20 == 0 or done == len(futs):
                print(
                    f"cdn progress {done}/{len(futs)} ok={ok} skip={skip} "
                    f"drop={drop} fail={fail}",
                    flush=True,
                )

    meta = {
        "platform": platform,
        "resVersion": res_version,
        "versionId": version_id,
        "anon_count": len(anon_abs),
        "keep_markers": marker_labels,
        "downloaded": ok,
        "skipped": skip,
        "dropped": drop,
        "failed": fail,
    }
    (dest_dir / "_cdn_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if fail:
        preview = "; ".join(errors[:5])
        raise RuntimeError(f"cdn anon 下载失败 {fail} 个，例如: {preview}")
    if markers and (ok + skip) == 0:
        raise RuntimeError(
            f"cdn anon 过滤后为空（markers={marker_labels}）。"
            "可将 cdn_keep_markers 设为 [] 以保留全部 bundle。"
        )

    print(
        f"cdn anon done: ok={ok} skip={skip} drop={drop} dir={dest_dir}",
        flush=True,
    )
    return dest_dir


def _run_studio_cli(cli: Path, studio_in: Path, studio_out: Path, extra: list[str], label: str) -> None:
    if not studio_in.exists():
        raise FileNotFoundError(f"studio_input[{label}] 不存在: {studio_in}")
    studio_out.mkdir(parents=True, exist_ok=True)
    cmd = [str(cli), str(studio_in), *extra, "-o", str(studio_out)]
    print(f"studio[{label}]:", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ArknightsStudioCLI[{label}] 失败 rc={proc.returncode}")
    print(f"studio[{label}] done -> {studio_out}", flush=True)


def merge_hot_over_apk(
    apk_root: Path,
    hot_root: Path,
    *,
    known_prefixes: frozenset[str] | None = None,
) -> tuple[int, int]:
    """
    热更侧为权威：按「同目录 + 逻辑 stem」删掉首包旧文件，再拷入热更文件。
    返回 (copied, removed)。
    """
    if not hot_root.is_dir():
        raise FileNotFoundError(f"热更导出目录不存在: {hot_root}")
    apk_root.mkdir(parents=True, exist_ok=True)

    copied = removed = 0
    for hot_file in sorted(hot_root.rglob("*")):
        if not hot_file.is_file():
            continue
        rel = hot_file.relative_to(hot_root)
        dst = apk_root / rel
        dst_parent = dst.parent
        dst_parent.mkdir(parents=True, exist_ok=True)
        logic = logical_stem(hot_file.stem, known_prefixes)

        for old in list(dst_parent.iterdir()):
            if not old.is_file():
                continue
            if logical_stem(old.stem, known_prefixes) != logic:
                continue
            old.unlink()
            removed += 1

        shutil.copy2(hot_file, dst)
        copied += 1

    print(f"merge hot->apk: copied={copied} removed_same_logical={removed}", flush=True)
    return copied, removed


def run_arknights_studio(cfg: dict) -> Path:
    """apk → input_dir；hot → studio_hot_tmp；再按逻辑名前缀合并（热更为权威）。

    cdn_fetch_anon=True 时：从官方 CDN 拉 anon/* 到 cdn_anon_dir，作为热更输入
    （不再依赖本机 PersistentData/Bundles/anon）。

    cleanup_intermediates=True 时：APK 导出后删除官网缓存包；热更导出后删除
    cdn_anon_dir；合并后删除 studio_hot_tmp。CDN 在 APK 处理完后再下，避免叠峰值。
    """
    cli = Path(cfg["studio_cli"])
    apk_out = Path(cfg["input_dir"])
    hot_tmp = Path(cfg.get("studio_hot_tmp") or (apk_out.parent / f"{apk_out.name}_hot_tmp"))
    extra = list(cfg.get("studio_args") or [])
    cleanup = bool(cfg.get("cleanup_intermediates"))
    apk_dir = Path(cfg.get("apk_download_dir") or (app_dir() / "apk_download"))
    cdn_dir = Path(cfg.get("cdn_anon_dir") or (apk_out.parent / "cdn_anon"))

    if not cli.is_file():
        raise FileNotFoundError(f"studio_cli 不存在: {cli}")

    known: frozenset[str] | None = None
    fbs_dir = cfg.get("fbs_dir")
    if fbs_dir and Path(fbs_dir).is_dir():
        known = frozenset(build_schema_index(Path(fbs_dir)))

    apk_in = cfg.get("studio_input_apk")
    hot_in = cfg.get("studio_input_hot")
    fetched_cdn = False

    if bool(cfg.get("apk_fetch_official")) and not apk_in:
        apk_in = ensure_studio_input_apk(cfg)

    if not apk_in and not hot_in and not cfg.get("cdn_fetch_anon") and cfg.get("studio_input"):
        _run_studio_cli(cli, Path(cfg["studio_input"]), apk_out, extra, "legacy")
        return apk_out

    if not apk_in and not hot_in and not cfg.get("cdn_fetch_anon"):
        raise FileNotFoundError(
            "未配置 studio 输入：请设 cdn_fetch_anon=True，或 studio_input_apk / studio_input_hot"
        )

    if apk_in:
        _run_studio_cli(cli, Path(apk_in), apk_out, extra, "apk")
        if cleanup and _path_is_under(Path(apk_in), apk_dir):
            _cleanup_path(Path(apk_in), reason="after apk studio")

    if bool(cfg.get("cdn_fetch_anon")):
        hot_in = download_cdn_anon(cfg)
        fetched_cdn = True

    if hot_in:
        # 仅 CDN/热更、无首包时：直接导出到 input_dir，避免空合并
        if not apk_in:
            _run_studio_cli(
                cli, Path(hot_in), apk_out, extra, "cdn" if cfg.get("cdn_fetch_anon") else "hot"
            )
        else:
            if hot_tmp.exists():
                shutil.rmtree(hot_tmp)
            label = "cdn" if cfg.get("cdn_fetch_anon") else "hot"
            _run_studio_cli(cli, Path(hot_in), hot_tmp, extra, label)
            merge_hot_over_apk(apk_out, hot_tmp, known_prefixes=known)
        if cleanup and fetched_cdn:
            _cleanup_path(cdn_dir, reason="after cdn studio")
        if cleanup:
            _cleanup_path(hot_tmp, reason="after hot merge")

    return apk_out


def _prompt_line(msg: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        s = input(f"{msg}{hint}: ").strip()
    except EOFError:
        return default
    return s or default


def _resolve_existing_path(raw: str, *, base: Path) -> Path | None:
    if not raw:
        return None
    p = Path(raw.strip().strip('"'))
    if not p.is_absolute():
        p = (base / p).resolve()
    return p if p.exists() else None


def find_studio_cli(cfg: dict) -> Path | None:
    """配置项 → 打包内置 → exe 旁常见文件名。"""
    base = app_dir()
    bundle = bundle_dir()
    cur = cfg.get("studio_cli")
    if cur:
        p = Path(cur)
        if p.is_file():
            return p
    names = (
        "ArknightsStudioCLI.exe",
        "ArknightsStudioCLI",
        "AssetStudioCLI.exe",
    )
    search_roots = [
        bundle / "studio",
        bundle,
        base / "studio",
        base,
    ]
    for root in search_roots:
        if not root.is_dir() and root != bundle and root != base:
            continue
        for name in names:
            cand = root / name
            if cand.is_file():
                return cand
        # 解压后可能多一层目录
        if root.is_dir():
            for name in names:
                matches = list(root.rglob(name))
                if matches:
                    return matches[0]
    return None


def ensure_studio_cli(cfg: dict) -> Path:
    cli = find_studio_cli(cfg)
    if cli is not None:
        cfg["studio_cli"] = cli
        print(f"studio_cli={cli}", flush=True)
        return cli
    print("未找到内置/本地 ArknightsStudioCLI。")
    print("请放到 exe 同目录。")
    print("下载参考: https://github.com/aelurum/AssetStudio/releases")
    raw = _prompt_line("studio_cli 路径")
    p = _resolve_existing_path(raw, base=app_dir())
    if p is None or not p.is_file():
        raise FileNotFoundError(f"studio_cli 无效: {raw!r}")
    cfg["studio_cli"] = p
    return p


def parse_cli_mode(argv: list[str]) -> str | None:
    """--mode decode|cdn|full|full-local|cdn-studio|studio|menu|batch 或 -m。"""
    for i, a in enumerate(argv):
        if a in ("--mode", "-m") and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--mode="):
            return a.split("=", 1)[1].strip().lower()
    if "--menu" in argv:
        return "menu"
    if "--batch" in argv:
        return "batch"
    return None


def show_menu() -> str:
    print()
    print("=" * 56)
    print("  Ark-FbsDecoder  —  FlatBuffers / AES → JSON")
    print("=" * 56)
    print("  1) 解码 data/ → output/              （已有 .bytes）")
    print("  2) 仅下载 CDN anon 热更包")
    print("  3) 全流程：官网 APK + CDN 热更 → 导出 → 解码  ★")
    print("  4) 全流程：本地 APK + 本地热更 → 导出 → 解码")
    print("  5) 仅 CDN 热更 → 导出 → 解码（无安装包）")
    print("  6) 自定义：仅 APK 或仅本地热更")
    print("  0) 退出")
    print("=" * 56)
    print("  ★ 推荐：自动下官网 APK + CDN 热更，内置 Studio 导出后解码")
    while True:
        choice = _prompt_line("请选择", "3")
        if choice in ("0", "1", "2", "3", "4", "5", "6"):
            return choice
        print("无效选项，请输入 0-6")


def _prompt_required_apk(cfg: dict) -> Path:
    base = app_dir()
    default = str(cfg.get("studio_input_apk") or "")
    print("请提供首包路径：解压后的 APK 内 assets/AB/... 目录，或安装包资源目录。")
    while True:
        apk_raw = _prompt_line("studio_input_apk", default)
        apk = _resolve_existing_path(apk_raw, base=base) if apk_raw else None
        if apk is not None:
            return apk
        print("路径无效或不存在，APK/首包为必填。")


def _prompt_required_hot(cfg: dict) -> Path:
    base = app_dir()
    default = str(cfg.get("studio_input_hot") or "")
    print("请提供本机热更目录（如 Bundles 或 anon）。")
    while True:
        hot_raw = _prompt_line("studio_input_hot", default)
        hot = _resolve_existing_path(hot_raw, base=base) if hot_raw else None
        if hot is not None:
            return hot
        print("路径无效或不存在，本地热更为必填。")


def apply_menu_choice(cfg: dict, choice: str) -> str:
    """
    根据菜单设置 cfg，返回下一步动作：
    decode | cdn_only | exit
    """
    base = app_dir()
    if choice == "0":
        return "exit"

    if choice == "1":
        cfg["run_studio_first"] = False
        cfg["cdn_fetch_anon"] = False
        return "decode"

    if choice == "2":
        return "cdn_only"

    # 3) 全流程：官网自动下 APK + CDN 热更
    if choice == "3":
        ensure_studio_cli(cfg)
        cfg["run_studio_first"] = True
        cfg["cdn_fetch_anon"] = True
        cfg["apk_fetch_official"] = True
        cfg["studio_input_apk"] = None
        cfg["studio_input_hot"] = None
        print(
            "将：官网下载 APK → Studio 导出 → CDN anon → 合并 → 解码\n"
            "（APK 约 2GB，需网络；工具链已尽量打进 exe）"
        )
        return "decode"

    # 4) 全流程：本地 APK + 本地热更
    if choice == "4":
        ensure_studio_cli(cfg)
        cfg["run_studio_first"] = True
        cfg["cdn_fetch_anon"] = False
        cfg["apk_fetch_official"] = False
        cfg["studio_input_apk"] = _prompt_required_apk(cfg)
        cfg["studio_input_hot"] = _prompt_required_hot(cfg)
        print("将：导出 APK → 导出本地热更 → 合并 → 解码")
        return "decode"

    # 5) 仅 CDN 热更（无首包）
    if choice == "5":
        ensure_studio_cli(cfg)
        cfg["cdn_fetch_anon"] = True
        cfg["apk_fetch_official"] = False
        cfg["run_studio_first"] = True
        cfg["studio_input_apk"] = None
        cfg["studio_input_hot"] = None
        print("注意：无 APK 首包时，结果可能不完整（仅热更 anon）。")
        return "decode"

    # 6) 自定义单源
    if choice == "6":
        ensure_studio_cli(cfg)
        cfg["cdn_fetch_anon"] = False
        cfg["apk_fetch_official"] = False
        cfg["run_studio_first"] = True
        print("可只填其一：仅 APK，或仅本地热更。")
        apk_raw = _prompt_line(
            "studio_input_apk（可空）",
            str(cfg.get("studio_input_apk") or ""),
        )
        hot_raw = _prompt_line(
            "studio_input_hot（可空）",
            str(cfg.get("studio_input_hot") or ""),
        )
        apk = _resolve_existing_path(apk_raw, base=base) if apk_raw else None
        hot = _resolve_existing_path(hot_raw, base=base) if hot_raw else None
        if apk is None and hot is None:
            raise FileNotFoundError("APK 与热更路径至少填一个，且路径需存在")
        cfg["studio_input_apk"] = apk
        cfg["studio_input_hot"] = hot
        return "decode"

    raise ValueError(f"未知菜单项: {choice}")


def mode_to_choice(mode: str) -> str | None:
    mapping = {
        "decode": "1",
        "1": "1",
        "cdn": "2",
        "2": "2",
        "full": "3",
        "full-cdn": "3",
        "apk-cdn": "3",
        "3": "3",
        "full-local": "4",
        "apk-hot": "4",
        "4": "4",
        "cdn-studio": "5",
        "cdn_studio": "5",
        "5": "5",
        "studio": "6",
        "6": "6",
        "exit": "0",
        "0": "0",
    }
    return mapping.get(mode)


def run_decode(cfg: dict) -> None:
    input_root: Path = cfg["input_dir"]
    out_root: Path = cfg["out_dir"]
    fbs_dir: Path = cfg["fbs_dir"]
    flatc: str = str(cfg["flatc"])
    template_dir = cfg.get("template_dir")
    template_dir_path = Path(template_dir) if template_dir else None
    defaults_json: bool = bool(cfg.get("defaults_json", True))
    flatten_kv: bool = bool(cfg.get("flatten_kv", True))
    skip_existing: bool = bool(cfg.get("skip_existing", False))
    chat_mask: str = str(cfg.get("chat_mask") or "").strip()
    use_template_pad: bool = bool(cfg.get("use_template_pad", True))
    postprocess_rules = load_postprocess_rules(cfg.get("postprocess_rules"))

    if not input_root.is_dir():
        raise FileNotFoundError(f"input_dir 不存在: {input_root}")
    if not any(p.is_file() for p in input_root.rglob("*")):
        raise FileNotFoundError(
            f"input_dir 为空: {input_root}\n"
            "请将 gamedata 下的 .bytes 放入 data/，或先选菜单 3/4 导出。"
        )
    if not fbs_dir.is_dir():
        raise FileNotFoundError(f"fbs_dir 不存在: {fbs_dir}")
    if not Path(flatc).is_file():
        try:
            subprocess.run([flatc, "--version"], capture_output=True, check=True)
        except Exception as e:
            raise FileNotFoundError(f"flatc 不可用: {flatc}") from e

    schemas = build_schema_index(fbs_dir)
    print(f"schemas={len(schemas)} input={input_root} out={out_root}")
    if template_dir_path:
        print(f"template_dir={template_dir_path} (path-align, no shape merge)")
    print(
        f"postprocess_rules: fields={len(postprocess_rules.get('by_field') or {})} "
        f"paths={len(postprocess_rules.get('by_path') or {})}"
    )
    if chat_mask:
        print(f"chat_mask=set (len={len(chat_mask)})")
    else:
        print("chat_mask=empty (AES fallback disabled)")

    def postprocess_and_write(data: object, dst: Path) -> str:
        tpl_tag = "raw"
        if flatten_kv:
            data = maybe_flatten_kv(data)
            data = unwrap_list_values(data)
            data = expand_jobject_base64(data)
            if postprocess_rules.get("by_field") or postprocess_rules.get("by_path"):
                data = apply_named_postprocess_rules(data, postprocess_rules)
            else:
                data = restore_blackboard_lists(data)
            tpl_tag = "no-template"
            if (
                use_template_pad
                and template_dir_path
                and template_dir_path.is_dir()
            ):
                tpl = find_template_file(
                    dst, out_root=out_root, template_dir=template_dir_path
                )
                if tpl is not None:
                    data = pad_from_template(data, load_template_json(tpl))
                    tpl_tag = tpl.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return tpl_tag

    ok = skip = fail = copy_n = 0
    skip_rows: list[str] = []
    fail_rows: list[str] = []

    def _rel(p: Path) -> str:
        return str(p.relative_to(input_root))

    for src in iter_input_files(input_root):
        suf = src.suffix.lower()

        if suf in TEXT_SUFFIXES:
            dst = out_root / src.resolve().relative_to(input_root.resolve())
            if skip_existing and dst.is_file():
                skip_rows.append(f"skip_existing\t{_rel(src)}")
                skip += 1
                continue
            try:
                process_text_file(src, dst)
                copy_n += 1
                print("copy", src.relative_to(input_root), "->", dst.relative_to(out_root))
            except Exception as e:
                fail_rows.append(f"copy\t{_rel(src)}\t{e}")
                fail += 1
                print("FAIL copy", src, e)
            continue

        resolved = resolve_schema(src, input_root=input_root, schemas=schemas)
        if resolved is None:
            dst = out_path_for(src, input_root=input_root, out_root=out_root, table=None)
            if skip_existing and dst.is_file():
                skip_rows.append(f"skip_existing\t{_rel(src)}")
                skip += 1
                continue
            if not chat_mask:
                print("SKIP no-schema (no chat_mask)", src.relative_to(input_root))
                skip_rows.append(f"no-schema\t{_rel(src)}")
                skip += 1
                continue
            try:
                raw = src.read_bytes()
            except Exception as e:
                fail_rows.append(f"read\t{_rel(src)}\t{e}")
                fail += 1
                print("FAIL read", src.relative_to(input_root), e)
                continue
            data = try_aes_chatmask_json(raw, chat_mask)
            if data is None:
                print(
                    "SKIP no-schema AES-fail",
                    src.relative_to(input_root),
                    "(无匹配 .fbs，且 CHAT_MASK 解密未得到 JSON)",
                )
                skip_rows.append(f"aes-fail\t{_rel(src)}")
                skip += 1
                continue
            try:
                tpl_tag = postprocess_and_write(data, dst)
                ok += 1
                nkeys = len(data) if isinstance(data, dict) else type(data).__name__
                print(
                    "ok",
                    src.relative_to(input_root),
                    "->",
                    dst.relative_to(out_root),
                    f"(aes, keys={nkeys}, tpl={tpl_tag})",
                )
            except Exception as e:
                fail_rows.append(f"aes-write\t{_rel(src)}\t{e}")
                fail += 1
                print("FAIL aes-write", src.relative_to(input_root), e)
            continue

        table, schema_path = resolved
        dst = out_path_for(src, input_root=input_root, out_root=out_root, table=table)
        if skip_existing and dst.is_file():
            skip_rows.append(f"skip_existing\t{_rel(src)}")
            skip += 1
            continue

        try:
            data = decode_one(
                src,
                schema_name=table,
                schema_path=schema_path,
                flatc=flatc,
                defaults_json=defaults_json,
                chat_mask=chat_mask,
            )
            tpl_tag = postprocess_and_write(data, dst)
            ok += 1
            nkeys = len(data) if isinstance(data, dict) else type(data).__name__
            print(
                "ok",
                src.relative_to(input_root),
                "->",
                dst.relative_to(out_root),
                f"({table}, keys={nkeys}, tpl={tpl_tag})",
            )
        except Exception as e:
            fail_rows.append(f"decode\t{_rel(src)}\t{e}")
            fail += 1
            print("FAIL", src.relative_to(input_root), e)

    print(f"done ok={ok} copy={copy_n} skip={skip} fail={fail}")
    report = out_root / "decode_issues.txt"
    out_root.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "\n".join(
            [
                f"ok={ok} copy={copy_n} skip={skip} fail={fail}",
                "",
                f"=== FAIL ({len(fail_rows)}) ===",
                *fail_rows,
                "",
                f"=== SKIP ({len(skip_rows)}) ===",
                *skip_rows,
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"issues -> {report}")
    if fail_rows:
        print("--- FAIL ---")
        for row in fail_rows:
            print(row)


def _pause_if_needed() -> None:
    if is_frozen() or sys.stdin.isatty():
        try:
            input("\n按 Enter 退出...")
        except EOFError:
            pass


def main(argv: list[str] | None = None) -> None:
    start_time = time.perf_counter()
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = load_config()
    cli_mode = parse_cli_mode(argv)

    # exe 默认菜单；源码默认按 config（--batch）；可用 --menu / --mode 覆盖
    use_menu = False
    if cli_mode == "batch":
        use_menu = False
    elif cli_mode == "menu" or (cli_mode is None and is_frozen()):
        use_menu = True
    elif cli_mode is None and "--menu" in argv:
        use_menu = True

    action = "decode"
    try:
        if use_menu and (cli_mode is None or cli_mode == "menu"):
            choice = show_menu()
            action = apply_menu_choice(cfg, choice)
        elif cli_mode and cli_mode not in ("batch", "menu"):
            choice = mode_to_choice(cli_mode)
            if choice is None:
                raise SystemExit(
                    f"未知 --mode={cli_mode!r}，可选: "
                    "decode|cdn|full|full-local|cdn-studio|studio|menu|batch"
                )
            action = apply_menu_choice(cfg, choice)
        else:
            # batch / 配置驱动
            action = "decode"

        if action == "exit":
            print("已退出。")
            return

        if action == "cdn_only":
            dest = download_cdn_anon(cfg)
            print(f"CDN anon 已保存到: {dest}")
        else:
            if bool(cfg.get("run_studio_first")):
                run_arknights_studio(cfg)
            run_decode(cfg)
            if bool(cfg.get("cleanup_intermediates")):
                _cleanup_path(Path(cfg["input_dir"]), reason="after decode")

    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        if is_frozen():
            _pause_if_needed()
            raise SystemExit(1) from e
        raise

    end_time = time.perf_counter()
    print(f"本次耗时: {end_time - start_time:.1f} 秒")
    if is_frozen() or use_menu:
        _pause_if_needed()


if __name__ == "__main__":
    main()
