#!/usr/bin/env python3
"""GitHub Actions helpers: probe 底包/热更/FBS/StudioCLI, fetch tools, write config, sync output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
API_UA = "Ark-FbsDecoder-CI"
ANDROID_APK_ENTRY = "https://ak.hypergryph.com/downloads/android_lastest"
CDN_HV = "https://ak-conf.hypergryph.com/config/prod/official/{platform}/version"
CDN_HU = "https://ak.hycdn.cn/assetbundle/official"
FBS_REPO = "MooncellWiki/OpenArknightsFBS"
FBS_BRANCH = "main"
STUDIO_REPO = "aelurum/AssetStudio"
FLATC_VERSION = "v25.2.10"
GAMEDATA_REPO = "Black-Star-Red/ArknightsGameData"
GAMEDATA_BRANCH = "master"
PRESERVE_IN_DEST = {
    ".git",
    ".gitattributes",
    ".gitignore",
    "README.md",
    "LICENSE",
    "LICENSE.md",
}
DATA_DIR_HINTS = {
    "excel",
    "battle",
    "art",
    "building",
    "levels",
    "story",
    "levelscripts",
    "[uc]lua",
}


def _bool_arg(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _gh_out(key: str, value: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    text = "" if value is None else str(value)
    if "\n" in text:
        raise ValueError(f"GITHUB_OUTPUT {key} contains newline")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={text}\n")


def _gh_summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(md.rstrip() + "\n")


def _http_open(url: str, *, headers: dict[str, str] | None = None, timeout: float = 60):
    hdrs = {"User-Agent": UA, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    return urllib.request.urlopen(req, timeout=timeout)


def _http_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: float = 120) -> bytes:
    with _http_open(url, headers=headers, timeout=timeout) as resp:
        return resp.read()


def _http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 120) -> Any:
    raw = _http_bytes(url, headers=headers, timeout=timeout)
    return json.loads(raw.decode("utf-8"))


def _github_headers() -> dict[str, str]:
    headers = {
        "User-Agent": API_UA,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _sha256_text(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def _header_map(resp) -> dict[str, str]:
    return {
        "final": resp.geturl(),
        "cd": resp.headers.get("Content-Disposition") or "",
        "length": resp.headers.get("Content-Length") or "",
        "etag": resp.headers.get("ETag") or resp.headers.get("Etag") or "",
        "last_modified": resp.headers.get("Last-Modified") or "",
        "content_range": resp.headers.get("Content-Range") or "",
    }


def resolve_apk_identity(entry: str = ANDROID_APK_ENTRY) -> dict[str, Any]:
    """Follow official redirect; do not download the APK body."""
    meta = None
    last_err: Exception | None = None
    for method, extra in (
        ("HEAD", {}),
        ("GET", {"Range": "bytes=0-0"}),
    ):
        try:
            req = urllib.request.Request(
                entry,
                method=method,
                headers={"User-Agent": UA, **extra},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                meta = _header_map(resp)
            break
        except Exception as e:
            last_err = e
            print(f"apk {method} {entry}: {e}", flush=True)
    if meta is None:
        raise RuntimeError(f"无法解析官网 APK 入口: {entry}") from last_err

    final = meta["final"]
    name = "arknights.apk"
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', meta["cd"], re.I)
    if m:
        name = m.group(1).strip()
    else:
        name = Path(urlparse(final).path).name or name
    if not name.lower().endswith(".apk"):
        name += ".apk"

    length = meta["length"] or meta["content_range"]
    ident_hash = _sha256_text(name, length, meta["etag"], final)
    return {
        "entry": entry,
        "final_url": final,
        "filename": name,
        "content_length": length,
        "etag": meta["etag"],
        "last_modified": meta["last_modified"],
        "hash": ident_hash,
    }


def probe_hot(platform: str = "Android") -> dict[str, Any]:
    ver_url = CDN_HV.format(platform=platform)
    version = _http_json(ver_url)
    if not isinstance(version, dict) or not version.get("resVersion"):
        raise RuntimeError(f"version API 无 resVersion: {ver_url} -> {version!r}")
    res_version = str(version["resVersion"])
    hu = CDN_HU.rstrip("/")
    list_url = f"{hu}/{platform}/assets/{res_version}/hot_update_list.json"
    raw = _http_bytes(list_url, timeout=300)
    hot_hash = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
        version_id = str(payload.get("versionId") or res_version)
        ab_count = len(payload.get("abInfos") or [])
    except Exception:
        version_id = res_version
        ab_count = -1
    return {
        "platform": platform,
        "resVersion": res_version,
        "clientVersion": version.get("clientVersion"),
        "versionId": version_id,
        "hot_update_list_url": list_url,
        "ab_count": ab_count,
        "hash": _sha256_text(platform, res_version, version_id, hot_hash),
        "list_sha256": hot_hash,
    }


def probe_fbs() -> dict[str, Any]:
    url = f"https://api.github.com/repos/{FBS_REPO}/commits/{FBS_BRANCH}"
    data = _http_json(url, headers=_github_headers())
    sha = str(data.get("sha") or "")
    if not sha:
        raise RuntimeError(f"OpenArknightsFBS 无 sha: {url}")
    msg = ""
    try:
        msg = str((data.get("commit") or {}).get("message") or "").splitlines()[0]
    except Exception:
        msg = ""
    return {"repo": FBS_REPO, "branch": FBS_BRANCH, "sha": sha, "message": msg}


def _pick_studio_asset(assets: list[dict[str, Any]]) -> dict[str, str] | None:
    names = [(str(a.get("name") or ""), str(a.get("browser_download_url") or "")) for a in assets]
    names = [(n, u) for n, u in names if n and u]
    preferred = (
        "ArknightsStudioCLI_net472_win32_64.zip",
        "ArknightsStudioCLI_net8_win64.zip",
        "ArknightsStudioCLI_net6_win64.zip",
    )
    by_name = {n: u for n, u in names}
    for want in preferred:
        if want in by_name:
            return {"name": want, "url": by_name[want]}
    for n, u in names:
        ln = n.lower()
        if "arknightsstudiocli" in ln and ln.endswith(".zip") and "win" in ln:
            return {"name": n, "url": u}
    for n, u in names:
        ln = n.lower()
        if "arknightsstudiocli" in ln and "portable" in ln and ln.endswith(".zip"):
            return {"name": n, "url": u}
    return None


def probe_studio() -> dict[str, Any]:
    url = f"https://api.github.com/repos/{STUDIO_REPO}/releases?per_page=20"
    releases = _http_json(url, headers=_github_headers())
    if not isinstance(releases, list):
        raise RuntimeError(f"AssetStudio releases 非列表: {type(releases)}")
    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith("ak-v"):
            continue
        picked = _pick_studio_asset(rel.get("assets") or [])
        if not picked:
            continue
        return {
            "repo": STUDIO_REPO,
            "tag": tag,
            "name": rel.get("name"),
            "asset": picked["name"],
            "asset_url": picked["url"],
        }
    raise RuntimeError("未找到带 ArknightsStudioCLI 资源的 ak-v* release")


def load_previous_fingerprint(gamedata_repo: str, branch: str) -> dict[str, Any] | None:
    url = f"https://raw.githubusercontent.com/{gamedata_repo}/{branch}/_ci_source.json"
    try:
        data = _http_json(url, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"warn: previous fingerprint HTTP {e.code}: {url}", flush=True)
        return None
    except Exception as e:
        print(f"warn: previous fingerprint: {e}", flush=True)
        return None
    return data if isinstance(data, dict) else None


def _changed(cur: dict[str, Any] | None, prev: dict[str, Any] | None, *keys: str) -> bool:
    if not prev:
        return True
    a: Any = cur or {}
    b: Any = prev
    for k in keys:
        a = (a or {}).get(k) if isinstance(a, dict) else None
        b = (b or {}).get(k) if isinstance(b, dict) else None
    return a != b


def cmd_probe(args: argparse.Namespace) -> int:
    force = str(os.environ.get("FORCE_RUN") or args.force).lower() in ("1", "true", "yes")
    print("probing APK / CDN / OpenArknightsFBS / ArknightsStudioCLI ...", flush=True)
    apk = resolve_apk_identity()
    print(f"apk: {apk['filename']} hash={apk['hash'][:12]} url={apk['final_url']}", flush=True)
    hot = probe_hot(args.platform)
    print(
        f"hot: platform={hot['platform']} resVersion={hot['resVersion']} "
        f"client={hot.get('clientVersion')} hash={hot['hash'][:12]}",
        flush=True,
    )
    fbs = probe_fbs()
    print(f"fbs: {fbs['sha'][:12]} {fbs.get('message')}", flush=True)
    studio = probe_studio()
    print(f"studio: {studio['tag']} {studio['asset']}", flush=True)

    prev = load_previous_fingerprint(args.gamedata_repo, args.gamedata_branch)
    apk_changed = _changed({"apk": apk}, prev, "apk", "hash")
    hot_changed = _changed({"hot": hot}, prev, "hot", "hash")
    fbs_changed = _changed({"fbs": fbs}, prev, "fbs", "sha")
    studio_changed = _changed({"studio": studio}, prev, "studio", "tag") or _changed(
        {"studio": studio}, prev, "studio", "asset"
    )
    should_run = force or apk_changed or hot_changed

    fingerprint = {
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "force": force,
        "should_run": should_run,
        "apk_changed": apk_changed,
        "hot_changed": hot_changed,
        "fbs_changed": fbs_changed,
        "studio_changed": studio_changed,
        "apk": apk,
        "hot": hot,
        "fbs": fbs,
        "studio": studio,
        "flatc": {"version": args.flatc_version},
    }
    out_file = Path(args.out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"fingerprint -> {out_file}", flush=True)
    print(
        f"should_run={should_run} apk_changed={apk_changed} hot_changed={hot_changed} "
        f"fbs_changed={fbs_changed} studio_changed={studio_changed} force={force}",
        flush=True,
    )

    _gh_out("should_run", "true" if should_run else "false")
    _gh_out("apk_changed", "true" if apk_changed else "false")
    _gh_out("hot_changed", "true" if hot_changed else "false")
    _gh_out("fbs_changed", "true" if fbs_changed else "false")
    _gh_out("studio_changed", "true" if studio_changed else "false")
    _gh_out("apk_hash", apk["hash"])
    _gh_out("apk_filename", apk["filename"])
    _gh_out("res_version", hot["resVersion"])
    _gh_out("hot_hash", hot["hash"])
    _gh_out("client_version", hot.get("clientVersion") or "")
    _gh_out("fbs_sha", fbs["sha"])
    _gh_out("studio_tag", studio["tag"])
    _gh_out("studio_asset", studio["asset"])
    _gh_out("studio_asset_url", studio["asset_url"])
    _gh_out("flatc_version", args.flatc_version)

    _gh_summary(
        "\n".join(
            [
                "## GameData probe",
                "",
                f"- should_run: `{should_run}` (force={force})",
                f"- APK: `{apk['filename']}` changed=`{apk_changed}` hash=`{apk['hash'][:12]}`",
                f"- hot: resVersion=`{hot['resVersion']}` client=`{hot.get('clientVersion')}` changed=`{hot_changed}`",
                f"- OpenArknightsFBS: `{fbs['sha'][:12]}` changed=`{fbs_changed}`",
                f"- ArknightsStudioCLI: `{studio['tag']}` / `{studio['asset']}` changed=`{studio_changed}`",
                "",
            ]
        )
    )
    return 0


def _download(url: str, dest: Path, timeout: float = 300) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"download {url} -> {dest}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": API_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def _git_clone_fbs(dest: Path, sha: str) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{FBS_REPO}.git"
    print(f"git clone --depth 1 {url} (want {sha[:12]})", flush=True)
    subprocess.run(["git", "clone", "--depth", "1", "--branch", FBS_BRANCH, url, str(dest)], check=True)
    got = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
    print(f"OpenArknightsFBS HEAD={got}", flush=True)
    if sha and got != sha:
        print(f"warn: FBS HEAD {got[:12]} != probed {sha[:12]} (race or shallow clone)", flush=True)


def _extract_studio(zip_path: Path, studio_dir: Path) -> Path:
    if studio_dir.exists():
        shutil.rmtree(studio_dir)
    studio_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(studio_dir)
    found = list(studio_dir.rglob("ArknightsStudioCLI.exe"))
    if not found:
        found = list(studio_dir.rglob("ArknightsStudioCLI"))
    if not found:
        raise RuntimeError(f"zip 内未找到 ArknightsStudioCLI: {zip_path}")
    exe = found[0]
    target = studio_dir / exe.name
    if exe.resolve() != target.resolve():
        shutil.copy2(exe, target)
        for dll in exe.parent.glob("*.dll"):
            shutil.copy2(dll, studio_dir / dll.name)
        extra = exe.parent / "ArknightsStudioCLI.runtimeconfig.json"
        if extra.is_file():
            shutil.copy2(extra, studio_dir / extra.name)
    return target


def cmd_fetch_tools(args: argparse.Namespace) -> int:
    vendor = Path(args.vendor_dir)
    vendor.mkdir(parents=True, exist_ok=True)
    fbs_dir = vendor / "OpenArknightsFBS" / "FBS"
    studio_dir = vendor / "studio"
    studio_exe = studio_dir / "ArknightsStudioCLI.exe"
    flatc = vendor / ("flatc.exe" if os.name == "nt" else "flatc")

    want_fbs = args.fbs_changed or not fbs_dir.is_dir() or not any(fbs_dir.glob("*.fbs"))
    if want_fbs:
        _git_clone_fbs(vendor / "OpenArknightsFBS", args.fbs_sha)
    else:
        print(f"skip OpenArknightsFBS (unchanged, cache hit) {fbs_dir}", flush=True)

    want_studio = args.studio_changed or not studio_exe.is_file()
    if want_studio:
        zpath = vendor / (args.studio_asset or "ArknightsStudioCLI.zip")
        _download(args.studio_asset_url, zpath)
        exe = _extract_studio(zpath, studio_dir)
        print(f"studio_cli={exe}", flush=True)
        try:
            zpath.unlink()
        except OSError:
            pass
    else:
        print(f"skip ArknightsStudioCLI (unchanged, cache hit) {studio_exe}", flush=True)

    if not flatc.is_file():
        zip_name = "Windows.flatc.binary.zip" if os.name == "nt" else "Linux.flatc.binary.g++-13.zip"
        url = f"https://github.com/google/flatbuffers/releases/download/{args.flatc_version}/{zip_name}"
        zpath = vendor / "flatc.zip"
        _download(url, zpath)
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(vendor)
        if not flatc.is_file():
            matches = list(vendor.rglob("flatc.exe" if os.name == "nt" else "flatc"))
            if not matches:
                raise RuntimeError(f"flatc not found after extract: {vendor}")
            shutil.copy2(matches[0], flatc)
        try:
            zpath.unlink()
        except OSError:
            pass
        if os.name != "nt":
            flatc.chmod(flatc.stat().st_mode | 0o111)
        print(f"flatc={flatc}", flush=True)
    else:
        print(f"skip flatc (cache hit) {flatc}", flush=True)

    if not fbs_dir.is_dir():
        raise FileNotFoundError(f"fbs_dir missing: {fbs_dir}")
    if not studio_exe.is_file():
        raise FileNotFoundError(f"studio_cli missing: {studio_exe}")
    if not flatc.is_file():
        raise FileNotFoundError(f"flatc missing: {flatc}")
    return 0


def cmd_write_config(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    vendor = Path(args.vendor_dir)
    if not vendor.is_absolute():
        vendor = (root / vendor).resolve()
    gamedata = Path(args.gamedata_dir)
    if not gamedata.is_absolute():
        gamedata = (root / gamedata).resolve()
    cfg = {
        "input_dir": "data",
        "out_dir": "output",
        "fbs_dir": str(vendor / "OpenArknightsFBS" / "FBS"),
        "flatc": str(vendor / ("flatc.exe" if os.name == "nt" else "flatc")),
        "template_dir": str(gamedata) if gamedata.is_dir() else None,
        "defaults_json": True,
        "flatten_kv": True,
        "postprocess_rules": str(root / "k_postprocess_rules.json"),
        "use_template_pad": bool(gamedata.is_dir()),
        "skip_existing": False,
        "chat_mask": "",
        "cdn_fetch_anon": True,
        "cdn_platform": args.platform,
        "cdn_hu": CDN_HU,
        "cdn_hv": CDN_HV,
        "cdn_anon_dir": "cdn_anon",
        "cdn_skip_existing": True,
        "cdn_workers": args.cdn_workers,
        "cdn_keep_markers": ["gamedata"],
        "cleanup_intermediates": True,
        "apk_fetch_official": True,
        "apk_download_dir": "apk_download",
        "apk_android_url": ANDROID_APK_ENTRY,
        "apk_skip_existing": True,
        "apk_fetch_timeout_ms": 1_200_000,
        "run_studio_first": True,
        "studio_cli": str(vendor / "studio" / "ArknightsStudioCLI.exe"),
        "studio_input_apk": None,
        "studio_input_hot": None,
        "studio_hot_tmp": "studio_hot_tmp",
        "studio_args": [
            "-t",
            "textAsset",
            "-g",
            "container",
            "--filter-by-container",
            "gamedata",
        ],
    }
    dest = Path(args.out)
    dest.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"config -> {dest}", flush=True)
    return 0


def _looks_like_gamedata(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any((path / name).is_dir() for name in ("excel", "battle", "story", "levels"))


def find_gamedata_roots(output: Path) -> list[Path]:
    roots: list[Path] = []
    for rel in ("gamedata", "dyn/gamedata"):
        p = output.joinpath(*rel.split("/"))
        if _looks_like_gamedata(p):
            roots.append(p)
    if roots:
        return roots
    if _looks_like_gamedata(output):
        return [output]
    raise FileNotFoundError(
        f"未在 {output} 下找到 gamedata（期望 gamedata/excel 或 dyn/gamedata/excel）"
    )


def _merge_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in {".git", "decode_issues.txt"}:
            continue
        target = dest / child.name
        if child.is_dir():
            _merge_tree(child, target)
        else:
            shutil.copy2(child, target)


def cmd_sync(args: argparse.Namespace) -> int:
    output = Path(args.output)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    roots = find_gamedata_roots(output)
    print("gamedata roots:", ", ".join(str(p) for p in roots), flush=True)

    with tempfile.TemporaryDirectory(prefix="gamedata_stage_") as td:
        staging = Path(td) / "stage"
        for root in roots:
            _merge_tree(root, staging)
        staged_names = {p.name for p in staging.iterdir()}
        for child in list(dest.iterdir()):
            if child.name in PRESERVE_IN_DEST or child.name == "_ci_source.json":
                continue
            stale = child.name not in staged_names and (
                child.name in DATA_DIR_HINTS or child.suffix.lower() == ".json"
            )
            if stale:
                print(f"remove stale {child}", flush=True)
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        for child in staging.iterdir():
            target = dest / child.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
            print(f"sync {child.name}", flush=True)

    fp_src = Path(args.fingerprint)
    if fp_src.is_file():
        payload = json.loads(fp_src.read_text(encoding="utf-8"))
        payload = {
            "probed_at": payload.get("probed_at"),
            "apk": payload.get("apk"),
            "hot": payload.get("hot"),
            "fbs": payload.get("fbs"),
            "studio": payload.get("studio"),
            "flatc": payload.get("flatc"),
        }
        (dest / "_ci_source.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("wrote _ci_source.json", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CI helpers for Arknights gamedata updates")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="Check APK / hot update / FBS / StudioCLI")
    sp.add_argument("--out-file", default="fingerprint.json")
    sp.add_argument("--platform", default="Android")
    sp.add_argument("--gamedata-repo", default=GAMEDATA_REPO)
    sp.add_argument("--gamedata-branch", default=GAMEDATA_BRANCH)
    sp.add_argument("--flatc-version", default=FLATC_VERSION)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sf = sub.add_parser("fetch-tools", help="Clone/download FBS, StudioCLI, flatc")
    sf.add_argument("--vendor-dir", default="vendor")
    sf.add_argument("--fbs-sha", default="")
    sf.add_argument("--fbs-changed", type=_bool_arg, default=False)
    sf.add_argument("--studio-tag", default="")
    sf.add_argument("--studio-asset", default="")
    sf.add_argument("--studio-asset-url", required=True)
    sf.add_argument("--studio-changed", type=_bool_arg, default=False)
    sf.add_argument("--flatc-version", default=FLATC_VERSION)
    sf.set_defaults(func=cmd_fetch_tools)

    sc = sub.add_parser("write-config", help="Write config.local.json for CI")
    sc.add_argument("--root", default=".")
    sc.add_argument("--vendor-dir", default="vendor")
    sc.add_argument("--gamedata-dir", default="gamedata-repo")
    sc.add_argument("--out", default="config.local.json")
    sc.add_argument("--platform", default="Android")
    sc.add_argument("--cdn-workers", type=int, default=8)
    sc.set_defaults(func=cmd_write_config)

    ss = sub.add_parser("sync", help="Copy decoded JSON into ArknightsGameData working tree")
    ss.add_argument("--output", default="output")
    ss.add_argument("--dest", default="gamedata-repo")
    ss.add_argument("--fingerprint", default="fingerprint.json")
    ss.set_defaults(func=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        raise
