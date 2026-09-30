"""pywheels: fetch and verify a wheel from python-wheels-builds before
anything gets near `pip install`.

pywheels is an independent, unofficial project - not affiliated with,
endorsed by, or sponsored by PyPA, PyPI, or the Python Software
Foundation. See the "Disclaimer" section in README.md.

    pywheels verify dbt-oss                 # best attested wheel for THIS machine
    pywheels verify dbt-oss==2.0.5          # ...pinned to an upstream version
    pywheels list dbt-oss                   # every attested wheel, per version/platform
    pywheels install dbt-oss                # verify, then install the exact wheel
    pywheels doctor

You never name a release tag. The registry (registry-v2.json, served off
python-wheels.github.io) describes every attested wheel - name, upstream
version, platform tag, python requirement, signer workflow, policy version,
upstream commit - all derived from the wheel's own verified attestations.
pywheels picks the wheel that fits your interpreter and platform (pip's own
tag ranking) across ALL releases, so `dbt-oss==2.0.5` finds the
`dbt-oss-v2.0.5.post1` musllinux build on Alpine and the `dbt-oss-v2.0.5`
win-arm64 build on Windows-on-ARM without either name being typed. Tags are
labels only; --tag / --wheel exist to pin one when you want to.

The registry is a hint, not the authority: the wheel's own attestations
must still verify, scoped to the exact signer workflow, and must agree with
what the registry claimed (policy version, predicate types, wheel digest).

WHAT IS VERIFIED - and what is not
  verified:      the exact wheel you install: its build provenance, and that
                 it was built from an unmodified upstream commit.
  NOT verified:  its dependencies (resolved by pip/uv at install time, to
                 whatever versions they resolve to) and the safety of the
                 upstream code itself. pywheels takes no responsibility for
                 either. `install` therefore installs only the verified wheel
                 (`pip install --no-deps <wheel>`), shows you its
                 Requires-Dist, and asks how to resolve them.

Backend selection (--backend auto|sigstore|gh, default auto): sigstore first
(offline, needs the optional `sigstore` extra), else the `gh` CLI (needs
network, nothing from pip). If neither is available verification of our own
wheels fails loudly - see verify.py's VerificationError - never a silent pass.

`install` first checks whether plain upstream has real wheels for your
platform (see the note on overrides.json below). If it does, that is not
ours to attest: pywheels says so and prints the plain `pip install` command
without running it. Only a wheel pywheels has verified is installed for you.
Use --attested-only to skip that probe and go straight to our build.

Not yet handled generally: some upstream packages hide their real wheel
availability behind package-specific indirection that plain pip resolution
can't see through - e.g. dbt-oss, whose PyPI "sdist" is actually a stub
build backend that downloads a real wheel from GitHub, keyed by platform
(see overrides.json). Add an entry there for each such package as you
confirm its actual mechanism - don't guess at a new package's mechanism
speculatively.

Legacy: with a schema-1 registry only, `verify <pkg> --tag <tag>` still
works via the GitHub REST API. `--local-dir` verification of files already
on disk (e.g. `gh run download` output) is unchanged.
"""

from __future__ import annotations

import argparse
import email
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from .github_release import (
    DownloadError,
    ReleaseAssets,
    fetch_registry_asset,
    fetch_release_assets,
    find_local_assets,
)
from .registry import (
    CANONICAL_REPO,
    Asset,
    NoCompatibleWheel,
    Registry,
    RegistryError,
    group_by_version,
    is_compatible,
    load as load_registry,
)
from .verify import (
    BUILD_PROVENANCE_PREDICATE,
    DEFAULT_UPSTREAM_PREDICATE_TYPE,
    VerificationError,
    gh_available,
    sigstore_available,
    verify_wheel,
    workflow_path,
)
from .release_trust import ReleaseTrust, Signature, collect_release_trust

HELP_SCOPE = (
    "Only the exact wheel is verified: its build provenance and that it was built from unmodified upstream "
    "source. Its dependencies and the safety of the upstream code are NOT verified; pywheels takes no "
    "responsibility for either."
)

SCOPE_NOTICE = (
    "scope: pywheels verified ONLY the exact wheel above - its build provenance and that it was built "
    "from unmodified upstream source.\n"
    "       It did NOT verify its dependencies (resolved by pip/uv at install time) or the safety of the "
    "upstream code, and takes no responsibility for either."
)


# ------------------------------------------------------------------ parser

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tag", default=None,
                   help="only consider this release (e.g. dbt-oss-v2.0.5.post1). Normally not needed: "
                        "the wheel is chosen by name, version and your platform")
    p.add_argument("--wheel", default=None, help="exact wheel filename to use instead of auto-selecting")
    p.add_argument("--workflow", default=None,
                   help="override the signer workflow (filename or path). Normally taken from the registry; "
                        "only for testing a workflow that isn't registered yet (e.g. with --local-dir)")
    p.add_argument("--ref", default="refs/heads/main", help="git ref the signing workflow ran from (default: %(default)s)")
    p.add_argument("--upstream-predicate-type", default=None,
                   help=f"upstream-source predicate type URL (default: from the registry, else {DEFAULT_UPSTREAM_PREDICATE_TYPE})")
    p.add_argument("--workdir", default=".pywheels-cache", help="where downloaded assets are cached (default: %(default)s)")
    p.add_argument("--no-source-archive", action="store_true",
                   help="don't download/check the release's source archive (can be over 1 GB). "
                        "The wheel's attestations are still fully verified")
    p.add_argument("--skip-workflow-audit", action="store_true",
                   help="don't fetch the signing workflow at its build commit to confirm the "
                        "'sha-pinned' policy label (one small request to raw.githubusercontent.com)")
    p.add_argument("--skip-release-check", action="store_true",
                   help="don't look up the release's immutable flag, tag signature and source-commit signature "
                        "(a few small GitHub API requests; informational only, never changes the verdict)")
    p.add_argument("--backend", choices=["auto", "sigstore", "gh"], default="auto",
                   help="verification backend (default: %(default)s - sigstore if installed, else gh)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="also print the raw per-attestation results; passes extra logging, repeatable (-vv). sigstore: its own -v/-vv. "
                        "gh: has no verbosity flag, so this asks for --format=json instead")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pywheels",
        description="Verify Sigstore-attested wheels from python-wheels-builds before installing. "
                    "Only the exact wheel is verified - never its dependencies.",
        epilog="Verified = the exact wheel's build provenance and unmodified upstream source. "
               "NOT verified = its dependencies and the safety of the upstream code; pywheels takes no "
               "responsibility for those. pywheels is an independent, unofficial project - not affiliated "
               "with, endorsed by, or sponsored by PyPA, PyPI, or the Python Software Foundation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="verify the attested wheel for your platform",
                       description="Resolve NAME[==VERSION] to the attested wheel that fits this machine, "
                                   "download it, and verify it. " + HELP_SCOPE)
    v.add_argument("package", help="package name, optionally pinned: dbt-oss, dbt-oss==2.0.5, 'dbt-oss>=2'")
    _add_common(v)
    v.add_argument("--local-dir", type=Path, default=None,
                   help="skip the download and verify files already on disk under this directory "
                        "(e.g. output of `gh run download`); needs --workflow")

    i = sub.add_parser("install", help="verify our attested wheel and install it (deps are your call)",
                       description="Verify the attested wheel for this platform, install exactly that file with "
                                   "`pip install --no-deps`, then show its Requires-Dist and ask how to resolve "
                                   "them. " + HELP_SCOPE)
    i.add_argument("package", help="package name, optionally pinned, e.g. dbt-oss or dbt-oss==2.0.5")
    _add_common(i)
    i.add_argument("--index-url", default=None, help="pip index to probe for upstream wheels (default: PyPI)")
    i.add_argument("--attested-only", action="store_true",
                   help="skip the upstream-wheel probe and go straight to our attested build")
    i.add_argument("--print-only", action="store_true",
                   help="verify, but print the pip commands instead of running them")
    i.add_argument("--deps", choices=["newest", "oldest", "skip"], default=None,
                   help="how to resolve the wheel's dependencies without asking: newest = pip's default, "
                        "oldest = lowest compatible versions (needs `uv` on PATH; pip has no such mode), "
                        "skip = install none")
    i.add_argument("--yes-deps", action="store_true", help="same as --deps newest (for scripts)")
    i.add_argument("--constraint", "-c", type=Path, default=None, help="constraints file passed through to pip/uv")

    ls = sub.add_parser("list", help="show every attested wheel in the registry, per version and platform")
    ls.add_argument("package", nargs="?", default=None, help="limit to one package")

    sub.add_parser("doctor", help="check which verification backends are usable, and how to fix the ones that aren't")
    return parser


# ----------------------------------------------------------------- helpers

def _parse_spec(spec: str):
    """'dbt_oss==2.0.5' -> ('dbt-oss', SpecifierSet('==2.0.5')). Splits name from
    version BEFORE anything builds a filename from it."""
    try:
        req = Requirement(spec)
    except InvalidRequirement as exc:
        raise ValueError(f"not a valid package requirement: {spec!r} ({exc})") from exc
    return str(canonicalize_name(req.name)), req.specifier


def _upstream_predicate(args, asset: Optional[Asset]) -> str:
    if args.upstream_predicate_type:
        return args.upstream_predicate_type
    if asset:
        for t in asset.predicate_types:
            if t != BUILD_PROVENANCE_PREDICATE:
                return t
    return DEFAULT_UPSTREAM_PREDICATE_TYPE


def _print_resolution(asset: Asset, sel_also: list) -> None:
    fit = "for this machine" if is_compatible(asset) else "NOT for this machine"
    print(f"resolved {asset.name} {asset.version} {fit} ({asset.wheel_tag})")
    print(f"  release   {asset.tag} (revision {asset.revision})")
    print(f"  upstream  {asset.upstream_label}")
    print(f"  policy    {asset.policy_version}")
    print(f"  signer    {asset.signer_workflow.rsplit('/', 1)[-1]}")
    if sel_also:
        older = ", ".join(f"{a.tag} [{a.policy_version}]" for a in sel_also)
        print(f"  also matched, not chosen (lower policy/revision or other version): {older}")


def _print_report(report, *, show_errors: bool = True) -> None:
    print(f"(using backend: {report.backend})")
    for a in report.attestations:
        status = "ignored" if a.ignored else ("OK" if a.verified else "FAILED")
        print(f"[{status}] {a.predicate_type or '(unknown predicate)'}")
        if show_errors and not a.verified and not a.ignored:
            print(f"    {a.detail}", file=sys.stderr)
    for t in report.required_predicate_types:
        if not any(a.verified and a.predicate_type == t for a in report.attestations):
            print(f"[FAILED] registry lists {t}, but the wheel has no verified attestation of that type", file=sys.stderr)
    if report.expected_policy_version is not None:
        if report.policy_ok:
            print(f"[OK] policy version {report.expected_policy_version} matches the registry")
        elif report.policy_ok is False:
            print(f"[FAILED] {report.policy_note}", file=sys.stderr)
        else:
            print(f"[unchecked] policy version: {report.policy_note}")
    if report.archive_checked:
        print(f"[{'OK' if report.archive_ok else 'FAILED'}] source archive digest")
        if not report.archive_ok and report.archive_check_error:
            print(f"    {report.archive_check_error}", file=sys.stderr)
    for m in report.mismatches:
        print(f"[FAILED] {m}", file=sys.stderr)
    for n in report.notes:
        print(f"[unchecked] {n}")


def _short_run(url: str) -> str:
    return url.split("github.com/", 1)[-1].replace("/attempts/1", "")


def _term_width() -> int:
    return max(40, shutil.get_terminal_size((80, 24)).columns - 1)


def _row(label: str, text: str, *, indent: int = 2, col: int = 16) -> None:
    """One `label   value` row. Long values wrap under the value column
    (hanging indent) instead of falling back to the start of the line."""
    lead = " " * indent + label.ljust(col - indent)
    print(textwrap.fill(text, width=_term_width(), initial_indent=lead,
                        subsequent_indent=" " * col, break_on_hyphens=False))


def _bullet(text: str, *, indent: int = 4) -> None:
    print(textwrap.fill(text, width=_term_width(), initial_indent=" " * indent + "- ",
                        subsequent_indent=" " * (indent + 2), break_on_hyphens=False))


def _sig_text(sig: Signature) -> str:
    if sig.state == "signed-person":
        return f"signed by {sig.signer} (GitHub reports the signature valid; pywheels does not pin the key)"
    if sig.state == "signed-web-flow":
        return "signed by GitHub's web-flow key - a web edit; it does not identify a person"
    if sig.state == "unsigned":
        return "unsigned" + (f" ({sig.reason})" if sig.reason and sig.reason != "unsigned" else "")
    return f"signature present but GitHub could not verify it ({sig.reason})"


def _print_transparency(f: dict, rt: Optional[ReleaseTrust]) -> None:
    """Rekor entry + release/tag/commit signing. Informational: every line
    says what was looked at, and none of it feeds the verdict."""
    rows: list = []
    if f.get("rekor_log_index") is not None:
        text = f"rekor.sigstore.dev entry #{f['rekor_log_index']}"
        if f.get("rekor_integrated_time") is not None:
            when = datetime.fromtimestamp(int(f["rekor_integrated_time"]), tz=timezone.utc)
            text += f", logged {when:%Y-%m-%d %H:%M} UTC"
        rows.append(("transparency log", text + " (entry carried by the verified bundle)"))
    if rt is not None:
        if rt.immutable is True:
            rows.append(("release", f"{rt.tag} is an immutable release - its tag and assets are locked once published"))
        elif rt.immutable is False:
            rows.append(("release", f"{rt.tag} is NOT immutable - its tag or assets could be changed after publishing"))
        if rt.release_attested is True:
            rows.append(("", "GitHub's release attestation matches this wheel (gh release verify-asset)"))
        elif rt.release_attested is False:
            rows.append(("", f"GitHub's release attestation did NOT match this wheel: {rt.release_attest_detail}"))
        if rt.tag_signature is not None:
            kind = f"{rt.tag_kind} tag, " if rt.tag_kind else ""
            rows.append(("tag", kind + _sig_text(rt.tag_signature)))
        if rt.tag_mismatch:
            rows.append(("", rt.tag_mismatch))
        if rt.commit_signature is not None and rt.commit_sha:
            rows.append(("source commit", f"{rt.commit_sha[:12]} " + _sig_text(rt.commit_signature)))
    if not rows and not (rt and rt.notes):
        return
    print("\n  transparency & signing (informational - the verdict does not depend on it)")
    for label, text in rows:
        _row(label, text, indent=4, col=22)
    for n in (rt.notes if rt else []):
        _bullet(f"unchecked: {n}")


def _print_receipt(asset: Asset, report, args) -> None:
    """Plain-English summary of what was checked. Every line is derived from
    the report - nothing here is printed unless the code actually checked it,
    and anything it could not check is said to be unchecked."""
    f = report.facts
    wf = workflow_path(args.workflow or asset.signer_workflow)
    ref = f" @ {args.ref}" if report.backend == "sigstore" else ""
    print(f"\nverified: {report.wheel.name}")
    _row("signed by", asset.builds_repo)
    _row("", f"{wf}{ref}")
    clean = ", clean tree" if f.get("tree_clean") is True else ""
    _row("built from", f"{asset.upstream_label}{clean} (as stated in the signed attestation)")
    how = []
    if f.get("event_name"):
        how.append("manually dispatched" if f["event_name"] == "workflow_dispatch" else f"triggered by {f['event_name']}")
    if f.get("runner_environment"):
        how.append(f"on a {f['runner_environment']} runner")
    if how:
        _row("build", " ".join(how))
    if f.get("build_commit"):
        _row("", f"builds-repo commit {f['build_commit'][:12]}")
        if f.get("invocation"):
            run = _short_run(f["invocation"])
            if run.startswith(asset.builds_repo + "/"):  # repo already shown under "signed by"
                run = run[len(asset.builds_repo) + 1:]
            _row("", f"run {run}")
    if report.policy_ok:
        _row("policy", f"{asset.policy_version} (matches the registry)")
    else:
        _row("policy", f"{asset.policy_version} (label only - not compared: {report.policy_note})")
    if report.pin_audit:
        n, _bad, path, commit = report.pin_audit
        _row("", f"all {n} action refs in {path.rsplit('/', 1)[-1]} @ {commit[:12]} are pinned to full commit SHAs")
    elif "sha-pinned" in asset.policy_version:
        _row("", "workflow pinning NOT confirmed by pywheels (audit skipped or unavailable)")
    else:
        _row("", "this pywheels can't check what that policy requires")

    print("\n  checked")
    if report.cross_checked_by:
        how_v = f"{report.backend} and {report.cross_checked_by[0]} agree"
    elif report.backend == "gh":
        how_v = "gh only, online - install sigstore for an offline cross-check"
    else:
        how_v = f"{report.backend} only - install gh for an online cross-check"
    _bullet(f"wheel digest is covered by a signed, transparency-logged attestation ({how_v})")
    _bullet("signer identity pinned to the workflow above; provenance names the same repo and workflow")
    _bullet("upstream repo, tag and commit in the attestation match the registry")
    if f.get("tree_clean") is True:
        _bullet("attestation asserts the upstream checkout was unmodified (tree_clean)")
    if report.archive_checked and report.archive_ok:
        size = f" ({report.archive_bytes / 1e6:,.1f} MB)" if report.archive_bytes else ""
        _bullet(f"source archive{size} matches the digest recorded in the attestation")
    elif not report.archive_checked:
        _bullet("source archive NOT checked (--no-source-archive)")
    for n in report.notes:
        _bullet(f"unchecked: {n}")
    _print_transparency(f, getattr(report, "release_trust", None))
    print("\n  trust boundary")
    _row("trusted", "builder repo and GitHub Actions", indent=4, col=18)
    _row("not covered", "wheel dependencies and upstream-code safety", indent=4, col=18)


def _select(args, registry: Registry, name: str, spec: SpecifierSet):
    return registry.select(name, spec, tag=args.tag, filename=args.wheel)


def _print_available(available: list) -> None:
    print("attested wheels that exist, but not for this machine:", file=sys.stderr)
    for a in sorted(available, key=lambda a: (a.version, a.wheel_tag, a.tag), reverse=True):
        print(f"  {a.name} {a.version:<8} {a.wheel_tag:<38} {a.tag}", file=sys.stderr)
    print("`pywheels verify <pkg> --wheel <filename>` checks one of them anyway (verify only - it can't be installed here).", file=sys.stderr)


def _fetch_and_verify(args, asset: Asset):
    dest = Path(args.workdir)
    dest.mkdir(parents=True, exist_ok=True)
    assets = fetch_registry_asset(asset, dest, want_archive=not args.no_source_archive)
    report = verify_wheel(
        assets.wheel,
        assets.bundle,
        repo=asset.builds_repo,
        workflow_file=args.workflow or asset.signer_workflow,
        ref=args.ref,
        upstream_predicate_type=_upstream_predicate(args, asset),
        source_archive_path=assets.archive,
        backend=args.backend,
        verbose=args.verbose,
        expected_policy_version=asset.policy_version,
        required_predicate_types=asset.predicate_types,
        expected_facts={
            "upstream_repo": asset.upstream_repo,
            "upstream_tag": asset.upstream_tag,
            "upstream_commit": asset.upstream_commit,
            "snapshot_archive_name": asset.archive_name if not args.no_source_archive else None,
        },
        audit_pins="sha-pinned" in asset.policy_version and not args.skip_workflow_audit,
    )
    return report, assets


# ------------------------------------------------------------------ verify

def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        name, spec = _parse_spec(args.package)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # -- files already on disk -------------------------------------------
    if args.local_dir is not None:
        if not args.workflow:
            print("error: --workflow is required with --local-dir (there's no registry entry to take it from)", file=sys.stderr)
            return 2
        try:
            assets = find_local_assets(name, args.local_dir)
            report = verify_wheel(
                assets.wheel, assets.bundle, repo=CANONICAL_REPO, workflow_file=args.workflow, ref=args.ref,
                upstream_predicate_type=_upstream_predicate(args, None), source_archive_path=assets.archive,
                backend=args.backend, verbose=args.verbose,
            )
        except (FileNotFoundError, VerificationError) as exc:
            print(f"{name}: could not verify - {exc}", file=sys.stderr)
            return 2
        return _finish_verify(name, report)

    try:
        registry = load_registry()
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # -- legacy schema-1 registry: tag-only, no platform data ---------------
    if registry.schema == 1:
        if not args.tag:
            print("error: the registry is schema 1 (tags only, no platform data): pass --tag", file=sys.stderr)
            return 2
        try:
            workflow = args.workflow or registry.workflow_for_tag(args.tag)
            dest = Path(args.workdir)
            dest.mkdir(parents=True, exist_ok=True)
            assets = fetch_release_assets(CANONICAL_REPO, args.tag, name, dest)
            report = verify_wheel(
                assets.wheel, assets.bundle, repo=CANONICAL_REPO, workflow_file=workflow, ref=args.ref,
                upstream_predicate_type=_upstream_predicate(args, None), source_archive_path=assets.archive,
                backend=args.backend, verbose=args.verbose,
            )
        except (RegistryError, FileNotFoundError, VerificationError) as exc:
            print(f"{name}: could not verify - {exc}", file=sys.stderr)
            return 2
        return _finish_verify(name, report)

    # -- schema 2: resolve by wheel -----------------------------------------
    try:
        sel = _select(args, registry, name, spec)
    except NoCompatibleWheel as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_available(exc.available)
        return 2
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_resolution(sel.chosen, sel.also_matched)
    if not is_compatible(sel.chosen):
        print("note: this wheel does not match your platform; verifying it anyway because you named it")
    try:
        report, _ = _fetch_and_verify(args, sel.chosen)
    except (FileNotFoundError, DownloadError, VerificationError) as exc:
        print(f"{name}: could not verify - {exc}", file=sys.stderr)
        return 2
    if report.ok and not args.skip_release_check:
        report.release_trust = collect_release_trust(
            repo=sel.chosen.builds_repo, tag=sel.chosen.tag,
            build_commit=report.facts.get("build_commit"), wheel=report.wheel,
        )
    return _finish_verify(sel.chosen.name, report, sel.chosen, args)


def _finish_verify(name: str, report, asset: Optional[Asset] = None, args=None) -> int:
    if report.ok and asset is not None:
        if args.verbose:
            _print_report(report)
        _print_receipt(asset, report, args)
        return 0
    _print_report(report)
    if report.ok:
        print(f"\n{name}: verified OK")
        print(SCOPE_NOTICE)
        return 0
    print(f"\n{name}: VERIFICATION FAILED - refusing to trust this wheel", file=sys.stderr)
    return 1


# -------------------------------------------------------------------- list

def _cmd_list(args: argparse.Namespace) -> int:
    try:
        registry = load_registry()
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if registry.schema == 1:
        print(f"registry (schema 1, tags only) at {registry.url}:")
        for tag, entry in registry.v1.items():
            print(f"  {tag}  provides {', '.join(entry.get('provides', []))}  workflow {entry.get('workflow')}")
        return 0

    assets = registry.for_package(args.package) if args.package else registry.assets
    if not assets:
        print(f"no registered release provides {args.package!r}" if args.package else "registry is empty", file=sys.stderr)
        return 1

    for name, versions in group_by_version(assets).items():
        print(name)
        for version, group in versions.items():
            print(f"  {version}   upstream {group[0].upstream_label}")
            for a in sorted(group, key=lambda a: (a.wheel_tag, a.policy_version, a.revision)):
                here = "*" if is_compatible(a) else " "
                print(f"    {here} {a.wheel_tag:<38} {a.tag:<24} policy {a.policy_version}")
    print("\n* = installable on this machine")
    return 0


# ----------------------------------------------------------------- install

_OVERRIDES_PATH = Path(__file__).resolve().parent / "overrides.json"


def _load_overrides() -> dict:
    try:
        return json.loads(_OVERRIDES_PATH.read_text())
    except FileNotFoundError:
        return {}


def _pip_can_resolve_wheels(spec: str, index_url: Optional[str]) -> bool:
    """Default strategy. Dry-run only - downloads (never installs) `spec` and
    its full dependency closure into a throwaway dir with --only-binary=:all:,
    then deletes it. Success means pip found real wheels for every package in
    that closure. --no-deps would defeat the point; don't add it here.

    Only correct when the package's own PyPI index metadata reflects what it
    can serve; see overrides.json for the confirmed exception."""
    with tempfile.TemporaryDirectory(prefix="pywheels-check-") as tmp:
        cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "-d", tmp, spec]
        if index_url:
            cmd += ["--index-url", index_url]
        return subprocess.run(cmd, capture_output=True).returncode == 0


def _pip_can_resolve_via_source_build(spec: str, index_url: Optional[str]) -> bool:
    """Override strategy "source-build-probe": for a package whose PyPI sdist
    is a stub whose PEP 517 backend downloads a prebuilt wheel keyed by
    platform (dbt-oss, see overrides.json), the only way to know if upstream
    covers this platform is to run that backend. --no-deps: we probe whether
    THIS package's backend serves this platform, not its dependencies."""
    with tempfile.TemporaryDirectory(prefix="pywheels-probe-") as tmp:
        cmd = [sys.executable, "-m", "pip", "download", "--no-binary=:all:", "--no-deps", "-d", tmp, spec]
        if index_url:
            cmd += ["--index-url", index_url]
        return subprocess.run(cmd, capture_output=True).returncode == 0


_STRATEGIES = {"source-build-probe": _pip_can_resolve_via_source_build}


def _upstream_available(spec: str, name: str, index_url: Optional[str]) -> bool:
    override = _load_overrides().get(name)
    strategy = _STRATEGIES.get(override["strategy"]) if override else None
    return (strategy or _pip_can_resolve_wheels)(spec, index_url)


def _print_cmd(parts: List[str]) -> None:
    print("  " + " ".join(shlex.quote(p) for p in parts))


def _wheel_requirements(wheel: Path) -> List[Requirement]:
    """Requires-Dist from the wheel's METADATA, with environment markers
    evaluated for this interpreter and extras left out."""
    with zipfile.ZipFile(wheel) as z:
        meta = next((n for n in z.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            raise VerificationError(f"{wheel.name} has no .dist-info/METADATA")
        msg = email.message_from_bytes(z.read(meta))
    reqs = []
    for line in msg.get_all("Requires-Dist") or []:
        r = Requirement(line)
        if r.marker is not None and not r.marker.evaluate({"extra": ""}):
            continue
        reqs.append(r)
    return reqs


def _pip_install(argv: List[str], *, print_only: bool) -> int:
    if print_only:
        _print_cmd(argv)
        return 0
    return subprocess.run(argv).returncode


def _install_attested(args, registry: Registry, asset: Asset, installed: dict) -> Optional[List[Requirement]]:
    """Verify `asset`, install exactly that file with --no-deps, and return its
    requirements that are NOT themselves attested by us. Any requirement the
    registry can serve for this platform (e.g. dbt-core alongside dbt-oss) goes
    through this same verified path instead of being handed to pip, where it
    could resolve to an unverified copy from PyPI. Returns None on failure."""
    try:
        report, assets = _fetch_and_verify(args, asset)
    except (FileNotFoundError, DownloadError, VerificationError) as exc:
        print(f"{asset.name}: could not verify ({exc})", file=sys.stderr)
        return None
    if not report.ok or args.verbose:
        _print_report(report)
    if not report.ok:
        print(f"\n{asset.name}: VERIFICATION FAILED for {asset.tag} - not installing", file=sys.stderr)
        return None
    _print_receipt(asset, report, args)
    print(f"\n{asset.name} {asset.version}: verified OK ({asset.tag}).")

    if _pip_install([sys.executable, "-m", "pip", "install", "--no-deps", str(assets.wheel)], print_only=args.print_only) != 0:
        print(f"{asset.name}: pip install --no-deps failed", file=sys.stderr)
        return None
    installed[asset.canonical_name] = asset

    remaining: List[Requirement] = []
    for req in _wheel_requirements(assets.wheel):
        key = str(canonicalize_name(req.name))
        if key in installed:
            continue
        try:
            sub = registry.select(req.name, req.specifier)
        except RegistryError:
            remaining.append(req)
            continue
        print(f"\n{asset.name} requires {req}: we have an attested build - installing that instead of letting pip choose")
        more = _install_attested(args, registry, sub.chosen, installed)
        if more is None:
            return None
        remaining.extend(more)
    return remaining


def _choose_deps(args, remaining: List[Requirement]) -> str:
    have_uv = shutil.which("uv") is not None
    print("\ndependencies of the installed wheel (NOT verified by pywheels):")
    for r in remaining:
        print(f"  {r}")
    if args.yes_deps:
        return "newest"
    if args.deps:
        if args.deps == "oldest" and not have_uv:
            print("error: --deps oldest needs `uv` on PATH (pip has no lowest-version mode)", file=sys.stderr)
            return "skip"
        return args.deps
    if not sys.stdin.isatty():
        print("not a terminal and no --deps/--yes-deps given: skipping dependencies.")
        return "skip"
    print("\nhow should they be resolved?")
    print("  1) let pip resolve - newest compatible (pip's default)")
    if have_uv:
        print("  2) oldest compatible versions (uv pip install --resolution lowest)")
    print("  3) skip - I'll install them myself")
    while True:
        choice = input("choice [1]: ").strip() or "1"
        if choice == "1":
            return "newest"
        if choice == "2" and have_uv:
            return "oldest"
        if choice == "3":
            return "skip"
        print("  please enter one of the listed numbers")


def _install_deps(args, remaining: List[Requirement], installed: dict) -> int:
    if not remaining:
        print("\nno third-party dependencies to install.")
        return 0
    mode = _choose_deps(args, remaining)
    if mode == "skip":
        print("skipping dependencies. install them yourself, e.g.:")
        _print_cmd(["pip", "install", *[str(r) for r in remaining]])
        return 0

    with tempfile.TemporaryDirectory(prefix="pywheels-pins-") as tmp:
        # Pin the wheels we verified, so resolution can fail loudly instead of
        # silently swapping one for an unverified copy from an index.
        pins = Path(tmp) / "pywheels-pins.txt"
        pins.write_text("".join(f"{a.name}=={a.version}\n" for a in installed.values()))
        constraint_args = ["-c", str(pins)] + (["-c", str(args.constraint)] if args.constraint else [])
        reqs = [str(r) for r in remaining]
        if mode == "oldest":
            argv = ["uv", "pip", "install", "--python", sys.executable, "--resolution", "lowest", *constraint_args, *reqs]
        else:
            argv = [sys.executable, "-m", "pip", "install", *constraint_args, *reqs]
        return _pip_install(argv, print_only=args.print_only)


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        name, spec = _parse_spec(args.package)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pip_spec = args.package

    if not args.attested_only:
        print(f"pywheels install {pip_spec}: checking whether pip can resolve an installable build from your configured indexes...")
        if _upstream_available(pip_spec, name, args.index_url):
            print(f"\npip can resolve an installable build of {pip_spec} and its dependencies from your configured indexes.")
            print("pywheels does not attest those artifacts - where they come from depends on your index configuration.")
            print("to install them anyway, run:\n")
            cmd = ["pip", "install", pip_spec] + (["--index-url", args.index_url] if args.index_url else [])
            _print_cmd(cmd)
            print("\n(add --attested-only to install our verified build instead)")
            return 0
        print(f"\npip could not resolve an installable build of {pip_spec} and its dependencies from your configured indexes.")

    print(f"checking {CANONICAL_REPO} for an attested build...")
    fallback = ["pip", "install", "--no-binary=:all:", pip_spec]
    try:
        registry = load_registry()
        if registry.schema < 2:
            raise RegistryError("the registry is schema 1 and can't pick a wheel for your platform")
        sel = _select(args, registry, name, spec)
    except RegistryError as exc:
        print(exc, file=sys.stderr)
        if isinstance(exc, NoCompatibleWheel):
            _print_available(exc.available)
        print("nothing we can vouch for. building from source, unverified, is your only option:\n")
        _print_cmd(fallback)
        return 1

    if not is_compatible(sel.chosen):
        print(f"{sel.chosen.filename} does not match this machine's platform/python; refusing to install it. "
              "(`verify --wheel` can still check it.)", file=sys.stderr)
        return 2

    _print_resolution(sel.chosen, sel.also_matched)
    installed: dict = {}
    remaining = _install_attested(args, registry, sel.chosen, installed)
    if remaining is None:
        print("\nrefusing to install an unverified wheel. building from source, unverified, is your remaining option:\n")
        _print_cmd(fallback)
        return 1

    rc = _install_deps(args, remaining, installed)
    print("\nreminder: only the wheel(s) above were verified - not their dependencies, and not the upstream code itself.")
    return rc


# ------------------------------------------------------------------ doctor

_GH_INSTALL_HINTS = {
    "Linux": "sudo apt install gh   (Debian/Ubuntu)  |  see https://github.com/cli/cli/blob/trunk/docs/install_linux.md for other distros",
    "Darwin": "brew install gh",
    "Windows": "winget install --id GitHub.cli   |   or: choco install gh",
}


def _cmd_doctor(_args: argparse.Namespace) -> int:
    print("pywheels doctor")
    print("-" * 44)
    any_backend = False

    if sigstore_available():
        proc = subprocess.run([sys.executable, "-m", "sigstore", "--version"], capture_output=True, text=True)
        if proc.returncode == 0:
            any_backend = True
            print(f"[OK]      sigstore backend: {(proc.stdout or proc.stderr).strip()}")
        else:
            print("[BROKEN]  sigstore is installed but `python -m sigstore` did not run cleanly")
            print(f"          {(proc.stderr or proc.stdout).strip()}")
            print("          fix: pip install --upgrade --force-reinstall sigstore")
    else:
        print("[missing] sigstore backend (optional)")
        print("          fix: pip install sigstore   (or: pip install pywheels[sigstore])")

    if gh_available():
        proc = subprocess.run(["gh", "--version"], capture_output=True, text=True)
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if proc.stdout or proc.stderr else "gh"
        any_backend = True
        print(f"[OK]      gh backend: {version}")
        auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if auth.returncode != 0:
            print("          note: gh is not authenticated (`gh auth login`) - public-repo verification "
                  "should still work, but may hit lower rate limits")
    else:
        print("[missing] gh backend (optional)")
        print(f"          fix: {_GH_INSTALL_HINTS.get(platform.system(), 'see https://cli.github.com')}")

    if gh_available():
        try:
            has_rv = subprocess.run(["gh", "release", "verify-asset", "--help"], capture_output=True).returncode == 0
        except OSError:
            has_rv = False
        print("[info]    gh release verify-asset: " + ("available (release attestation can be checked)" if has_rv
                                                       else "not in this gh - upgrade gh to check GitHub's release attestation"))
    if not (sigstore_available() and gh_available()):
        print("[info]    only one backend present: `auto` verifies with it alone. Install both for the "
              "offline + online cross-check")
    print("[info]    uv: " + (shutil.which("uv") or "not found (only needed for `install --deps oldest`)"))

    print("-" * 44)
    if any_backend:
        print("at least one verification backend is usable")
        return 0
    print("NO verification backend is usable - pywheels will refuse to verify any wheel")
    print("install sigstore or gh (see above) before relying on this tool")
    return 1


def main(argv=None) -> None:
    # stdout is block-buffered whenever it isn't a real terminal (CI), while
    # stderr is unbuffered, so failure diagnostics would jump ahead of the
    # OK/FAILED lines they explain. Force line-buffering so they interleave.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    args = _build_parser().parse_args(argv)
    handler = {"verify": _cmd_verify, "install": _cmd_install, "list": _cmd_list, "doctor": _cmd_doctor}[args.command]
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
