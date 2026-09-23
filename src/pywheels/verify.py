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

Attestation types we didn't ask for (IGNORED_PREDICATE_TYPES) are recorded
but never attempted or counted - see release/v0.2 below.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
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
        return has_build_provenance and has_upstream_source and archive_ok


def _signer_identity(repo: str, workflow_file: str, ref: str) -> str:
    return f"https://github.com/{repo}/.github/workflows/{workflow_file}@{ref}"


def _signer_workflow(repo: str, workflow_file: str) -> str:
    return f"{repo}/.github/workflows/{workflow_file}"


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
        results.append(AttestationResult(predicate_type, predicate, verified=ok, detail=detail))
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
                if r.predicate_type == predicate_type and not r.predicate:
                    r.predicate = predicate
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

    report = VerifyReport(wheel=wheel_path, backend=chosen)

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
            actual = hashlib.sha256(source_archive_path.read_bytes()).hexdigest()
            report.archive_ok = (actual == expected)

    return report