"""pywheels: fetch and verify a wheel from python-wheels-builds before
anything gets near `pip install`.

pywheels is an independent, unofficial project - not affiliated with,
endorsed by, or sponsored by PyPA, PyPI, or the Python Software
Foundation. See the "Disclaimer" section in README.md.

    pywheels verify dbt-core --tag dbt-oss-v2.0.5

    pywheels verify dbt-core --workflow smoke-test-build-dbt-oss.yml \
        --local-dir ./pywheels-sample

    pywheels doctor

Backend selection (--backend auto|sigstore|gh, default auto): try sigstore
first (offline, needs the optional `sigstore` extra), fall back to the
`gh` CLI if that's not installed (needs network, needs nothing from pip).
If neither is available, verification of one of pywheels' own wheels fails
loudly - see verify.py's VerificationError, not a silent pass-through.

There's no --repo flag: the repo pywheels verifies against is fixed
(CANONICAL_REPO in registry.py) - there's only ever one, so there's
nothing to pass or get wrong. --workflow is normally resolved for you too:
given --tag, pywheels looks up which workflow file actually built and
attested that specific release from registry.json, a small static file
served off python-wheels.github.io and keyed by tag (see registry.py for
why - short version: fetched as a plain HTTPS GET off GitHub Pages, not
the GitHub REST API, so it isn't subject to that API's 60-requests/hour
unauthenticated rate limit). Only pass --workflow yourself to test a
workflow that isn't registered yet - e.g. together with --local-dir,
where there's no tag to look it up by at all.

    pywheels install dbt-core==2.0.5

--tag/--workflow are both optional here too, resolved from the registry -
--tag defaults to the latest attested build of the package; only pass
either yourself to pin to a specific release or test an unregistered
workflow.

v1 `install` does NOT call `pip install` for you. It only tells you which
`pip install ...` command is safe to run, because "safe" here specifically
means "verified", and we can only ever verify wheels signed by our own
canonical repo - never an arbitrary upstream wheel. Concretely, per package:
  1. Can pip resolve real, non-source wheels for this package and its
     whole dependency closure on your platform? If so, say so and print
     the plain `pip install` command - unverified, since it's not ours to
     attest, but not blocked either.
     NOTE: for a pure-Python package like dbt-core itself this check is
     almost meaningless on its own (dbt-core ships one universal
     py3-none-any wheel for every platform) - the real gap for us is
     always in a *dependency* that ships real per-platform wheels (e.g.
     dbt-extractor, a Rust extension with no win-arm64 wheel on PyPI).
     Because pip resolves the whole tree, not just the top-level package,
     this check does still fail correctly on an unsupported platform -
     it's just resolving the dependency graph, not "does dbt-core have a
     wheel". Nothing here inspects any dependency's own download-a-binary
     logic at import/run time (if one exists) - see the "not yet handled"
     note below.
  2. Otherwise, fetch the matching wheel for --tag (or the registry's
     latest attested build, if --tag wasn't given) and run it through
     verify_wheel. If it verifies, print the `pip install` command for the
     wheel now sitting in --workdir. If verification fails or the backend
     can't run at all, say so plainly and do NOT print an install command
     for it.
  3. If neither worked, print the `pip install --no-binary=:all:` command
     as the last resort, with no attestation behind it either.

Not yet handled generally: some upstream packages hide their real wheel
availability behind package-specific indirection that plain pip resolution
can't see through - e.g. dbt-oss, whose PyPI "sdist" is actually a stub
build backend that downloads a real wheel from GitHub, keyed by platform,
with no source to fall back to (see overrides.json). Add an entry there for
each such package as you confirm its actual mechanism (e.g. pytorch, which
needs a non-default --index-url to see real wheels at all) - don't guess at
a new package's mechanism speculatively; confirm it the way dbt-oss's was
confirmed, by reading what its build backend or index actually does.

`verify` always assumes you're pointing it at one of our own wheels; the
"is this actually a python-wheels artifact, or a normal upstream package
we don't sign" decision lives in `install`, above.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .github_release import fetch_release_assets, find_local_assets
from .registry import CANONICAL_REPO, RegistryError, resolve as registry_resolve, workflow_for_tag
from .verify import (
    verify_wheel,
    VerificationError,
    sigstore_available,
    gh_available,
    DEFAULT_UPSTREAM_PREDICATE_TYPE,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pywheels",
        description="Verify Sigstore-attested wheels from python-wheels-builds before installing.",
        epilog="pywheels is an independent, unofficial project - not affiliated with, "
               "endorsed by, or sponsored by PyPA, PyPI, or the Python Software Foundation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="verify a wheel's attestations before you trust it")
    v.add_argument("package", help="package name, e.g. dbt-core")
    v.add_argument("--tag", help="release tag, e.g. dbt-oss-v2.0.5 (ignored with --local-dir)")
    v.add_argument("--workflow", default=None,
                    help="workflow filename that signed it, e.g. build-dbt-oss-win-arm64.yml. "
                         "Normally looked up automatically from the registry for --tag; only "
                         "pass this yourself to test a workflow that isn't registered yet "
                         "(e.g. together with --local-dir)")
    v.add_argument("--ref", default="refs/heads/main", help="git ref the signing workflow ran from (default: %(default)s)")
    v.add_argument("--upstream-predicate-type", default=DEFAULT_UPSTREAM_PREDICATE_TYPE,
                    help="predicate type URL for the upstream-source attestation (default: %(default)s)")
    v.add_argument("--workdir", default=".pywheels-cache", help="where downloaded assets are cached (default: %(default)s)")
    v.add_argument("--local-dir", type=Path, default=None,
                    help="skip the GitHub fetch and verify files already on disk under this directory "
                         "instead (e.g. output of `gh run download`)")
    v.add_argument("--backend", choices=["auto", "sigstore", "gh"], default="auto",
                    help="verification backend (default: %(default)s - sigstore if installed, else gh)")
    v.add_argument("-v", "--verbose", action="count", default=0,
                    help="pass through extra logging to the backend. Repeatable (-vv). "
                         "sigstore: raises its own -v/-vv debug logging. "
                         "gh: has no verbosity flag, so this instead asks for --format=json "
                         "(the closest thing gh offers to a detailed result dump).")

    i = sub.add_parser("install", help="print the safe pip install command for a package: verified ours, or plain upstream, or source")
    i.add_argument("package", help="package name, optionally pinned, e.g. dbt-core or dbt-core==2.0.5")
    i.add_argument("--workflow", default=None,
                    help="workflow filename that signed the build, e.g. build-dbt-oss-win-arm64.yml. "
                         "Normally resolved automatically from the registry - only pass this to "
                         "test an unregistered workflow yourself")
    i.add_argument("--tag", default=None,
                    help="exact release tag to use (default: resolved automatically from the "
                         "registry - latest attested build of this package, or the exact one "
                         "matching a pinned version like dbt-core==2.0.5)")
    i.add_argument("--ref", default="refs/heads/main", help="git ref the signing workflow ran from (default: %(default)s)")
    i.add_argument("--upstream-predicate-type", default=DEFAULT_UPSTREAM_PREDICATE_TYPE,
                    help="predicate type URL for the upstream-source attestation (default: %(default)s)")
    i.add_argument("--index-url", default=None, help="pip index to check for upstream wheels (default: pip's own default, i.e. PyPI)")
    i.add_argument("--workdir", default=".pywheels-cache", help="where our downloaded wheel is cached if we fetch one (default: %(default)s)")
    i.add_argument("--backend", choices=["auto", "sigstore", "gh"], default="auto",
                    help="verification backend for our own wheel (default: %(default)s - sigstore if installed, else gh)")
    i.add_argument("-v", "--verbose", action="count", default=0, help="pass through extra logging to the verify backend")

    sub.add_parser("doctor", help="check which verification backends are usable, and how to fix the ones that aren't")

    return parser


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.local_dir is None and not args.tag:
        print("error: pass either --tag (to fetch a release) or --local-dir (to use files on disk)", file=sys.stderr)
        return 2

    workflow = args.workflow
    if workflow is None:
        if args.tag is None:
            print(
                "error: --workflow is required with --local-dir, since there's no tag to "
                "look it up by (a tag not yet registered has nothing to look up)",
                file=sys.stderr,
            )
            return 2
        try:
            workflow = workflow_for_tag(args.tag)
        except RegistryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        if args.local_dir is not None:
            assets = find_local_assets(args.package, args.local_dir)
        else:
            dest = Path(args.workdir)
            dest.mkdir(parents=True, exist_ok=True)
            assets = fetch_release_assets(CANONICAL_REPO, args.tag, args.package, dest)

        report = verify_wheel(
            assets.wheel,
            assets.bundle,
            repo=CANONICAL_REPO,
            workflow_file=workflow,
            ref=args.ref,
            upstream_predicate_type=args.upstream_predicate_type,
            source_archive_path=assets.archive,
            backend=args.backend,
            verbose=args.verbose,
        )
    except (FileNotFoundError, VerificationError) as exc:
        print(f"{args.package}: could not verify - {exc}", file=sys.stderr)
        return 2

    print(f"(using backend: {report.backend})")
    for a in report.attestations:
        status = "ignored" if a.ignored else ("OK" if a.verified else "FAILED")
        print(f"[{status}] {a.predicate_type or '(unknown predicate)'}")
        if not a.verified and not a.ignored:
            print(f"    {a.detail}", file=sys.stderr)

    if report.archive_checked:
        print(f"[{'OK' if report.archive_ok else 'FAILED'}] source archive digest")
        if not report.archive_ok and report.archive_check_error:
            print(f"    {report.archive_check_error}", file=sys.stderr)

    if report.ok:
        print(f"\n{args.package}: verified OK")
        return 0

    print(f"\n{args.package}: VERIFICATION FAILED - refusing to trust this wheel", file=sys.stderr)
    return 1


def _split_package_spec(spec: str):
    """'dbt-core==2.0.5' -> ('dbt-core', '2.0.5'); 'dbt-core' -> ('dbt-core', None)."""
    if "==" in spec:
        name, version = spec.split("==", 1)
        return name, version
    return spec, None


_OVERRIDES_PATH = Path(__file__).resolve().parent / "overrides.json"


def _load_overrides() -> dict:
    try:
        return json.loads(_OVERRIDES_PATH.read_text())
    except FileNotFoundError:
        return {}


def _pip_can_resolve_wheels(spec: str, index_url: Optional[str]) -> bool:
    """Default strategy. Dry-run only - downloads (never installs) `spec`
    and its full dependency closure into a throwaway dir with
    --only-binary=:all:, then deletes it. Success means pip found real
    wheels for every package in that closure, not just the top-level one:
    a pure-Python top-level package (like dbt-core's own py3-none-any
    wheel) will "have a wheel" on every platform regardless, so this check
    only means something because pip resolves dependencies too - that's
    where a real per-platform gap (e.g. dbt-extractor having no win-arm64
    wheel) actually shows up. --no-deps would defeat the point; don't add
    it here.

    Only correct when the package's own PyPI index metadata actually
    reflects what it can serve. Some packages hide real wheel availability
    behind a stub build backend or an alternate index - see overrides.json
    and _pip_can_resolve_via_source_build for the confirmed exception."""
    with tempfile.TemporaryDirectory(prefix="pywheels-check-") as tmp:
        cmd = [sys.executable, "-m", "pip", "download", "--only-binary=:all:", "-d", tmp, spec]
        if index_url:
            cmd += ["--index-url", index_url]
        return subprocess.run(cmd, capture_output=True).returncode == 0


def _pip_can_resolve_via_source_build(spec: str, index_url: Optional[str]) -> bool:
    """Override strategy "source-build-probe". For a package whose PyPI
    "sdist" never actually compiles anything - it's a stub whose PEP 517
    build backend downloads a real prebuilt wheel from elsewhere, keyed by
    platform (see overrides.json's note for dbt-oss specifically) - the
    only way to know if upstream covers this platform is to actually run
    that backend. --no-deps: we're probing whether THIS package's own
    backend serves this platform, not re-checking its dependencies (which
    the default strategy already handles fine when they're ordinary
    wheels, as dbt-oss's sole dependency `mashumaro` is)."""
    with tempfile.TemporaryDirectory(prefix="pywheels-probe-") as tmp:
        cmd = [sys.executable, "-m", "pip", "download", "--no-binary=:all:", "--no-deps", "-d", tmp, spec]
        if index_url:
            cmd += ["--index-url", index_url]
        return subprocess.run(cmd, capture_output=True).returncode == 0


_STRATEGIES = {
    "source-build-probe": _pip_can_resolve_via_source_build,
}


def _upstream_available(spec: str, package: str, index_url: Optional[str]) -> bool:
    override = _load_overrides().get(package)
    strategy = _STRATEGIES.get(override["strategy"]) if override else None
    check = strategy or _pip_can_resolve_wheels
    return check(spec, index_url)


def _print_pip_cmd(*parts: str) -> None:
    print("  " + " ".join(parts))


def _cmd_install(args: argparse.Namespace) -> int:
    package, version = _split_package_spec(args.package)
    spec = args.package

    print(f"pywheels install {spec}: checking whether pip can resolve real wheels for your platform...")
    if _upstream_available(spec, package, args.index_url):
        print(f"\nupstream has wheels for {spec} and its dependencies on this platform.")
        print("this is NOT attested by us - we only ever verify wheels from our own canonical repo. safe to run:\n")
        cmd = ["pip", "install", spec]
        if args.index_url:
            cmd += ["--index-url", args.index_url]
        _print_pip_cmd(*cmd)
        return 0

    print(f"\npip could not resolve real wheels for {spec} and its dependencies on this platform.")
    print(f"checking {CANONICAL_REPO} for an attested build...")

    workflow = args.workflow
    if args.tag and workflow is None:
        try:
            workflow = workflow_for_tag(args.tag)
        except RegistryError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
    elif not args.tag:
        try:
            tag, resolved_workflow = registry_resolve(package, version)
        except RegistryError as exc:
            print(f"{exc}")
            print("nothing we can vouch for. building from source, unverified, is your only option:\n")
            _print_pip_cmd("pip", "install", "--no-binary=:all:", spec)
            return 1
        args.tag = tag
        if workflow is None:
            workflow = resolved_workflow
        if not version:
            print(f"no version pinned - using latest attested build: {tag}")

    try:
        tag = args.tag
        dest = Path(args.workdir)
        dest.mkdir(parents=True, exist_ok=True)
        assets = fetch_release_assets(CANONICAL_REPO, tag, package, dest)
    except FileNotFoundError as exc:
        print(f"{CANONICAL_REPO}: no wheel available either ({exc})")
        print("nothing we can vouch for. building from source, unverified, is your only option:\n")
        _print_pip_cmd("pip", "install", "--no-binary=:all:", spec)
        return 1

    try:
        report = verify_wheel(
            assets.wheel,
            assets.bundle,
            repo=CANONICAL_REPO,
            workflow_file=workflow,
            ref=args.ref,
            upstream_predicate_type=args.upstream_predicate_type,
            source_archive_path=assets.archive,
            backend=args.backend,
            verbose=args.verbose,
        )
    except VerificationError as exc:
        print(f"{args.package}: could not verify ({exc})", file=sys.stderr)
        print("refusing to recommend an unverifiable wheel. building from source, unverified:\n")
        _print_pip_cmd("pip", "install", "--no-binary=:all:", spec)
        return 2

    print(f"(using backend: {report.backend})")
    for a in report.attestations:
        status = "ignored" if a.ignored else ("OK" if a.verified else "FAILED")
        print(f"[{status}] {a.predicate_type or '(unknown predicate)'}")
    if report.archive_checked:
        print(f"[{'OK' if report.archive_ok else 'FAILED'}] source archive digest")
        if not report.archive_ok and report.archive_check_error:
            print(f"    {report.archive_check_error}", file=sys.stderr)

    if not report.ok:
        print(f"\n{args.package}: VERIFICATION FAILED for {tag} - refusing to recommend this wheel", file=sys.stderr)
        print("building from source, unverified, is your remaining option:\n")
        _print_pip_cmd("pip", "install", "--no-binary=:all:", spec)
        return 1

    print(f"\n{args.package}: verified OK ({tag}). safe to run:\n")
    _print_pip_cmd("pip", "install", str(assets.wheel))
    return 0


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
        hint = _GH_INSTALL_HINTS.get(platform.system(), "see https://cli.github.com")
        print(f"          fix: {hint}")

    print("-" * 44)
    if any_backend:
        print("at least one verification backend is usable")
        return 0
    print("NO verification backend is usable - pywheels will refuse to verify any wheel")
    print("install sigstore or gh (see above) before relying on this tool")
    return 1


def main(argv=None) -> None:
    # stdout is block-buffered (not line-buffered) whenever it isn't a real
    # terminal - which is exactly the case under CI - while stderr is
    # always unbuffered. Left alone, that means every stderr line (our
    # archive_check_error diagnostics, verification failure messages, etc)
    # gets flushed immediately while stdout's lines sit queued until the
    # process exits - so in a captured CI log, the "why it failed" lines
    # appear to jump to the top, ahead of the OK/FAILED context they're
    # explaining, instead of appearing where they were actually printed.
    # Force line-buffering so stdout and stderr interleave in real order.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "verify":
        sys.exit(_cmd_verify(args))
    elif args.command == "install":
        sys.exit(_cmd_install(args))
    elif args.command == "doctor":
        sys.exit(_cmd_doctor(args))


if __name__ == "__main__":
    main()