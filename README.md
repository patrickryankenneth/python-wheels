# python-wheels-builds

Reproducible, attested builds of upstream Python packages for platforms the
upstream project doesn't publish official wheels for — starting with
[`dbt-oss`](https://github.com/dbt-labs/dbt-oss) / `dbt-core` on
**Windows ARM64**.

This repo doesn't fork or modify the packages it builds. It checks out an
exact upstream git tag, builds it unmodified, and cryptographically attests
both *what was built* and *where it came from*, so anyone downloading a
wheel from here can verify it matches upstream — without having to trust
this repo blindly.

## Why this exists

Some popular Python packages with native (Rust/C) extensions don't ship
Windows ARM64 wheels yet, even though the upstream source builds fine there.
This project cross-builds those wheels on real `windows-11-arm` runners and
publishes them, with enough provenance attached that the build can be
verified rather than trusted.

This is the first package (`dbt-oss` / `dbt-core`); the workflow is written
to generalize to others.

## How a build works

The [`build-dbt-oss-win-arm64`](.github/workflows/build-dbt-oss-win-arm64.yml)
workflow, on manual dispatch:

1. **Resolves** the requested git tag in `dbt-labs/dbt-oss` to an exact commit
   SHA via `git ls-remote` (no floating tags).
2. **Checks out** that exact commit and verifies the working tree is clean
   and `HEAD` matches the resolved SHA — no drift, no local patches.
3. **Builds** the requested wheel(s) with `maturin build --release`, one at a
   time, into isolated `dist/<name>/` folders. For the `dbt-oss` name, the
   `name` field in `pyproject.toml` is temporarily renamed and restored
   immediately after — mirroring upstream's own `release-v2.yml` "Rename
   pyproject for oss wheel" step, since `dbt-oss` and `dbt-core` are the same
   codebase under two package names during their naming transition.
4. **Re-verifies** afterward that no tracked source file was modified and
   `HEAD` still matches the resolved SHA, then records the upstream repo,
   tag, and commit in `upstream-source.json`.
5. **Attests**, per wheel:
   - **Build provenance** ([`actions/attest-build-provenance`](https://github.com/actions/attest-build-provenance)) — proves the wheel was built by *this* workflow, on *this* commit of *this* repo, not tampered with afterward.
   - **Upstream source** (`actions/attest`, custom predicate type) — proves the wheel corresponds to the exact upstream repo/tag/commit recorded in `upstream-source.json`.

   Each wheel gets its own single-subject attestation rather than one shared
   attestation covering both, so a consumer verifying one wheel never has to
   reason about another wheel's provenance.
6. **Publishes** a GitHub Release with the wheel(s) attached, after the
   release job independently re-verifies every attestation bundle with
   `gh attestation verify` and sanity-checks that each wheel's bundle is
   distinct.

## Verifying a wheel yourself

You don't have to trust this repo — verify it:

```bash
gh attestation verify dbt_oss-<version>-<platform>.whl --repo patrickryankenneth/python-wheels-builds
gh attestation verify dbt_oss-<version>-<platform>.whl --repo patrickryankenneth/python-wheels-builds \
  --predicate-type https://patrickryankenneth.github.io/attestations/upstream-source/v1
```

The first confirms the wheel was built by this repo's workflow, unmodified
since. The second confirms which upstream commit it was built from. Both are
backed by Sigstore-signed, GitHub-hosted attestations — not by anything this
repo self-asserts in a README.

## The bigger picture

This repo is one piece of a small trust chain:

| Repo | Role |
|---|---|
| **python-wheels-builds** (this repo) | Builds wheels, attests build provenance + upstream source, publishes GitHub Releases. |
| **python-wheels.github.io** | Hosts a [PEP 503](https://peps.python.org/pep-0503/) simple index pointing at the released wheels, so they're `pip`-installable via `--extra-index-url`. |
| **python-wheels** (CLI, planned) | A thin installer — e.g. `python-wheels install dbt-oss` — that resolves the right wheel for your platform, runs the same `gh attestation verify` checks automatically before installing, and fails closed if verification doesn't pass. |

The goal of the CLI is that a user never has to run the `gh attestation
verify` commands above by hand, or implicitly trust that this GitHub account
hasn't been compromised — the tool checks it for them, every install,
before any code from the wheel runs. The same attestation pattern is meant
to extend to other packages beyond `dbt-oss`, using this repo's workflow as
the template.

## Status

Early — one package, manual `workflow_dispatch` builds, no automated
tracking of new upstream releases yet. Treat wheels published here as
independently verifiable, not as an official or endorsed distribution of
`dbt-oss`/`dbt-core`.

## Security

If you find an issue with the build process, the attestation setup, or a
mismatch between a published wheel and its claimed upstream source, please
open an issue in this repo.
