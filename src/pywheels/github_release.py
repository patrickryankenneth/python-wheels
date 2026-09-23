"""Minimal GitHub Releases client: locate and download the assets pywheels
needs for one package from a release - the wheel, its attestation bundle,
and the source archive if one is attached.

Also supports finding those same three files in a local directory tree
(find_local_assets) - useful for testing against artifacts you already
have on disk (e.g. pulled via `gh run download`) without a live release to
fetch from.

Uses only the standard library (urllib) - two simple GETs don't justify a
`requests` dependency.

v1 scope: single-package repo (dbt-oss). The archive-name match below is
dbt-oss-specific; generalize it (e.g. read a manifest asset instead of
guessing a filename pattern) once pywheels serves more than one package.
"""

from __future__ import annotations

import fnmatch
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

API_ROOT = "https://api.github.com"


@dataclass
class ReleaseAssets:
    wheel: Path
    bundle: Path
    archive: Optional[Path]


def _get_release(repo: str, tag: str) -> dict:
    url = f"{API_ROOT}/repos/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise FileNotFoundError(f"release {tag!r} not found in {repo} (HTTP {exc.code})") from exc


def get_latest_release_tag(repo: str) -> str:
    """Used by `install` when the caller doesn't pin a --tag. `verify` never
    calls this - it always wants a specific, caller-named release."""
    url = f"{API_ROOT}/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise FileNotFoundError(f"no releases found in {repo} (HTTP {exc.code})") from exc
    tag = release.get("tag_name")
    if not tag:
        raise FileNotFoundError(f"latest release in {repo} has no tag_name")
    return tag


def _download(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        dest.write_bytes(resp.read())
    return dest


def fetch_release_assets(repo: str, tag: str, package: str, dest_dir: Path) -> ReleaseAssets:
    release = _get_release(repo, tag)
    assets = {a["name"]: a for a in release.get("assets", [])}

    wheel_pattern = f"{package.replace('-', '_')}-*.whl"
    wheel_name = next((n for n in assets if fnmatch.fnmatch(n, wheel_pattern)), None)
    if wheel_name is None:
        raise FileNotFoundError(f"no wheel matching {wheel_pattern!r} in {repo}@{tag}")

    bundle_name = f"{wheel_name}.attestations.jsonl"
    if bundle_name not in assets:
        raise FileNotFoundError(f"no attestation bundle {bundle_name!r} in {repo}@{tag}")

    archive_name = next(
        (n for n in assets if n.startswith("dbt-oss-source-") and n.endswith(".tar.gz")),
        None,
    )

    wheel_path = _download(assets[wheel_name]["browser_download_url"], dest_dir / wheel_name)
    bundle_path = _download(assets[bundle_name]["browser_download_url"], dest_dir / bundle_name)
    archive_path = (
        _download(assets[archive_name]["browser_download_url"], dest_dir / archive_name)
        if archive_name else None
    )

    return ReleaseAssets(wheel=wheel_path, bundle=bundle_path, archive=archive_path)


def find_local_assets(package: str, local_dir: Path) -> ReleaseAssets:
    """Same three files as fetch_release_assets, located by searching a
    local directory tree instead of a GitHub release - e.g. the layout
    `gh run download` produces (wheel nested under an artifact-name folder,
    bundle files flat, archive nested under its own artifact folder)."""
    if not local_dir.is_dir():
        raise FileNotFoundError(f"not a directory: {local_dir}")

    wheel_pattern = f"{package.replace('-', '_')}-*.whl"
    wheel_path = next(
        (p for p in local_dir.rglob("*.whl") if fnmatch.fnmatch(p.name, wheel_pattern)),
        None,
    )
    if wheel_path is None:
        raise FileNotFoundError(f"no wheel matching {wheel_pattern!r} under {local_dir}")

    bundle_name = f"{wheel_path.name}.attestations.jsonl"
    bundle_path = next((p for p in local_dir.rglob(bundle_name)), None)
    if bundle_path is None:
        raise FileNotFoundError(f"no attestation bundle {bundle_name!r} under {local_dir}")

    archive_path = next(
        (p for p in local_dir.rglob("*.tar.gz") if p.name.startswith("dbt-oss-source-")),
        None,
    )

    return ReleaseAssets(wheel=wheel_path, bundle=bundle_path, archive=archive_path)