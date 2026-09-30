"""Minimal GitHub Releases client: locate and download the assets pywheels
needs for one package from a release - the wheel, its attestation bundle,
and the source archive if one is attached.

Also supports finding those same three files in a local directory tree
(find_local_assets) - useful for testing against artifacts you already
have on disk (e.g. pulled via `gh run download`) without a live release to
fetch from.

Uses only the standard library (urllib) - two simple GETs don't justify a
`requests` dependency.

Two ways to get a release's files:

  fetch_registry_asset  - registry schema 2. Every file's URL is derivable
                          from the registry entry
                          (https://github.com/<repo>/releases/download/<tag>/<file>),
                          so there's no GitHub REST API call at all (no 60/hr
                          limit) and no filename guessing. The wheel is hashed
                          while it streams and must match the registry's sha256.
  fetch_release_assets  - legacy, schema 1 / --tag only. Asks the REST API for
                          the release's asset list and guesses names. The
                          archive-name match is dbt-oss-specific.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sys
import time
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


class DownloadError(Exception):
    """A file couldn't be fetched, or didn't hash to what the registry promised."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _shorten(text: str, width: int) -> str:
    """Middle-ellipsize `text` to at most `width` chars (keeps the extension)."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    keep = width - 1
    head = keep // 2
    return text[:head] + "\u2026" + text[len(text) - (keep - head):]


def _download(url: str, dest: Path, *, expected_sha256: Optional[str] = None, label: Optional[str] = None) -> Path:
    """Stream `url` to `dest` (wheels are hundreds of MB to GB - never held in
    memory). With expected_sha256, a mismatch deletes the file and raises, and
    an existing file that already matches is reused instead of re-downloaded.
    Writes to `dest.part` first, so an interrupted download never looks complete."""
    if expected_sha256 and dest.exists():
        if _sha256_file(dest) == expected_sha256:
            print(f"  cached   {dest.name}", file=sys.stderr)
            return dest
        dest.unlink()

    label = label or dest.name
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, part.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            tty = sys.stderr.isatty() and not os.environ.get("PYWHEELS_NO_PROGRESS")
            bar_width = 24
            size = f"{total / 1e6:,.1f} MB" if total >= 1e6 else f"{total / 1e3:,.1f} KB"
            line_width = 0
            last_draw = 0.0
            min_interval = 0.2  # seconds - caps redraws to ~5/sec regardless of transfer speed
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if tty and total:
                    now = time.monotonic()
                    finished = done >= total
                    # A fast/cached transfer can complete a dozen+ chunks in
                    # well under a second, faster than some terminal clients
                    # (notably Termius and other JS-based renderers) can
                    # actually paint each "\r" redraw before the next one
                    # arrives - which shows up as a half-painted line getting
                    # reset over and over instead of a smooth bar. Throttling
                    # by wall-clock time (not just percent) caps how often we
                    # write regardless of transfer speed, so the renderer
                    # always has time to catch up; the final 100% write always
                    # happens even if it lands inside the throttle window.
                    if finished or (now - last_draw) >= min_interval:
                        last_draw = now
                        pct = done / total
                        filled = int(pct * bar_width)
                        bar = "#" * filled + "-" * (bar_width - filled)
                        # "\r" only returns to column 0 of the *current row*, so
                        # a line wider than the terminal wraps and every redraw
                        # leaves the wrapped-off head behind as scrollback spam.
                        # Re-read the width each draw (it can change mid-download)
                        # and shrink the label to fit, keeping one spare column
                        # so we never sit on the auto-wrap edge.
                        cols = shutil.get_terminal_size((80, 24)).columns - 1
                        tail = f": [{bar}] {int(pct * 100):3d}% of {size}"
                        room = cols - len("  fetching ") - len(tail)
                        if room < 8:  # very narrow: drop the bar, keep the percentage
                            tail = f": {int(pct * 100):3d}% of {size}"
                            room = cols - len("  fetching ") - len(tail)
                        shown = _shorten(label, max(room, 1))
                        line = f"  fetching {shown}{tail}"[:cols]
                        line_width = min(max(line_width, len(line)), cols)
                        print(f"\r{line.ljust(line_width)}", end="", file=sys.stderr)
            if tty and total:
                print(file=sys.stderr)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code == 404:
            raise FileNotFoundError(f"{label} not found at {url}") from exc
        raise DownloadError(f"{label}: HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{label}: download failed ({exc})") from exc

    if expected_sha256 and h.hexdigest() != expected_sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(
            f"{label}: sha256 {h.hexdigest()} does not match the registry's {expected_sha256} - discarded"
        )
    os.replace(part, dest)
    return dest


def fetch_registry_asset(asset, dest_dir: Path, *, want_archive: bool = True) -> ReleaseAssets:
    """Download one registry-v2 wheel, its attestation bundle and (optionally)
    the release's source archive. Cached under dest_dir/<tag>/ - the same wheel
    filename can legitimately exist in two releases with different bytes, so
    the tag is part of the cache key."""
    d = dest_dir / asset.tag
    d.mkdir(parents=True, exist_ok=True)
    wheel = _download(asset.download_url(), d / asset.filename, expected_sha256=asset.sha256)
    bundle = _download(asset.download_url(asset.bundle_name), d / asset.bundle_name)
    archive = None
    if want_archive:
        archive = _download(asset.download_url(asset.archive_name), d / asset.archive_name)
    return ReleaseAssets(wheel=wheel, bundle=bundle, archive=archive)


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