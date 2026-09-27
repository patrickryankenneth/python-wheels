# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [0.1.2] - 2026-09-26

### Added

- `verify`/`install` no longer take `--repo`, and `--workflow` is now
  optional on both. The canonical builds repo is fixed in code
  (`registry.py`'s `CANONICAL_REPO`), and the workflow file that actually
  built a given release is looked up automatically from `registry.json` -
  a small static file served off python-wheels.github.io, keyed by
  release tag, fetched with a plain HTTPS GET (not the GitHub REST API,
  so it isn't subject to that API's unauthenticated rate limit). Passing
  `--workflow` yourself is now only needed to test a workflow that isn't
  registered yet (e.g. together with `--local-dir`).
- `install` resolves an unpinned package to the most recently registered
  release providing it ("latest attested build"), and a pinned version
  (`dbt-core==2.0.5`) to the specific tag covering that version - even
  when the tag's own name doesn't match the package name (e.g. `dbt-core`
  riding along in a `dbt-oss-v2.0.5` tag), via each tag's `provides` list
  in the registry.

### Fixed

- Restored the non-affiliation disclaimer in `pywheels -h`'s top-level
  `description`/`epilog` and the `cli.py` module docstring. This had
  silently regressed after 0.1.1 - the 0.1.1 changelog entry below claims
  it shipped, but the argparse `description=`/`epilog=` kwargs never
  actually made it into that release due to an earlier rebase; `pywheels
  -h` printed no disclaimer at all until now.

## [0.1.1] - 2026-09-26

### Changed

- Added an explicit non-affiliation disclaimer - `python-wheels` is an
  independent, unofficial project, not affiliated with, endorsed by, or
  sponsored by PyPA, PyPI, or the Python Software Foundation. Added to
  README.md, the `pywheels`/`pywheels.cli` module docstrings, the
  top-level `pywheels -h` output (`description`/`epilog`), and the PyPI
  summary (`description` in `pyproject.toml`).

## [0.1.0] - 2026-09-23

Initial release.

`pywheels` verifies that a wheel from [python-wheels-builds](https://github.com/patrickryankenneth/python-wheels-builds)
was actually built by that repo's own GitHub Actions workflow before you
install it - not just downloaded from somewhere plausible. It checks two
required attestations (SLSA build provenance, plus our own
upstream-source attestation) against the exact signer identity, and, when
a source archive is available, that the archive's digest matches what the
attestation recorded.

### Added

- `pywheels verify` - verify a wheel's attestations directly, either
  fetched from a GitHub release (`--tag`) or already sitting on disk
  (`--local-dir`, e.g. output of `gh run download`).
- `pywheels install` - the main workflow: check whether pip can already
  resolve real wheels for a package on the current platform; if not,
  fetch and verify our attested build instead; print the exact
  `pip install` command that's safe to run, or fall back to an
  unverified source build as a last resort. Never runs `pip install`
  itself.
- `pywheels doctor` - reports which verification backend(s) are usable
  and how to fix the ones that aren't.
- Two independent verification backends: `sigstore` (offline, needs the
  optional `sigstore` extra) and the `gh` CLI (needs network, no pip
  dependency). Falls back from the former to the latter automatically;
  refuses to trust a wheel if neither is available.
- Per-package build-detection overrides (`overrides.json`) for packages
  whose real wheel availability isn't visible to plain `pip download`
  probing - e.g. `dbt-oss`, whose PyPI sdist is a stub build backend that
  fetches a real prebuilt wheel from GitHub, keyed by platform.
- Initial attested build: `dbt-oss` for `win_arm64`, filling a real gap
  upstream doesn't cover on that platform yet.

[0.1.2]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.2
[0.1.1]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.1
[0.1.0]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.0