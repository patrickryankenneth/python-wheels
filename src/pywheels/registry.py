"""Registry client: which attested wheels exist, and which one fits THIS machine.

Two static JSON files are served off python-wheels.github.io:

  registry-v2.json  (schema 2, preferred)  one entry per release tag, one
                    sub-entry per wheel file, every field derived from the
                    wheel's own verified attestations by the site's
                    update-registry workflow - nothing hand-typed.
  registry.json     (schema 1, legacy)     flat {tag: {workflow, provides}}.
                    Kept only so `verify --tag` still works if v2 is ever
                    unreachable; it carries no platform data, so nothing
                    can be auto-selected from it.

Both are fetched with a plain HTTPS GET off GitHub Pages, NOT the GitHub
REST API, so the CLI never shares that API's 60-requests/hour/IP limit.

The registry is a HINT, never the authority. It tells the CLI which
workflow should have signed a wheel, and which digest/policy to expect;
verify.py then checks the wheel's own attestations and fails if they
disagree. A registry that lies can therefore make a verification fail, but
cannot make an unattested wheel pass: the signer identity is always pinned
to CANONICAL_REPO.

Resolution is by wheel, not by tag. Tags are labels (`dbt-oss-v2.0.5`,
`dbt-oss-v2.0.5.post1`) and are never parsed for meaning; name, version and
platform come from the asset entry. A newer `.postN` release may carry only
some platforms, so selection runs per platform across ALL releases:

  1. keep wheels whose `name` and `version` match the request,
  2. keep those whose `wheel_tag` this interpreter supports and whose
     `requires_python` it satisfies,
  3. take the best-fitting platform tag (pip's own ranking),
  4. among wheels with that tag, take the highest `policy_version`
     (date-prefixed, so plain string compare), then the highest `revision`.

With no version given, "latest" means the highest version that has a
compatible wheel - not the highest version overall.

Set PYWHEELS_REGISTRY_URL to point at a different registry-v2.json (e.g. a
raw.githubusercontent.com URL) while testing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from packaging import tags as _tags
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

CANONICAL_REPO = "patrickryankenneth/python-wheels-builds"
REGISTRY_V2_URL = "https://python-wheels.github.io/registry-v2.json"
REGISTRY_V1_URL = "https://python-wheels.github.io/registry.json"

SUPPORTED_SCHEMAS = {1, 2}

_TIMEOUT = 15


class RegistryError(Exception):
    """No usable answer - registry unreachable/malformed, or nothing matches
    what was asked. Callers decide what to do next; this module only
    reports, it never falls back on its own."""


class NoCompatibleWheel(RegistryError):
    """The registry has the package, just nothing this machine can use.
    `available` lets the caller show what does exist."""

    def __init__(self, message: str, available: list):
        super().__init__(message)
        self.available = available


# ------------------------------------------------------------------ model

@dataclass(frozen=True)
class Asset:
    """One attested wheel, as described by registry-v2.json."""

    builds_repo: str
    tag: str
    revision: int
    filename: str
    name: str
    version: str
    wheel_tag: str
    sha256: str
    requires_python: str
    signer_workflow: str  # a path, e.g. ".github/workflows/build-dbt-oss-musllinux.yml"
    policy_version: str
    predicate_types: Tuple[str, ...]
    upstream_repo: str
    upstream_tag: str
    upstream_commit: str

    @property
    def canonical_name(self) -> str:
        return str(canonicalize_name(self.name))

    def download_url(self, filename: Optional[str] = None) -> str:
        return f"https://github.com/{self.builds_repo}/releases/download/{self.tag}/{filename or self.filename}"

    @property
    def bundle_name(self) -> str:
        return f"{self.filename}.attestations.jsonl"

    @property
    def archive_name(self) -> str:
        """Source archive attached to the release: `<upstream repo name>-source-<commit>.tar.gz`."""
        repo_name = self.upstream_repo.rstrip("/").rsplit("/", 1)[-1]
        return f"{repo_name}-source-{self.upstream_commit}.tar.gz"

    @property
    def upstream_label(self) -> str:
        repo = self.upstream_repo.replace("https://github.com/", "")
        return f"{repo} {self.upstream_tag} @ {self.upstream_commit[:12]}"


@dataclass
class Selection:
    chosen: Asset
    also_matched: list  # other compatible wheels for the same request (older policy/revision, other posts)


def _asset_from_entry(builds_repo: str, tag: str, revision: int, filename: str, e: dict) -> Asset:
    try:
        prov = e["provenance"]
        up = e["upstream"]
        # A version PEP 440 can't parse would otherwise sort as 0 and silently
        # never be selected (e.g. "2.0.5.post1.post1" is not a valid version).
        Version(e["version"])
        return Asset(
            builds_repo=builds_repo,
            tag=tag,
            revision=int(revision),
            filename=filename,
            name=e["name"],
            version=e["version"],
            wheel_tag=e["wheel_tag"],
            sha256=e["sha256"],
            requires_python=e.get("requires_python", "") or "",
            signer_workflow=e["signer_workflow"],
            policy_version=prov["policy_version"],
            predicate_types=tuple(prov.get("predicate_types", ())),
            upstream_repo=up["repo"],
            upstream_tag=up["tag"],
            upstream_commit=up["commit"],
        )
    except (KeyError, TypeError, ValueError, InvalidVersion) as exc:
        raise RegistryError(f"registry entry {tag}/{filename} is malformed (missing or invalid field: {exc})") from exc


class Registry:
    def __init__(self, schema: int, url: str, *, assets: Optional[list] = None, v1: Optional[dict] = None):
        self.schema = schema
        self.url = url
        self.assets = assets or []
        self.v1 = v1 or {}

    # -- schema 1 (legacy) ------------------------------------------------
    def workflow_for_tag(self, tag: str) -> str:
        """v1 only: workflow file that built `tag`. For v2 use the asset's signer_workflow."""
        entry = self.v1.get(tag)
        if entry is None:
            raise RegistryError(f"tag {tag!r} is not in the registry at {self.url}")
        workflow = entry.get("workflow")
        if not workflow:
            raise RegistryError(f"registry entry for {tag!r} has no 'workflow' field")
        return workflow

    # -- schema 2 ---------------------------------------------------------
    def for_package(self, name: str) -> list:
        want = canonicalize_name(name)
        return [a for a in self.assets if a.canonical_name == want]

    def package_names(self) -> list:
        seen = {}
        for a in self.assets:
            seen.setdefault(a.canonical_name, a.name)
        return sorted(seen.values())

    def select(
        self,
        name: str,
        specifier: Optional[SpecifierSet] = None,
        *,
        tag: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Selection:
        if self.schema < 2:
            raise RegistryError(
                f"the registry at {self.url} is schema 1: it lists release tags only, with no wheel or "
                "platform data, so a wheel can't be chosen automatically (use --tag with `verify`)"
            )
        pool = self.for_package(name)
        if not pool:
            raise RegistryError(f"no registered release provides {name!r}")
        if tag:
            pool = [a for a in pool if a.tag == tag]
            if not pool:
                raise RegistryError(f"release {tag!r} has no wheel named {name!r} in the registry")
        if filename:
            pool = [a for a in pool if a.filename == filename]
            if not pool:
                raise RegistryError(f"no wheel {filename!r} for {name!r} in the registry")

        if specifier is not None and str(specifier):
            in_version = [a for a in pool if _version_ok(a.version, specifier)]
            if not in_version:
                known = ", ".join(sorted({a.version for a in pool}, key=_vkey, reverse=True))
                raise RegistryError(f"no registered build of {name}{specifier} (registered versions: {known})")
            pool = in_version

        if filename:
            # Naming an exact wheel means "this one" - platform fit is not
            # part of the question (verify-only use on a machine that can't
            # run it). Callers that install must check is_compatible().
            best = max(pool, key=lambda a: (_vkey(a.version), a.policy_version, a.revision))
            return Selection(chosen=best, also_matched=[a for a in pool if a is not best])

        usable = [(a, r) for a in pool if (r := _platform_rank(a)) is not None]
        if not usable:
            raise NoCompatibleWheel(
                f"{name}: {len(pool)} attested wheel(s) exist, but none is compatible with this machine "
                f"(python {sys.version_info.major}.{sys.version_info.minor}, {_tags_summary()})",
                available=pool,
            )

        best_version = max((a.version for a, _ in usable), key=_vkey)
        usable = [(a, r) for a, r in usable if _vkey(a.version) == _vkey(best_version)]
        best_rank = min(r for _, r in usable)
        finalists = [a for a, r in usable if r == best_rank]
        finalists.sort(key=lambda a: (a.policy_version, a.revision), reverse=True)
        others = [a for a, _ in usable if a is not finalists[0]]
        return Selection(chosen=finalists[0], also_matched=others)


# ----------------------------------------------------------- compatibility

_ranks: Optional[dict] = None


def _supported_ranks() -> dict:
    global _ranks
    if _ranks is None:
        _ranks = {}
        for i, t in enumerate(_tags.sys_tags()):
            _ranks.setdefault(t, i)
    return _ranks


def _tags_summary() -> str:
    first = next(iter(_tags.sys_tags()), None)
    return f"platform {first.platform}" if first else "unknown platform"


def _platform_rank(asset: Asset) -> Optional[int]:
    """Best (lowest) rank of any tag in asset.wheel_tag among this
    interpreter's supported tags, or None if it can't run here or the
    wheel's requires_python excludes this interpreter."""
    if asset.requires_python:
        try:
            if not SpecifierSet(asset.requires_python).contains(".".join(map(str, sys.version_info[:3])), prereleases=True):
                return None
        except Exception:
            return None
    ranks = _supported_ranks()
    try:
        hits = [ranks[t] for t in _tags.parse_tag(asset.wheel_tag) if t in ranks]
    except Exception:
        return None
    return min(hits) if hits else None


def is_compatible(asset: Asset) -> bool:
    return _platform_rank(asset) is not None


def _vkey(v: str):
    try:
        return Version(v)
    except InvalidVersion:
        return Version("0")


def _version_ok(v: str, spec: SpecifierSet) -> bool:
    try:
        return spec.contains(Version(v), prereleases=True)
    except InvalidVersion:
        return False


# ------------------------------------------------------------------- fetch

_cache: Optional[Registry] = None


def registry_url() -> str:
    return os.environ.get("PYWHEELS_REGISTRY_URL") or REGISTRY_V2_URL


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry at {url} is not valid JSON: {exc}") from exc


def _parse_v2(url: str, data: dict) -> Registry:
    builds_repo = data.get("builds_repo")
    releases = data.get("releases")
    if builds_repo != CANONICAL_REPO:
        raise RegistryError(
            f"registry at {url} names builds repo {builds_repo!r}, but this pywheels only trusts "
            f"{CANONICAL_REPO!r}; refusing to use it"
        )
    if not isinstance(releases, dict):
        raise RegistryError(f"registry at {url} is malformed (no 'releases' object)")
    assets = []
    for tag, rel in releases.items():
        revision = rel.get("revision", 0)
        for filename, entry in (rel.get("assets") or {}).items():
            assets.append(_asset_from_entry(builds_repo, tag, revision, filename, entry))
    return Registry(2, url, assets=assets)


def load() -> Registry:
    """Fetch the registry (cached per process). v2 first; if v2 doesn't
    exist (HTTP 404) fall back to legacy v1. Any other failure is an error -
    silently downgrading on a network blip would hide a real problem."""
    global _cache
    if _cache is not None:
        return _cache

    url = registry_url()
    if os.environ.get("PYWHEELS_REGISTRY_URL"):
        print(f"warning: using registry override {url}", file=sys.stderr)
    try:
        data = _get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and url == REGISTRY_V2_URL:
            return _load_v1()
        raise RegistryError(f"could not fetch registry from {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RegistryError(f"could not fetch registry from {url}: {exc}") from exc

    if not isinstance(data, dict):
        raise RegistryError(f"registry at {url} is malformed (expected a JSON object)")
    schema = data.get("schema")
    if schema != 2:
        raise RegistryError(
            f"registry at {url} uses schema {schema!r}, which this pywheels ({_version()}) can't read - "
            "update pywheels: pip install --upgrade pywheels"
        )
    _cache = _parse_v2(url, data)
    return _cache


def _load_v1() -> Registry:
    global _cache
    try:
        data = _get_json(REGISTRY_V1_URL)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise RegistryError(f"could not fetch registry from {REGISTRY_V1_URL}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"registry at {REGISTRY_V1_URL} is malformed (expected a JSON object)")
    _cache = Registry(1, REGISTRY_V1_URL, v1=data)
    return _cache


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "unknown"


def group_by_version(assets: Iterable[Asset]) -> dict:
    """{canonical name: {version: [assets]}}, versions newest first - for `list`."""
    out: dict = {}
    for a in assets:
        out.setdefault(a.canonical_name, {}).setdefault(a.version, []).append(a)
    return {
        n: dict(sorted(vs.items(), key=lambda kv: _vkey(kv[0]), reverse=True))
        for n, vs in sorted(out.items())
    }