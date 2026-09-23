# python-wheels

**Status: working - v0.1.0.** The `python-wheels` CLI (command: `pywheels`,
also installed as `python-wheels`) verifies and installs attested wheels
today. First attested build shipped: `dbt-oss` for `win_arm64`.

## The problem

Some Python packages don't ship a wheel for your platform: an
uncommon architecture, Alpine/musl instead of glibc, an OS upstream doesn't
target, or just a combination nobody's gotten around to building for. Right
now the options are "build it from source yourself, every time" or "hope
someone in the community hosts one somewhere."

It's also not always upstream being lazy. PyPI caps a project at 10GB of
total storage, and wheels for every platform/arch/Python-version
combination add up fast - that's part of why projects like PyTorch host
some of their wheels off-PyPI and point people at an extra index instead.
`python-wheels` is aimed at exactly that situation: popular packages that
can't or don't publish a wheel for your platform, for any reason.

## What it does

```bash
pip install python-wheels
pywheels install dbt-oss==2.0.5 \
  --repo patrickryankenneth/python-wheels-builds \
  --tag dbt-oss-v2.0.5 \
  --workflow build-dbt-oss-win-arm64.yml
```

1. **Checks upstream first.** If pip can already resolve real wheels for
   the package - and its whole dependency closure, not just the top-level
   package - on your platform, it says so and prints the plain
   `pip install` command; this tool gets out of the way whenever upstream
   already has you covered. A small per-package override list
   (`overrides.json`) handles the rare case where a package's real wheel
   availability is hidden from pip's normal resolution - e.g. `dbt-oss`,
   whose PyPI sdist is a stub build backend that fetches a real prebuilt
   wheel from GitHub, keyed by platform, with no source to fall back to.
2. **Falls back, but verifies.** If there's no usable upstream wheel, it
   fetches the matching release from
   [python-wheels-builds](https://github.com/patrickryankenneth/python-wheels-builds)
   and checks, before recommending anything:
   - its **build provenance attestation** (SLSA - built by the expected CI
     workflow, from the expected repo and ref, unmodified since),
   - its **upstream-source attestation** (built from the real, tagged
     upstream release - not a fork or a patched copy),
   - the **source archive's digest** against what that attestation
     recorded, when a source archive is available.

   Verification runs offline via [sigstore](https://pypi.org/project/sigstore/)
   (`pip install python-wheels[sigstore]`) if it's installed, or falls back
   to the `gh` CLI (no pip dependency, needs network) if it isn't. Run
   `pywheels doctor` to see which backend is usable on your machine.
3. **Fails loudly, not silently.** `pywheels install` never runs
   `pip install` for you - it only ever prints the exact command that's
   safe to run. If verification fails, or neither backend is available at
   all, it says so plainly and falls back to `pip install
   --no-binary=:all:` (unverified, from source) as the last resort - never
   a silent, unattested install.

`pywheels verify` runs the same verification directly against a wheel
that's already on disk, or a specific release tag - useful for checking a
build without going through the whole `install` flow.

## Where the wheels actually come from

This repo is only the installer and verifier. The wheels themselves are
built and attested in
[python-wheels-builds](https://github.com/patrickryankenneth/python-wheels-builds),
and also served as a plain [PEP 503](https://peps.python.org/pep-0503/)
index at [python-wheels.github.io](https://python-wheels.github.io) for
anyone who just wants `pip install --extra-index-url
https://python-wheels.github.io/simple/ <package>` without the
verification step. `pywheels` is the piece that ties the CLI, the
release repo, and the attestation checks together so you don't have to run
`gh attestation verify` by hand.

## Longer-term direction

The build side is currently one hand-tuned workflow for one package
(`dbt-oss`/`dbt-core` on Windows ARM64). The goal is to turn that into a
standardized, reproducible build recipe that can target other popular PyPI
packages missing wheels for a given platform - starting with the platforms
that come up most often (Alpine/musl, less-common architectures, newer
Python versions upstream hasn't built for yet), rather than trying to
cover everything at once.

## Not yet decided

- CLI surface beyond `install`/`verify`/`doctor` (list available
  platforms? show why a package fell back? a way to request a new
  package/platform combo?)
- How package/platform requests get prioritized once this covers more than
  one package
- Whether to eventually provide a wrapper mode that invokes pip directly after verification, 
  versus keeping the strict "print the safe command only" separation.

Contributions and issues on any of the above are welcome - this project is
still early, even with a first attested build shipped.