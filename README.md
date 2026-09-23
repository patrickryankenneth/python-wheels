# python-wheels

**Status: planned, not yet built.** This repo will hold the `python-wheels`
CLI. Nothing here works yet — this README is the plan.

## The problem

Some Python packages don't ship a wheel for your platform: an
uncommon architecture, Alpine/musl instead of glibc, an OS upstream doesn't
target, or just a combination nobody's gotten around to building for. Right
now the options are "build it from source yourself, every time" or "hope
someone in the community hosts one somewhere."

It's also not always upstream being lazy. PyPI caps a project at 10GB of
total storage, and wheels for every platform/arch/Python-version
combination add up fast — that's part of why projects like PyTorch host
some of their wheels off-PyPI and point people at an extra index instead.
`python-wheels` is aimed at exactly that situation: popular packages that
can't or don't publish a wheel for your platform, for any reason.

## What it will do

```bash
python-wheels install some-package
```

1. **Check upstream first.** If `some-package` already has a compatible
   wheel on PyPI for your platform, just install that — this tool should
   get out of the way whenever upstream already has you covered.
2. **Fall back, but verify.** If there's no upstream wheel, look for one
   built by this project (via the `--extra-index-url` at
   [python-wheels.github.io](https://python-wheels.github.io)) and, before
   installing anything, verify:
   - the wheel's build provenance attestation (it was built by the
     expected CI workflow, unmodified since),
   - its upstream-source attestation (it was built from the real, tagged
     upstream commit — not a fork or a patched copy).

   See [python-wheels-builds](https://github.com/patrickryankenneth/python-wheels-builds)
   for how those attestations are generated. If either check fails, the
   install is refused rather than silently falling back to source.
3. **Fail loudly, not silently.** No unverified wheel, no unattested
   source, no quiet fallback to "just trust it."

## Where the wheels actually come from

This repo is only the installer. The wheels themselves are built and
attested in [python-wheels-builds](https://github.com/patrickryankenneth/python-wheels-builds),
and served from the index at
[python-wheels.github.io](https://python-wheels.github.io). `python-wheels`
is the piece that ties those two together and does the verification so you
don't have to run `gh attestation verify` by hand.

## Longer-term direction

The build side is currently one hand-tuned workflow for one package
(`dbt-oss`/`dbt-core` on Windows ARM64). The goal is to turn that into a
standardized, reproducible build recipe that can target other
popular PyPI packages missing wheels for a given platform — starting with
the platforms that come up most often (Alpine/musl, less-common
architectures, newer Python versions upstream hasn't built for yet), rather
than trying to cover everything at once.

## Not yet decided

- Exact CLI surface beyond `install` (list available platforms? show why a
  package fell back? a way to request a new package/platform combo?)
- How package/platform requests get prioritized once this covers more than
  one package
- Packaging and release process for the CLI itself

Contributions and issues on any of the above are welcome even at this
early stage — this README will get replaced by real docs once there's
something to install.
