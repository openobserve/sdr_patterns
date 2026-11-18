#!/usr/bin/env python3
"""Build starter_packs.json from starter_packs/catalog.json and one starter_packs/<id>.json per pack.

OpenObserve downloads the generated file and rejects it whole when any pack is malformed, so every
rule it applies is checked here first. --check fails when the committed file is stale; --remote also
checks that every alert id is in the Alert Library manifest and every gallery dashboard exists on S3.
"""

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "starter_packs"
HEADER_FILE = SOURCE_DIR / "catalog.json"
OUTPUT_FILE = ROOT / "starter_packs.json"
BUCKET = "https://openobserve-datasources-bucket.s3.amazonaws.com"
MANIFEST_URL = f"{BUCKET}/alerts/manifest.json"
ID_RE = re.compile(r"^[a-z0-9-]+$")
SUPPORTED_MAJOR = 1
REQUIRED_STRINGS = ("id", "version", "title", "description", "paused_impact")
REQUIRED_LISTS = ("dashboards", "saved_views")
TIMEOUT_SECS = 30


def load_json(path, errors):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        errors.append(f"{path.name}: cannot read JSON: {err}")
        return None


def check_header(header, errors):
    version = header.get("format_version") if isinstance(header, dict) else None
    if not isinstance(version, str) or version.split(".")[0] != str(SUPPORTED_MAJOR):
        errors.append(f"catalog.json: format_version must be {SUPPORTED_MAJOR}.x.y, got {version!r}")
    ids = header.get("packs") if isinstance(header, dict) else None
    if not isinstance(ids, list) or not ids:
        errors.append("catalog.json: packs must be a non-empty list of pack ids")
        return []
    seen = set()
    for pack_id in ids:
        if not isinstance(pack_id, str) or not ID_RE.match(pack_id) or pack_id == "catalog":
            errors.append(f"catalog.json: invalid pack id {pack_id!r} (allowed: [a-z0-9-]+, not 'catalog')")
        elif pack_id in seen:
            errors.append(f"catalog.json: pack id {pack_id!r} is listed twice")
        else:
            seen.add(pack_id)
    return [pack_id for pack_id in ids if pack_id in seen]


def check_pack(file_id, pack, errors):
    where = f"{file_id}.json"
    if not isinstance(pack, dict):
        errors.append(f"{where}: must hold one JSON object")
        return
    for field in REQUIRED_STRINGS:
        if not isinstance(pack.get(field), str) or not pack[field].strip():
            errors.append(f"{where}: {field} must be a non-empty string")
    if pack.get("id") != file_id:
        errors.append(f"{where}: id {pack.get('id')!r} must equal the file name {file_id!r}")
    for field in REQUIRED_LISTS:
        if not isinstance(pack.get(field), list):
            errors.append(f"{where}: {field} must be a list")
    folder = pack.get("folder")
    if not isinstance(folder, dict) or not all(isinstance(folder.get(k), str) for k in ("name", "description")):
        errors.append(f"{where}: folder must have string name and description")
    if not isinstance(pack.get("detect"), dict):
        errors.append(f"{where}: detect must be an object")
    alerts = pack.get("alerts")
    if not isinstance(alerts, dict) or not all(
        isinstance(alerts.get(k), list) and all(isinstance(a, str) for a in alerts[k]) for k in ("core", "more")
    ):
        errors.append(f"{where}: alerts must have core and more lists of alert ids")
    for index, dashboard in enumerate(pack.get("dashboards") or []):
        if not isinstance(dashboard, dict) or not isinstance(dashboard.get("id"), str):
            errors.append(f"{where}: dashboards[{index}] needs a string id")
        elif not dashboard.get("gallery_path") and not dashboard.get("bundled"):
            errors.append(f"{where}: dashboard {dashboard['id']!r} needs gallery_path or bundled")


def check_no_orphans(ids, errors):
    listed = set(ids)
    for path in sorted(SOURCE_DIR.glob("*.json")):
        if path != HEADER_FILE and path.stem not in listed:
            errors.append(f"{path.name}: not listed in catalog.json packs")


def build(errors):
    header = load_json(HEADER_FILE, errors)
    if header is None:
        return None
    ids = check_header(header, errors)
    check_no_orphans(ids, errors)
    packs = []
    for pack_id in ids:
        path = SOURCE_DIR / f"{pack_id}.json"
        if not path.exists():
            errors.append(f"{path.name}: listed in catalog.json but missing")
            continue
        pack = load_json(path, errors)
        if pack is not None:
            check_pack(pack_id, pack, errors)
            packs.append(pack)
    if errors:
        return None
    body = {"format_version": header["format_version"], "packs": packs}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"format_version": header["format_version"], "catalog_version": digest[:12], "packs": packs}


def render(catalog):
    return json.dumps(catalog, indent=2, ensure_ascii=False) + "\n"


def check_committed(expected, errors):
    try:
        committed = OUTPUT_FILE.read_text(encoding="utf-8")
    except OSError:
        errors.append("starter_packs.json is missing; run scripts/build_starter_packs.py")
        return
    try:
        version = json.loads(committed).get("catalog_version")
    except ValueError:
        version = None
    if not isinstance(version, str) or not version or version.startswith("seed-"):
        errors.append(f"starter_packs.json: catalog_version {version!r} is empty or starts with 'seed-'")
    if committed != expected:
        errors.append("starter_packs.json is stale; run scripts/build_starter_packs.py and commit the result")


def fetch(url, method="GET"):
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "sdr-patterns-ci"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECS) as response:
        return response.status, response.read() if method == "GET" else b""


def check_remote(catalog, errors):
    try:
        _, raw = fetch(MANIFEST_URL)
        manifest = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError) as err:
        errors.append(f"cannot load {MANIFEST_URL}: {err}")
        return
    known = {a.get("id") for a in manifest.get("alerts", []) + manifest.get("anomaly_alerts", [])}
    for pack in catalog["packs"]:
        for alert_id in pack["alerts"]["core"] + pack["alerts"]["more"]:
            if alert_id not in known:
                errors.append(f"{pack['id']}: alert {alert_id!r} is not in the Alert Library manifest")
        for dashboard in pack["dashboards"]:
            path = dashboard.get("gallery_path")
            if not path:
                continue
            url = f"{BUCKET}/dashboards/{urllib.parse.quote(path, safe='/')}"
            try:
                status, _ = fetch(url, method="HEAD")
            except (urllib.error.URLError, OSError) as err:
                status = getattr(err, "code", err)
            if status != 200:
                errors.append(f"{pack['id']}: dashboard {path!r} answered {status} at {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when starter_packs.json is stale")
    parser.add_argument("--remote", action="store_true", help="also check alert ids and dashboards on S3")
    args = parser.parse_args()

    errors = []
    catalog = build(errors)
    if catalog is not None:
        if args.remote:
            check_remote(catalog, errors)
        if args.check:
            check_committed(render(catalog), errors)
        elif not errors:
            OUTPUT_FILE.write_text(render(catalog), encoding="utf-8")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    count = len(catalog["packs"])
    print(f"starter_packs.json: {count} packs, catalog_version {catalog['catalog_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
