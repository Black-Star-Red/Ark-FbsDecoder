"""从 zh_CN 生成 ensure_null 规则（仅 schema 字段名，排除 act53side 等 record id）。

用法: python gen_ensure_null_rules.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import fbs_to_json as m

OUT_ROOT = Path("dist/output")
ZH_ROOT = Path("zh_CN/gamedata")
RULES_PATH = Path("k_postprocess_rules.json")


def norm(p: Path) -> str:
    return str(p).replace("\\", "/")


def should_process(name: str, out_path: Path) -> bool:
    s = norm(out_path).lower()
    if "/levels/" in s or "/story/" in s or "/storyreview/" in s:
        return False
    if "/excel/" in s or "/battle/" in s:
        return True
    if (
        name.endswith("_table.json")
        or name.endswith("_data.json")
        or name.endswith("_db.json")
    ):
        return True
    return False


def collect_shapes(db: object) -> dict[str, set[str]]:
    """id-map / list[dict] 的 schema 字段 → 子 record 键并集。"""
    shapes: dict[str, set[str]] = defaultdict(set)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if not m._is_schema_field_name(str(k)):
                    walk(v)
                    continue
                if isinstance(v, dict) and m._looks_like_id_map(v):
                    for entry in v.values():
                        if isinstance(entry, dict):
                            shapes[k].update(
                                f for f in entry if m._KEY_RE.match(str(f))
                            )
                            walk(entry)
                elif isinstance(v, list) and v and isinstance(v[0], dict):
                    for entry in v:
                        if isinstance(entry, dict):
                            shapes[k].update(
                                f for f in entry if m._KEY_RE.match(str(f))
                            )
                            walk(entry)
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(db)
    return shapes


def shapes_to_rules(shapes: dict[str, set[str]]) -> dict[str, list[str]]:
    return {
        field: sorted(keys)
        for field, keys in sorted(shapes.items())
        if keys and m._is_schema_field_name(field)
    }


def count_missing_slots(da: object, db: object) -> int:
    n = 0

    def walk(x: object, y: object) -> None:
        nonlocal n
        if isinstance(y, dict) and isinstance(x, dict):
            for k, vy in y.items():
                if k not in x:
                    n += 1
                    continue
                walk(x[k], vy)
        elif isinstance(y, list) and isinstance(x, list):
            for i in range(min(len(x), len(y))):
                walk(x[i], y[i])

    walk(da, db)
    return n


def main() -> None:
    out_by: dict[str, list[Path]] = defaultdict(list)
    zh_by: dict[str, list[Path]] = defaultdict(list)
    for p in OUT_ROOT.rglob("*.json"):
        out_by[p.name].append(p)
    for p in ZH_ROOT.rglob("*.json"):
        zh_by[p.name].append(p)

    pairs: list[tuple[str, Path, Path]] = []
    for name in sorted(set(out_by) & set(zh_by)):
        if len(out_by[name]) != 1 or len(zh_by[name]) != 1:
            continue
        if should_process(name, out_by[name][0]):
            pairs.append((name, out_by[name][0], zh_by[name][0]))

    print(f"pairs={len(pairs)}")
    base = m.load_postprocess_rules(RULES_PATH)
    by_field = base.get("by_field") or {}
    by_path = base.get("by_path") or {}

    all_ens: dict[str, dict[str, list[str]]] = {}
    for name, op, zp in pairs:
        da = json.loads(op.read_text(encoding="utf-8"))
        db = json.loads(zp.read_text(encoding="utf-8"))
        rules_map = shapes_to_rules(collect_shapes(db))
        if not rules_map:
            print(f"= {name}: empty")
            continue
        trial = {
            "by_field": by_field,
            "by_path": by_path,
            "ensure_null": {name: rules_map},
        }
        before = count_missing_slots(da, db)
        after = count_missing_slots(
            m.apply_named_postprocess_rules(da, trial, file_name=name), db
        )
        all_ens[name] = rules_map
        print(f"+ {name}: fields={len(rules_map)} miss {before}->{after}")

    raw = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    raw["ensure_null"] = {k: all_ens[k] for k in sorted(all_ens)}
    RULES_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {RULES_PATH} files={len(all_ens)}")


if __name__ == "__main__":
    main()
