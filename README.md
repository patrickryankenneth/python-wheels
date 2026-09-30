# python-wheels

**Status: working - v0.2.0.** The `python-wheels` CLI (command: `pywheels`,
also installed as `python-wheels`) verifies and installs attested wheels
today. Run `pywheels list` to see which wheels are available.

> **Disclaimer:** `python-wheels` is an independent, unofficial project. It
> is **not affiliated with, endorsed by, or sponsored by** the Python
> Packaging Authority (PyPA), the Python Software Foundation, or PyPI.
> "PyPI," "Python," and the Python logo are trademarks of the PSF; this
> project is a third-party tool that builds on top of the public PyPI
> index, `pip`, and standard packaging specs (PEP 427, PEP 503) - it does
> not host, mirror, or modify PyPI itself, and the `python-wheels-builds`
> and `python-wheels.github.io` repos referenced below are this project's
> own infrastructure, not official Python infrastructure.

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
pywheels install dbt-oss==2.0.5
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
   - resolving which release and which workflow actually built it
   automatically, via a small registry served off
   [python-wheels.github.io](https://python-wheels.github.io) - and
   checks, before recommending anything:
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
3. **Installs only what it verified.** `pywheels install` installs exactly the
   verified wheel file (`pip install --no-deps <wheel>`), then shows you its
   dependencies and asks how to resolve them - those are never verified. If
   verification fails, or neither backend is available, it refuses to install
   and only *prints* the unverified `pip install --no-binary=:all:` command as
   your remaining option; it never runs it for you. Use `--print-only` to see
   every command without running any.

`pywheels verify` runs the verification without installing, and prints a
receipt of what was checked, what was not, and how to re-check it yourself.

| command | what it does |
| --- | --- |
| `pywheels verify <pkg>` | verify the attested wheel for this machine |
| `pywheels install <pkg>` | verify, then install exactly that wheel |
| `pywheels list [pkg]` | every attested wheel, per version and platform |
| `pywheels policy` | policy labels in the registry, and which parts pywheels checks |
| `pywheels doctor` | which verification backends work here |

## What is and isn't verified

Verified: the exact wheel you install - its build provenance and that it was
built from an unmodified upstream commit. **Not** verified: its dependencies,
and the safety of the upstream code. See [SECURITY.md](SECURITY.md) for the
full trust model and the known limitations.

**Policies.** Each wheel carries a policy label naming the promises the builds
repo makes about how it was built. pywheels does not restate those promises;
`pywheels policy` shows the labels and which of them it independently checks
(today: that every action in the signing workflow is pinned to a full commit
SHA). As of v0.2.0 policy documents are not yet hashed into the attestations,
and the current wheels were built before their policy was written down and
frozen, so their label is a name only. Wheels built after a policy is
published are intended to carry its hash.

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

- CLI surface beyond `install`/`verify`/`list`/`policy`/`doctor` (`--json`
  output? a way to request a new package/platform combo?)
- How package/platform requests get prioritized once this covers more than
  one package
- Policy documents bound into the attestations by hash, and pinning the
  signer by immutable repository ID.

Contributions and issues on any of the above are welcome - this project is
still early, even with a first attested build shipped.