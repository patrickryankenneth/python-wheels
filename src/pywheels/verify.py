"""Verification logic for python-wheels-builds artifacts.

Two backends, tried in this order unless one is forced:

  1. sigstore  - offline, no network needed at verify time, but requires
                 the `sigstore` package (optional dependency) and a local
                 attestation bundle.
  2. gh        - the GitHub CLI, if it's on PATH. Needs network (it asks
                 GitHub for attestations on the wheel's digest directly),
                 but needs nothing installed via pip.

If neither is available, verification of one of OUR OWN wheels fails
loudly (VerificationError) rather than silently letting an unverified
wheel through - this tool exists specifically to not do that. Deciding
whether a requested package is even one of ours (vs. a plain upstream
package pywheels doesn't attest, which is fine to install unverified with
a warning) is an `install`-command concern, not this module's - see the
docstring in cli.py.

For a given wheel, a successful verification means:
  1. Every attestation we actually asked for verifies, scoped to the exact
     signer identity (repo + workflow file) - not just "signed by someone
     using GitHub Actions OIDC".
  2. Among those, there's a SLSA build-provenance attestation AND our own
     upstream-source attestation - both required.
  3. If a source archive is supplied, it hashes to the digest recorded in
     the upstream-source predicate (snapshot_archive_sha256).

  4. If the caller passes what the registry claims (expected_policy_version,
     required_predicate_types), the wheel's OWN attestations must agree. The
     registry is a hint, never the authority: a disagreement fails, it is
     not "corrected" in the registry's favour.

  5. What the signed attestations SAY is cross-checked against the registry
     (upstream repo/tag/commit, source archive name) and against the signer
     we pinned (workflow repository and path inside the SLSA provenance), and
     the upstream-source predicate must assert tree_clean. When the policy
     label is `...sha-pinned...`, the signing workflow file is fetched at the
     exact builds-repo commit recorded in the provenance and every `uses:`
     must be pinned to a full commit SHA - so that label is checked, not
     trusted. A workflow that ISN'T covered by a check is reported as
     unchecked, never silently counted as passing.

Attestation types we didn't ask for (IGNORED_PREDICATE_TYPES) are recorded
but never attempted or counted - see release/v0.2 below. (GitHub's release
attestation is looked at separately, and only as information, by
release_trust.py - it never changes the verdict here.)

The Rekor transparency-log entry inside each verified bundle (log index and
integration time) is recorded on the AttestationResult and surfaced as
facts["rekor_log_index"] / ["rekor_integrated_time"]. It is read from the
same bundle the backend just verified; no extra network call is made.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OIDC_ISSUER = "https://token.actions.githubusercontent.com"
BUILD_PROVENANCE_PREDICATE = "https://slsa.dev/provenance/v1"

# GitHub auto-attaches this the moment a wheel becomes part of a published
# Release. We never asked for it, its bundle has no Rekor tlog entry (so
# sigstore-python can't even parse it), and nothing we do depends on it.
# Record it, don't verify it, don't count it against the wheel.
IGNORED_PREDICATE_TYPES = {
    "https://in-toto.io/attestation/release/v0.2",
}

DEFAULT_UPSTREAM_PREDICATE_TYPE = "https://patrickryankenneth.github.io/attestations/upstream-source/v1"


class VerificationError(Exception):
    """Hard failure: missing files, unreadable bundle, or no usable
    backend at all. A verified-but-failed check is a normal outcome
    reported via VerifyReport.ok, not an exception."""


def sigstore_available() -> bool:
    return importlib.util.find_spec("sigstore") is not None


def gh_available() -> bool:
    return shutil.which("gh") is not None


@dataclass
class AttestationResult:
    predicate_type: str
    predicate: dict
    verified: bool
    detail: str
    ignored: bool = False
    # {"log_index": int, "integrated_time": Optional[int], ...} from the
    # bundle's Rekor tlog entry, or None if the bundle carries none.
    tlog: Optional[dict] = None


@dataclass
class VerifyReport:
    wheel: Path
    backend: str = ""
    attestations: list = field(default_factory=list)  # list[AttestationResult]
    archive_checked: bool = False
    archive_ok: Optional[bool] = None
    # Set when archive_ok is False *because we never got predicate content
    # to check against* (e.g. `gh attestation download` failed) rather than
    # because we checked it and it genuinely didn't match. Still fails
    # closed either way - see verify_wheel - but this lets callers show an
    # actionable message instead of a bare "FAILED" that looks identical to
    # a real hash mismatch.
    archive_check_error: Optional[str] = None
    # Registry cross-checks. required_predicate_types: every one must be
    # present and verified. policy_ok: None = nothing to compare against
    # (registry made no claim, or the predicate content wasn't retrieved -
    # see policy_note), True/False = compared. Only False blocks.
    required_predicate_types: tuple = ()
    expected_policy_version: Optional[str] = None
    found_policy_versions: tuple = ()
    policy_ok: Optional[bool] = None
    policy_note: Optional[str] = None
    # Decoded from the VERIFIED predicates (empty for anything unverified).
    facts: dict = field(default_factory=dict)
    mismatches: list = field(default_factory=list)  # attestation disagrees with registry/pin -> fails
    notes: list = field(default_factory=list)       # could not be compared -> shown, doesn't fail
    pin_audit: Optional[tuple] = None                # (n `uses:` checked, unpinned refs, workflow path, commit)
    archive_bytes: Optional[int] = None
    # Backends that independently re-verified the wheel in `auto` mode
    # (besides `backend`). Empty = single backend only - shown in the receipt.
    cross_checked_by: list = field(default_factory=list)
    # release_trust.ReleaseTrust, attached by the CLI after a successful
    # verification. Informational only - never part of `ok`.
    release_trust: Optional[object] = None

    @property
    def ok(self) -> bool:
        checked = [a for a in self.attestations if not a.ignored]
        has_build_provenance = any(
            a.verified and a.predicate_type == BUILD_PROVENANCE_PREDICATE for a in checked
        )
        has_upstream_source = any(
            a.verified and a.predicate_type and a.predicate_type != BUILD_PROVENANCE_PREDICATE
            for a in checked
        )
        archive_ok = self.archive_ok is not False  # not checked -> doesn't block
        required_ok = all(
            any(a.verified and a.predicate_type == t for a in checked) for t in self.required_predicate_types
        )
        policy_ok = self.policy_ok is not False
        return (has_build_provenance and has_upstream_source and archive_ok and required_ok
                and policy_ok and not self.mismatches)


def _workflow_path(workflow: str) -> str:
    """Accept either a bare filename (registry v1, --workflow:
    'build-x.yml') or the path registry v2 records ('.github/workflows/build-x.yml')."""
    return workflow if "/" in workflow else f".github/workflows/{workflow}"


def _signer_identity(repo: str, workflow_file: str, ref: str) -> str:
    return f"https://github.com/{repo}/{_workflow_path(workflow_file)}@{ref}"


def _signer_workflow(repo: str, workflow_file: str) -> str:
    return f"{repo}/{_workflow_path(workflow_file)}"


def _find_values(obj, key: str):
    """Every value stored under `key` anywhere inside a decoded predicate."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _find_values(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _find_values(item, key)


workflow_path = _workflow_path  # public name for cli.py


def _dig(obj, *keys):
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int) and k < len(obj):
            obj = obj[k]
        else:
            return None
    return obj


def _collect_facts(report: "VerifyReport") -> dict:
    """Pull the fields we care about out of VERIFIED predicates only."""
    facts: dict = {}
    for a in report.attestations:
        if not a.verified or not a.predicate:
            continue
        p = a.predicate
        if a.predicate_type == BUILD_PROVENANCE_PREDICATE:
            wf = _dig(p, "buildDefinition", "externalParameters", "workflow") or {}
            gh = _dig(p, "buildDefinition", "internalParameters", "github") or {}
            dep = _dig(p, "buildDefinition", "resolvedDependencies", 0) or {}
            tlog = a.tlog or {}
            facts.update(
                rekor_log_index=tlog.get("log_index"), rekor_integrated_time=tlog.get("integrated_time"),
                workflow_repository=wf.get("repository"), workflow_path=wf.get("path"), workflow_ref=wf.get("ref"),
                builder_id=_dig(p, "runDetails", "builder", "id"),
                event_name=gh.get("event_name"), runner_environment=gh.get("runner_environment"),
                build_commit=_dig(dep, "digest", "gitCommit"), build_commit_uri=dep.get("uri"),
                invocation=_dig(p, "runDetails", "metadata", "invocationId"),
            )
        elif "upstream_commit" in p:
            for k in ("upstream_repo", "upstream_tag", "upstream_commit", "tree_clean", "snapshot_archive_name"):
                if k in p:
                    facts[k] = p[k]
    return {k: v for k, v in facts.items() if v is not None}


_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^\s'\"#]+)")
_SHA_RE = re.compile(r"@[0-9a-f]{40}$")


def audit_workflow_pins(text: str):
    """(number of external `uses:` refs, [refs not pinned to a full commit SHA]).
    Local `./` actions are skipped; docker:// refs must carry an @sha256: digest."""
    total, bad = 0, []
    for line in text.splitlines():
        m = _USES_RE.match(line)
        if not m:
            continue
        ref = m.group(1)
        if ref.startswith("./"):
            continue
        total += 1
        ok = ("@sha256:" in ref) if ref.startswith("docker://") else bool(_SHA_RE.search(ref))
        if not ok:
            bad.append(ref)
    return total, bad


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.read().decode("utf-8")


def _finalize_checks(report: "VerifyReport", *, repo: str, workflow_file: str,
                     expected_facts: Optional[dict], audit_pins: bool) -> None:
    """Cross-check what the verified attestations say against what we expect."""
    f = report.facts = _collect_facts(report)
    mism, notes = report.mismatches, report.notes

    for k, want in (expected_facts or {}).items():
        if want is None:
            continue
        got = f.get(k)
        if got is None:
            notes.append(f"{k}: registry says {want!r}, but no verified attestation content to compare it with")
        elif got != want:
            mism.append(f"{k}: registry says {want!r}, but the wheel's own attestation says {got!r}")

    if "upstream_commit" in f:
        if f.get("tree_clean") is not True:
            mism.append(f"upstream-source attestation does not assert tree_clean=true (got {f.get('tree_clean')!r})")

    if f.get("workflow_repository") is not None and f["workflow_repository"] != f"https://github.com/{repo}":
        mism.append(f"provenance says the workflow ran in {f['workflow_repository']}, expected https://github.com/{repo}")
    if f.get("workflow_path") is not None and f["workflow_path"] != _workflow_path(workflow_file):
        mism.append(f"provenance says workflow {f['workflow_path']}, expected {_workflow_path(workflow_file)}")

    if not audit_pins:
        return
    commit, path, uri = f.get("build_commit"), f.get("workflow_path"), f.get("build_commit_uri", "")
    if not commit or not path or not re.fullmatch(r"[0-9a-f]{40}", commit):
        notes.append("workflow pin audit skipped: provenance has no usable builds-repo commit")
        return
    if repo not in uri:
        mism.append(f"provenance's build commit belongs to {uri!r}, not {repo}")
        return
    try:
        text = _fetch_text(f"https://raw.githubusercontent.com/{repo}/{commit}/{path}")
    except Exception as exc:  # network trouble is not a verification failure
        notes.append(f"workflow pin audit skipped: could not fetch {path} at {commit[:12]} ({exc})")
        return
    total, bad = audit_workflow_pins(text)
    report.pin_audit = (total, tuple(bad), path, commit)
    if total == 0:
        notes.append(f"workflow pin audit: found no `uses:` lines in {path} at {commit[:12]} - nothing to confirm")
    elif bad:
        mism.append(f"policy says SHA-pinned, but {path} at {commit[:12]} has unpinned actions: {', '.join(bad)}")


def _split_bundle_lines(bundle_jsonl: Path) -> list:
    """Each line of a `gh attestation download` bundle is a complete
    Sigstore Bundle on its own. Split into temp files for per-bundle
    verification."""
    if not bundle_jsonl.exists():
        raise VerificationError(f"attestation bundle not found: {bundle_jsonl}")
    lines = [line for line in bundle_jsonl.read_text().splitlines() if line.strip()]
    if not lines:
        raise VerificationError(f"attestation bundle is empty: {bundle_jsonl}")
    tmpdir = Path(tempfile.mkdtemp(prefix="pywheels-bundle-"))
    paths = []
    for i, line in enumerate(lines):
        p = tmpdir / f"bundle-{i}.json"
        p.write_text(line)
        paths.append(p)
    return paths


def _tlog_info(bundle_line_path: Path) -> Optional[dict]:
    """The Rekor entry the bundle carries, or None. Only ever attached to an
    attestation that a backend verified, so the numbers are not free-floating
    claims from an unchecked file."""
    try:
        bundle = json.loads(bundle_line_path.read_text())
    except (OSError, ValueError):
        return None
    entries = _dig(bundle, "verificationMaterial", "tlogEntries") or []
    if not entries or not isinstance(entries[0], dict):
        return None
    e = entries[0]
    try:
        index = int(e.get("logIndex"))
    except (TypeError, ValueError):
        return None
    try:
        when: Optional[int] = int(e.get("integratedTime"))
    except (TypeError, ValueError):
        when = None
    return {"log_index": index, "integrated_time": when, "log_id": _dig(e, "logId", "keyId")}


def _decode_predicate(bundle_line_path: Path):
    bundle = json.loads(bundle_line_path.read_text())
    payload_b64 = bundle["dsseEnvelope"]["payload"]
    statement = json.loads(base64.b64decode(payload_b64))
    return statement.get("predicateType", ""), statement.get("predicate", {})


# ---------------------------------------------------------------- sigstore

def _sigstore_verify_identity(target: Path, bundle: Path, cert_identity: str, *, verbose: int = 0):
    # sigstore's -v/-vv is a top-level flag (it sits on the `sigstore`
    # parser, before the subcommand), not an option on `verify identity`
    # itself - see `sigstore --help`.
    cmd = [sys.executable, "-m", "sigstore"] + ["-v"] * verbose + [
        "verify", "identity",
        "--bundle", str(bundle),
        "--cert-identity", cert_identity,
        "--cert-oidc-issuer", OIDC_ISSUER,
        "--offline",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _verify_with_sigstore(wheel_path: Path, bundle_jsonl_path: Path, *, repo: str, workflow_file: str, ref: str,
                           verbose: int = 0):
    cert_identity = _signer_identity(repo, workflow_file, ref)
    results = []
    for line_path in _split_bundle_lines(bundle_jsonl_path):
        predicate_type, predicate = _decode_predicate(line_path)
        if predicate_type in IGNORED_PREDICATE_TYPES:
            results.append(AttestationResult(
                predicate_type, predicate, verified=False,
                detail="not checked - known GitHub-native attestation, not one we asked for",
                ignored=True,
            ))
            continue
        ok, detail = _sigstore_verify_identity(wheel_path, line_path, cert_identity, verbose=verbose)
        results.append(AttestationResult(predicate_type, predicate, verified=ok, detail=detail,
                                         tlog=_tlog_info(line_path)))
    return results


# --------------------------------------------------------------------- gh

def _gh_verify_identity(wheel_path: Path, repo: str, signer_workflow: str, predicate_type: str, *, verbose: int = 0):
    # `gh attestation verify` has no -v/--verbose of its own. The closest
    # equivalent gh documents is --format=json, which dumps the full
    # verification result instead of the one-line human summary - so
    # that's what verbose asks for here rather than a made-up flag.
    cmd = [
        "gh", "attestation", "verify", str(wheel_path),
        "--repo", repo,
        "--signer-workflow", signer_workflow,
        "--predicate-type", predicate_type,
    ]
    if verbose:
        cmd += ["--format", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _gh_download_bundle(wheel_path: Path, repo: str) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="pywheels-gh-"))
    # `gh` runs with cwd=tmpdir (below) so its output bundle lands somewhere
    # we can glob cleanly, isolated from anything else in the caller's cwd.
    # wheel_path may be relative (e.g. the CLI's default --workdir is
    # ".pywheels-cache") - resolved against the CALLER's cwd, not tmpdir.
    # Passing it through unresolved means `gh` looks for it relative to
    # tmpdir instead, where it never exists - it then fails trying to open
    # the wheel to hash it, in a way that reads like an attestation/archive
    # problem but is really just this path bug. Resolve before handing it
    # to a subprocess running in a different directory.
    proc = subprocess.run(
        ["gh", "attestation", "download", str(wheel_path.resolve()), "--repo", repo],
        capture_output=True, text=True, cwd=tmpdir,
    )
    if proc.returncode != 0:
        raise VerificationError(f"gh attestation download failed: {(proc.stderr or proc.stdout).strip()}")
    matches = list(tmpdir.glob("*.jsonl"))
    if not matches:
        raise VerificationError("gh attestation download did not produce a bundle file")
    return matches[0]


def _verify_with_gh(wheel_path: Path, *, repo: str, workflow_file: str, upstream_predicate_type: str,
                     verbose: int = 0):
    signer_workflow = _signer_workflow(repo, workflow_file)
    results = []

    ok, detail = _gh_verify_identity(wheel_path, repo, signer_workflow, BUILD_PROVENANCE_PREDICATE, verbose=verbose)
    results.append(AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=ok, detail=detail))

    ok, detail = _gh_verify_identity(wheel_path, repo, signer_workflow, upstream_predicate_type, verbose=verbose)
    results.append(AttestationResult(upstream_predicate_type, {}, verified=ok, detail=detail))

    # Bonus, not required: pull predicate content too, purely for the
    # archive-digest check below. A plain download - no sigstore parsing.
    # NOTE: this can fail for reasons that have nothing to do with the
    # wheel itself or with the identity checks above - `gh attestation
    # download` is a separate code path in `gh` from `verify identity` and
    # can fail on its own (network blip, rate limit, a `gh` bug, etc). The
    # identity checks above already ran and stand on their own, so we
    # don't turn this into a VerificationError - but we DO surface *why*
    # it failed instead of swallowing it, so a caller checking the archive
    # digest afterward can tell "we never got to check" apart from "we
    # checked and it didn't match", and can actually see the real reason
    # instead of guessing. See VerifyReport.archive_check_error.
    download_error = None
    try:
        bundle_path = _gh_download_bundle(wheel_path, repo)
        for line_path in _split_bundle_lines(bundle_path):
            predicate_type, predicate = _decode_predicate(line_path)
            for r in results:
                if r.predicate_type == predicate_type:
                    if not r.predicate:
                        r.predicate = predicate
                    if r.tlog is None:
                        r.tlog = _tlog_info(line_path)
    except VerificationError as exc:
        download_error = str(exc)

    return results, download_error


# ------------------------------------------------------------------- main

def verify_wheel(
    wheel_path: Path,
    bundle_jsonl_path: Optional[Path] = None,
    *,
    repo: str,
    workflow_file: str,
    ref: str = "refs/heads/main",
    upstream_predicate_type: str = DEFAULT_UPSTREAM_PREDICATE_TYPE,
    source_archive_path: Optional[Path] = None,
    backend: str = "auto",
    verbose: int = 0,
    expected_policy_version: Optional[str] = None,
    required_predicate_types: tuple = (),
    expected_facts: Optional[dict] = None,
    audit_pins: bool = False,
) -> VerifyReport:
    if not wheel_path.exists():
        raise VerificationError(f"wheel not found: {wheel_path}")

    chosen = backend
    if chosen == "auto":
        if sigstore_available():
            chosen = "sigstore"
        elif gh_available():
            chosen = "gh"
        else:
            raise VerificationError(
                "no verification backend available - install sigstore (`pip install sigstore`) "
                "or the GitHub CLI (`gh`, see https://cli.github.com). Run `pywheels doctor` for "
                "details. Refusing to treat an unverifiable wheel as trusted."
            )

    report = VerifyReport(
        wheel=wheel_path, backend=chosen,
        required_predicate_types=tuple(required_predicate_types),
        expected_policy_version=expected_policy_version,
    )

    if chosen == "sigstore":
        if bundle_jsonl_path is None:
            raise VerificationError("sigstore backend requires a local attestation bundle")
        report.attestations = _verify_with_sigstore(
            wheel_path, bundle_jsonl_path, repo=repo, workflow_file=workflow_file, ref=ref, verbose=verbose,
        )
    elif chosen == "gh":
        report.attestations, report.archive_check_error = _verify_with_gh(
            wheel_path, repo=repo, workflow_file=workflow_file, upstream_predicate_type=upstream_predicate_type,
            verbose=verbose,
        )
    else:
        raise VerificationError(f"unknown backend: {chosen!r}")

    # `auto` runs EVERY usable backend, not just the first: sigstore checks
    # the local bundle offline, gh asks GitHub online. Both must agree; a
    # missing one is only a note (sigstore is an optional dependency).
    if backend == "auto":
        other = "gh" if chosen == "sigstore" else "sigstore"
        try:
            if other == "gh" and gh_available():
                atts, _err = _verify_with_gh(
                    wheel_path, repo=repo, workflow_file=workflow_file,
                    upstream_predicate_type=upstream_predicate_type, verbose=verbose,
                )
            elif other == "sigstore" and sigstore_available() and bundle_jsonl_path is not None:
                atts = _verify_with_sigstore(
                    wheel_path, bundle_jsonl_path, repo=repo, workflow_file=workflow_file, ref=ref, verbose=verbose,
                )
            else:
                atts = None
        except VerificationError as exc:
            atts = None
            report.notes.append(f"{other} cross-check could not run: {exc}")
        if atts is not None:
            bad = [a.predicate_type or "(unknown predicate)" for a in atts if not a.ignored and not a.verified]
            if bad:
                report.mismatches.append(f"{other} backend disagrees with {chosen}: failed {', '.join(bad)}")
            else:
                report.cross_checked_by.append(other)

    if expected_policy_version is not None:
        found = sorted({
            str(v) for a in report.attestations if a.verified and a.predicate
            for v in _find_values(a.predicate, "policy_version")
        })
        report.found_policy_versions = tuple(found)
        if not found:
            report.policy_note = (
                f"registry says policy {expected_policy_version}, but no verified attestation predicate "
                "carries a policy_version to compare it with"
                + (f" ({report.archive_check_error})" if report.archive_check_error else "")
            )
        else:
            report.policy_ok = found == [expected_policy_version]
            if not report.policy_ok:
                report.policy_note = (
                    f"registry says policy {expected_policy_version}, but the wheel's own attestation says "
                    f"{', '.join(found)}"
                )

    if source_archive_path is not None:
        report.archive_checked = True
        expected = next(
            (a.predicate["snapshot_archive_sha256"] for a in report.attestations
             if a.verified and a.predicate and "snapshot_archive_sha256" in a.predicate),
            None,
        )
        if expected is None:
            # Still fail closed - we have no digest to trust the archive
            # against, whether that's because the predicate genuinely
            # doesn't carry one or because we never fetched it. But make
            # the two cases distinguishable to the caller: if a download
            # error is on record, this ISN'T a real digest mismatch.
            report.archive_ok = False
            if report.archive_check_error is None:
                report.archive_check_error = (
                    "no snapshot_archive_sha256 found in any verified attestation's predicate "
                    "(predicate content was retrieved, but the digest was genuinely absent)"
                )
        else:
            h = hashlib.sha256()
            with source_archive_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            report.archive_bytes = source_archive_path.stat().st_size
            report.archive_ok = (h.hexdigest() == expected)

    _finalize_checks(report, repo=repo, workflow_file=workflow_file,
                     expected_facts=expected_facts, audit_pins=audit_pins)
    return report