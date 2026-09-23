# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.0