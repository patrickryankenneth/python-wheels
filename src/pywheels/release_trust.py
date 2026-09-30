"""Release-level signals for a wheel pywheels has ALREADY verified.

Everything here is informational. The verdict (VerifyReport.ok) comes only
from the signed attestations; nothing in this module can make a wheel pass
or fail. It answers "who stands behind this release, and could it have
changed since?":

  immutable   GitHub's `immutable` flag on the release (tag + assets locked
              once published), and - when `gh` supports it - GitHub's own
              release attestation checked against the wheel we downloaded.
  tag         lightweight (nothing to sign) or annotated, and whether
              GitHub reports its signature as valid.
  commit      the builds-repo commit the provenance says the build ran
              from: signed by a person, signed by GitHub's web-flow key
              (a web edit - proves nothing about WHO), or unsigned.

"Signed" here is what GitHub reports. pywheels does not pin a signer key
(yet), so a valid signature means "a key GitHub knows for that account",
not "the maintainer's key".

Every lookup can fail on its own (offline, rate-limited, old `gh`); a
failure becomes a note, never an exception and never a verdict.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .verify import gh_available

API = "https://api.github.com"


class ApiError(Exception):
    pass


@dataclass
class Signature:
    subject: str   # "tag" | "source commit"
    # signed-person | signed-web-flow | unsigned | unverified
    state: str
    signer: str = ""
    reason: str = ""


@dataclass
class ReleaseTrust:
    repo: str
    tag: str
    immutable: Optional[bool] = None          # None = GitHub didn't report it
    release_attested: Optional[bool] = None   # None = not checked
    release_attest_detail: Optional[str] = None
    tag_kind: Optional[str] = None            # "lightweight" | "annotated"
    tag_commit: Optional[str] = None
    tag_mismatch: Optional[str] = None        # tag and build commit differ (a finding, not a failure)
    tag_signature: Optional[Signature] = None
    commit_sha: Optional[str] = None
    commit_signature: Optional[Signature] = None
    notes: list = field(default_factory=list)


def _api(path: str):
    """GET a GitHub REST path. Prefers `gh api` (authenticated => better rate
    limits) and falls back to plain HTTPS, so it works without `gh`."""
    if gh_available():
        try:
            p = subprocess.run(["gh", "api", path, "-H", "Accept: application/vnd.github+json"],
                               capture_output=True, text=True, timeout=30)
            if p.returncode == 0:
                return json.loads(p.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass  # fall through to plain HTTPS
    req = urllib.request.Request(f"{API}/{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "pywheels"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ApiError(f"GitHub API /{path.split('?')[0]}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApiError(f"GitHub API /{path.split('?')[0]}: {exc}") from exc


def _classify(subject: str, verification: Optional[dict], *, name: Optional[str],
              login: Optional[str], email: Optional[str]) -> Signature:
    v = verification or {}
    if v.get("verified"):
        if login == "web-flow" or (email or "").lower() == "noreply@github.com":
            return Signature(subject, "signed-web-flow", "GitHub (web-flow)", v.get("reason", ""))
        who = name or login or "unknown"
        if login and login != name:
            who = f"{who} ({login})"
        return Signature(subject, "signed-person", who, v.get("reason", ""))
    reason = v.get("reason") or "unknown"
    return Signature(subject, "unsigned" if reason == "unsigned" else "unverified", reason=reason)


def _gh_has(*sub: str) -> bool:
    try:
        return subprocess.run(["gh", *sub, "--help"], capture_output=True, timeout=15).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def collect_release_trust(*, repo: str, tag: str, build_commit: Optional[str], wheel: Path) -> ReleaseTrust:
    rt = ReleaseTrust(repo=repo, tag=tag, commit_sha=build_commit)
    q_tag = urllib.parse.quote(tag, safe="")

    # -- release: immutable flag ----------------------------------------
    try:
        rel = _api(f"repos/{repo}/releases/tags/{q_tag}")
        flag = rel.get("immutable")
        rt.immutable = flag if isinstance(flag, bool) else None
        if rt.immutable is None:
            rt.notes.append("GitHub did not report an `immutable` flag for this release")
    except ApiError as exc:
        rt.notes.append(f"release immutability not checked: {exc}")

    # -- release attestation (only exists for immutable releases) ---------
    if rt.immutable:
        if not gh_available():
            rt.notes.append("GitHub's release attestation not checked: needs the `gh` CLI")
        elif not _gh_has("release", "verify-asset"):
            rt.notes.append("GitHub's release attestation not checked: this `gh` has no `release verify-asset` (upgrade gh)")
        else:
            try:
                p = subprocess.run(["gh", "release", "verify-asset", tag, str(wheel), "--repo", repo],
                                   capture_output=True, text=True, timeout=60)
                rt.release_attested = p.returncode == 0
                if p.returncode != 0:
                    lines = (p.stderr or p.stdout).strip().splitlines()
                    rt.release_attest_detail = lines[-1] if lines else "gh reported a failure"
            except (subprocess.TimeoutExpired, OSError) as exc:
                rt.notes.append(f"GitHub's release attestation not checked: {exc}")

    # -- tag ----------------------------------------------------------------
    try:
        ref = _api(f"repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='/')}")
        obj = ref.get("object") or {}
        if obj.get("type") == "tag":
            t = _api(f"repos/{repo}/git/tags/{obj['sha']}")
            tagger = t.get("tagger") or {}
            rt.tag_kind = "annotated"
            rt.tag_commit = (t.get("object") or {}).get("sha")
            rt.tag_signature = _classify("tag", t.get("verification"), name=tagger.get("name"),
                                         login=None, email=tagger.get("email"))
        elif obj.get("type") == "commit":
            rt.tag_kind = "lightweight"
            rt.tag_commit = obj.get("sha")
            rt.tag_signature = Signature("tag", "unsigned", reason="there is no tag object to sign")
        else:
            rt.notes.append(f"tag {tag} points at a {obj.get('type')!r}, not a commit or tag object")
    except (ApiError, KeyError) as exc:
        rt.notes.append(f"tag signature not checked: {exc}")

    if rt.tag_commit and build_commit and rt.tag_commit != build_commit:
        rt.tag_mismatch = (f"tag points at {rt.tag_commit[:12]}, but the provenance says the wheel was "
                           f"built from {build_commit[:12]}")

    # -- the commit the build ran from ----------------------------------------
    if build_commit:
        try:
            c = _api(f"repos/{repo}/commits/{build_commit}")
            cm = c.get("commit") or {}
            committer = cm.get("committer") or {}
            rt.commit_signature = _classify(
                "source commit", cm.get("verification"), name=committer.get("name"),
                login=(c.get("committer") or {}).get("login"), email=committer.get("email"))
        except ApiError as exc:
            rt.notes.append(f"source commit signature not checked: {exc}")
    else:
        rt.notes.append("source commit signature not checked: provenance has no build commit")
    return rt
