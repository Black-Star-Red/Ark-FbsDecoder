from __future__ import annotations

import base64
import io
import json
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import time
import os
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

_CONFIG_PATH = Path(
    os.environ.get(
        "CONFIG_PATH",
        Path(__file__).resolve().parent / "config.local.json",
    )
)


def load_config() -> dict:
    if not _CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"配置文件不存在: {_CONFIG_PATH}\n"
            "请复制 config.example.json 为 config.local.json 并填写本机路径。"
        )
    raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    base = Path(__file__).resolve().parent
    cfg = dict(raw)

    def _resolve_path(value: object) -> Path | None:
        if value is None or value == "":
            return None
        p = Path(str(value))
        return p if p.is_absolute() else (base / p).resolve()

    for key in (
        "input_dir",
        "out_dir",
        "fbs_dir",
        "template_dir",
        "postprocess_rules",
        "cdn_anon_dir",
        "studio_hot_tmp",
        "studio_input_apk",
        "studio_input_hot",
    ):
        if key in raw:
            cfg[key] = _resolve_path(raw.get(key))

    if raw.get("flatc"):
        flatc = str(raw["flatc"])
        p = Path(flatc)
        if p.is_absolute():
            cfg["flatc"] = str(p)
        elif (base / p).is_file():
            cfg["flatc"] = str((base / p).resolve())
        else:
            cfg["flatc"] = flatc
    else:
        cfg["flatc"] = "flatc"

    cfg["studio_cli"] = _resolve_path(raw.get("studio_cli"))
    cfg["chat_mask"] = os.environ.get("CHAT_MASK", "").strip() or str(
        raw.get("chat_mask") or ""
    ).strip()
    return cfg


CONFIG = load_config()

RSA_HEADER = 128
BINARY_SUFFIXES = {".bytes", ".bin", ".dat"}
TEXT_SUFFIXES = {".json", ".txt", ".lua"}
_HEX6_SUFFIX_RE = re.compile(r"^(.+)([0-9a-fA-F]{6})$")


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


def _download_one_anon(
    *,
    ab: dict,
    dest_dir: Path,
    cdn_base: str,
    skip_existing: bool,
) -> str:
    name = str(ab.get("name") or "")
    ab_size = int(ab.get("abSize") or 0)
    out_name = Path(name.replace("\\", "/")).name  # e.g. xxx.bin
    dest = dest_dir / out_name
    if skip_existing and dest.is_file() and (ab_size <= 0 or dest.stat().st_size == ab_size):
        return "skip"

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
    dest.write_bytes(inner)
    return "ok"


def download_cdn_anon(cfg: dict) -> Path:
    """
    从官方 CDN 拉取 hot_update_list 中全部 anon/*，解出 UnityFS .bin 到 cdn_anon_dir。
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

    print(
        f"cdn anon: count={len(anon_abs)} -> {dest_dir} workers={workers} skip_existing={skip_existing}",
        flush=True,
    )

    ok = skip = fail = 0
    errors: list[str] = []

    def _job(ab: dict) -> tuple[str, str]:
        try:
            status = _download_one_anon(
                ab=ab, dest_dir=dest_dir, cdn_base=cdn_base, skip_existing=skip_existing
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
            else:
                fail += 1
                errors.append(detail)
            if done % 20 == 0 or done == len(futs):
                print(f"cdn progress {done}/{len(futs)} ok={ok} skip={skip} fail={fail}", flush=True)

    meta = {
        "platform": platform,
        "resVersion": res_version,
        "versionId": version_id,
        "anon_count": len(anon_abs),
        "downloaded": ok,
        "skipped": skip,
        "failed": fail,
    }
    (dest_dir / "_cdn_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if fail:
        preview = "; ".join(errors[:5])
        raise RuntimeError(f"cdn anon 下载失败 {fail} 个，例如: {preview}")

    print(f"cdn anon done: ok={ok} skip={skip} dir={dest_dir}", flush=True)
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
    """
    cli = Path(cfg["studio_cli"])
    apk_out = Path(cfg["input_dir"])
    hot_tmp = Path(cfg.get("studio_hot_tmp") or (apk_out.parent / f"{apk_out.name}_hot_tmp"))
    extra = list(cfg.get("studio_args") or [])

    if not cli.is_file():
        raise FileNotFoundError(f"studio_cli 不存在: {cli}")

    known: frozenset[str] | None = None
    fbs_dir = cfg.get("fbs_dir")
    if fbs_dir and Path(fbs_dir).is_dir():
        known = frozenset(build_schema_index(Path(fbs_dir)))

    apk_in = cfg.get("studio_input_apk")
    hot_in = cfg.get("studio_input_hot")

    if bool(cfg.get("cdn_fetch_anon")):
        hot_in = download_cdn_anon(cfg)

    if not apk_in and not hot_in and cfg.get("studio_input"):
        _run_studio_cli(cli, Path(cfg["studio_input"]), apk_out, extra, "legacy")
        return apk_out

    if not apk_in and not hot_in:
        raise FileNotFoundError(
            "未配置 studio 输入：请设 cdn_fetch_anon=True，或 studio_input_apk / studio_input_hot"
        )

    if apk_in:
        _run_studio_cli(cli, Path(apk_in), apk_out, extra, "apk")

    if hot_in:
        # 仅 CDN/热更、无首包时：直接导出到 input_dir，避免空合并
        if not apk_in:
            if apk_out.exists():
                # 保留目录，Studio -o 写入；先清掉旧 gamedata 避免脏文件可选，这里不 rm 整树
                pass
            _run_studio_cli(cli, Path(hot_in), apk_out, extra, "cdn" if cfg.get("cdn_fetch_anon") else "hot")
        else:
            if hot_tmp.exists():
                shutil.rmtree(hot_tmp)
            label = "cdn" if cfg.get("cdn_fetch_anon") else "hot"
            _run_studio_cli(cli, Path(hot_in), hot_tmp, extra, label)
            merge_hot_over_apk(apk_out, hot_tmp, known_prefixes=known)

    return apk_out


def main() -> None:
    start_time = time.perf_counter()

    # 1) 可选：Studio apk + hot（热更临时目录）→ 前缀合并 → 解码
    if bool(CONFIG.get("run_studio_first")):
        run_arknights_studio(CONFIG)

    input_root: Path = CONFIG["input_dir"]
    out_root: Path = CONFIG["out_dir"]
    fbs_dir: Path = CONFIG["fbs_dir"]
    flatc: str = str(CONFIG["flatc"])
    template_dir = CONFIG.get("template_dir")
    template_dir_path = Path(template_dir) if template_dir else None
    defaults_json: bool = bool(CONFIG.get("defaults_json", True))
    flatten_kv: bool = bool(CONFIG.get("flatten_kv", True))
    skip_existing: bool = bool(CONFIG.get("skip_existing", False))
    chat_mask: str = str(CONFIG.get("chat_mask") or "").strip()
    use_template_pad: bool = bool(CONFIG.get("use_template_pad", True))
    postprocess_rules = load_postprocess_rules(CONFIG.get("postprocess_rules"))

    if not input_root.is_dir():
        raise FileNotFoundError(f"input_dir 不存在: {input_root}")
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
            # 点名规则（map 矩阵 / 指定 blackboard 字段）；不靠后缀猜测
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
    for src in iter_input_files(input_root):
        suf = src.suffix.lower()

        if suf in TEXT_SUFFIXES:
            dst = out_root / src.resolve().relative_to(input_root.resolve())
            if skip_existing and dst.is_file():
                skip += 1
                continue
            try:
                process_text_file(src, dst)
                copy_n += 1
                print("copy", src.relative_to(input_root), "->", dst.relative_to(out_root))
            except Exception as e:
                fail += 1
                print("FAIL copy", src, e)
            continue

        resolved = resolve_schema(src, input_root=input_root, schemas=schemas)
        if resolved is None:
            # 无 fbs：尝试 AES(CHAT_MASK)；再不行打日志并跳过
            dst = out_path_for(src, input_root=input_root, out_root=out_root, table=None)
            if skip_existing and dst.is_file():
                skip += 1
                continue
            if not chat_mask:
                print("SKIP no-schema (no chat_mask)", src.relative_to(input_root))
                skip += 1
                continue
            try:
                raw = src.read_bytes()
            except Exception as e:
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
                fail += 1
                print("FAIL aes-write", src.relative_to(input_root), e)
            continue

        table, schema_path = resolved
        dst = out_path_for(src, input_root=input_root, out_root=out_root, table=table)
        if skip_existing and dst.is_file():
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
            fail += 1
            print("FAIL", src.relative_to(input_root), e)

    print(f"done ok={ok} copy={copy_n} skip={skip} fail={fail}")
    end_time = time.perf_counter()
    print(f"本次耗时: {end_time - start_time} seconds")


if __name__ == "__main__":
    main()
