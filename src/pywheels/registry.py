"""Package/tag -> attesting-workflow lookup, served as a static JSON file
off the python-wheels.github.io site (registry.json at the repo root, so
https://python-wheels.github.io/registry.json).

Why this exists, and why it's shaped this way:

  - Lives on the .io site, not in python-wheels-builds. That site is
    already pywheels' discovery/index layer (see simple/dbt-oss/index.html
    etc) - a registry fits its existing job. The builds repo's job is
    building and attesting, not indexing.
  - Fetched with a plain urllib GET, same as github_release.py already
    does for release assets - NOT the GitHub REST API. A CLI lots of
    people run, often from behind shared/corporate NAT, would otherwise
    all share the API's 60-unauthenticated-requests/hour-per-IP limit for
    something that doesn't need it. A file served off GitHub Pages' CDN
    has no such limit.
  - Keyed by release TAG, not bare package name. A tag names one already-
    immutable release - the builds workflow refuses to publish over an
    existing release with a different asset set - so a future workflow
    rename or hardening pass just adds a new tagged row; no published
    tag's row ever needs editing, because it's a fact about a specific
    past release, not an ever-current "the workflow for this package is
    currently X."
  - The .io repo carries the same signed-commit requirement as the builds
    repo for this reason: once registry.json feeds verification, that repo
    is part of the trust chain, not just a docs site.

Deliberately NOT keyed by workflow run ID or timestamp: the builds
workflow's own overwrite-refusal already makes "the same tag, retroactively
pointed at a different build" impossible through normal use (that would
need a new tag, which just gets its own row). Adding run-ID/timestamp
granularity on top would only cost us something real - a legitimate re-run
of the exact same workflow file (e.g. retrying a tag after a transient
runner failure, before it's ever published) would look like a different
identity even though nothing untrustworthy happened.

Schema (registry.json):

    {
      "<release-tag>": {
        "workflow": "<workflow-filename>.yml",
        "provides": ["<pip-package-name>", ...]
      },
      ...
    }

Rows are expected append-only, newest last - see _tags_providing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional, Tuple

CANONICAL_REPO = "patrickryankenneth/python-wheels-builds"
REGISTRY_URL = "https://python-wheels.github.io/registry.json"

_TIMEOUT = 15


class RegistryError(Exception):
    """No usable answer - registry unreachable/malformed, or no entry
    matches what was asked. Callers decide what to do next (cli.py falls
    back to an unverified source build); this module only reports, it
    never falls back on its own."""


_cache: Optional[dict] = None


def _fetch_registry() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    req = urllib.request.Request(REGISTRY_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise RegistryError(f"could not fetch registry from {REGISTRY_URL}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry at {REGISTRY_URL} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"registry at {REGISTRY_URL} is malformed (expected a JSON object)")
    _cache = data
    return _cache


def workflow_for_tag(tag: str) -> str:
    """The workflow file that built and attested `tag`. Raises
    RegistryError if the tag isn't registered - a tag not (yet) in the
    registry has nothing to look up, on purpose: verifying against a
    guessed workflow would defeat the point."""
    registry = _fetch_registry()
    entry = registry.get(tag)
    if entry is None:
        raise RegistryError(f"tag {tag!r} is not in the registry at {REGISTRY_URL}")
    workflow = entry.get("workflow")
    if not workflow:
        raise RegistryError(f"registry entry for {tag!r} has no 'workflow' field")
    return workflow


def _tags_providing(package: str, registry: dict):
    """(tag, entry) pairs whose 'provides' list includes `package`, in
    registry.json's own key order. json preserves object insertion order,
    and registry.json's rows are expected newest-last (append new tags,
    never edit a published one) - so the LAST match is the most recently
    registered tag for this package. This ordering is something the
    registry file's own (signed) commits control, not something inferred
    from the tag or version string, since tags aren't guaranteed to sort
    any particular way."""
    return [(tag, entry) for tag, entry in registry.items() if package in entry.get("provides", [])]


def resolve(package: str, version: Optional[str] = None) -> Tuple[str, str]:
    """Resolve a package (optionally pinned to an exact version) to
    (tag, workflow_file).

    No version -> the most recently registered tag that provides this
    package ("latest attested build").

    A version -> among the tags that provide this package, the one ending
    in `-v{version}` or `-{version}`. Deliberately NOT `{package}-v{version}`:
    a tag names the thing that was actually built, which is not always the
    package being asked for - e.g. dbt-core has no build of its own, it
    rides along in a dbt-oss-v2.0.5 tag (see registry.json's "provides").
    Requiring the package name as the tag's own prefix would silently miss
    every such alias. A version suffix is still specific enough to avoid
    false matches (a version string that also happens to be a substring of
    an unrelated tag won't match unless it's the trailing `-v{version}`/
    `-{version}` component).

    More than one match (e.g. two differently-prefixed tags both cut for
    the same version) is treated as ambiguous and raises rather than
    silently picking one - that shouldn't happen with a well-formed
    registry, so surfacing it beats guessing.
    """
    registry = _fetch_registry()
    candidates = _tags_providing(package, registry)
    if not candidates:
        raise RegistryError(f"no registered release provides {package!r}")

    if version is None:
        tag, entry = candidates[-1]
        return tag, entry["workflow"]

    suffixes = (f"-v{version}", f"-{version}")
    matches = [(tag, entry) for tag, entry in candidates if tag.endswith(suffixes)]
    if not matches:
        known = ", ".join(tag for tag, _ in candidates)
        raise RegistryError(
            f"no registered release of {package}=={version} (known tags providing {package}: {known})"
        )
    if len(matches) > 1:
        ambiguous = ", ".join(tag for tag, _ in matches)
        raise RegistryError(
            f"ambiguous: multiple registered tags providing {package} match version {version}: {ambiguous}"
        )
    tag, entry = matches[0]
    return tag, entry["workflow"]