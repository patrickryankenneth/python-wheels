# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-09-29

### Added
  
- **Registry schema 2.** Wheels are resolved by name, version and platform across
  *all* releases, using pip's own tag ranking, instead of by release tag. A newer
  `.postN` rebuild that only covers some platforms no longer hides the others, and
  "latest" means the newest version that has a wheel for this machine. Tags are
  labels and are never parsed. Schema 1 still works for `verify --tag`.
- `pywheels list` shows every attested wheel per version and platform.
- `pywheels policy` shows the policy labels in the registry and which of them pywheels
  independently checks. It does not restate the builder's promises, and it states that
  policy documents are not yet hashed into the attestations and that the current wheels
  were built before their policy was written down and frozen.
- A verification receipt after `verify`/`install`: what was checked, what was not, the
  trust boundary, Rekor entries, and a copy-paste recheck.
- Release-level signals, informational only and never able to change the verdict: the
  release's immutable flag, GitHub's release attestation (when `gh` supports it), tag
  signature, and source-commit signature.
- Two backends cross-check each other when both are installed (`sigstore` and `gh`).
  `--backend` selects one.
- The wheel is hashed as it downloads and must match the registry's SHA-256. Downloads
  are cached per release tag and written to `.part` files first.
- `install` verifies, installs exactly the verified file with `pip install --no-deps`,
  shows its `Requires-Dist`, and asks how to resolve dependencies (`--deps`,
  `--yes-deps`, `--constraint`, `--print-only`).
- `--no-source-archive` skips the source tarball only; the wheel's attestations are
  still fully verified. `--skip-workflow-audit` and `--skip-release-check` skip the
  small extra requests those checks make.
- `PYWHEELS_REGISTRY_URL` points at a different registry for testing (a warning is
  printed, and the registry must still name the canonical builds repo).

### Changed

- Adds one runtime dependency, `packaging>=22.0`, for version, requirement and platform-tag handling.
- The pinned-workflow check is now looked up through one table (`policies.py`) instead of
  string matching on the policy label in several places.
- A registry entry whose version is not valid PEP 440 is now a hard error naming the
  entry. Previously it sorted as version 0 and was silently never selected.

### Security notes

- Known limitations are listed in [SECURITY.md](https://github.com/patrickryankenneth/python-wheels/blob/main/SECURITY.md), including that the signer
  is pinned by repository name rather than immutable repository ID.

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

[0.2.0]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.2.0
[0.1.2]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.2
[0.1.1]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.1
[0.1.0]: https://github.com/patrickryankenneth/python-wheels/releases/tag/v0.1.0