# Security Policy

`pywheels` exists to answer one question before you install something: *was
this exact wheel built by the workflow it claims, from the upstream source
it claims?* Anything that makes that answer wrong is a security bug.

## Reporting a vulnerability

Please report privately rather than opening a public issue.

- Preferred: a private [GitHub Security Advisory](https://github.com/patrickryankenneth/python-wheels/security/advisories/new)
- Alternative: email patrickryankenneth@gmail.com with "SECURITY" in the subject

Include what you found, its impact, steps to reproduce (or a minimal
example), and the affected version(s). If the problem is in a *wheel* or in
the build pipeline rather than in this CLI, report it on
[python-wheels-builds](https://github.com/patrickryankenneth/python-wheels-builds/security/advisories/new)
instead. If you're unsure which, send it here and I'll route it.

This is a small project maintained in spare time, so there is no formal SLA.
Security reports take priority over everything else, and I'll acknowledge
receipt as soon as I can. I'd like to fix issues before they're discussed
publicly and will credit reporters who want credit.

## Supported versions

Only the latest release on PyPI receives fixes. Please upgrade
(`pip install --upgrade python-wheels`) before reporting, and say which
version you tested.

## What counts as a vulnerability

Highest priority - anything that lets a wheel be reported as verified when it
shouldn't be:

- a way to make `verify` or `install` exit 0 for a wheel that is unattested,
  tampered with, or signed by anything other than the pinned workflow in the
  canonical builds repo
- a way to make a registry or a release asset override the trust decision (the
  registry is meant to be a hint: a lying registry may cause a failure, never a pass)
- confusion between the wheel that was verified and the file that gets installed
  (the installed file must be the verified file)
- failures that turn into passes: a missing backend, a malformed bundle, an empty
  attestation list, a timeout

Also in scope: command or argument injection through registry fields, tags or
filenames; path traversal when downloading or caching; and anything that makes
the receipt claim a check that was not performed.

## What pywheels verifies, and what it does not

Verified, for the exact wheel you install:

- the wheel's SHA-256 is covered by signed, transparency-logged attestations
  (SLSA build provenance, plus the upstream-source attestation)
- the signer identity is pinned to the canonical builds repo and the workflow
  the registry names, and the provenance names the same repo and workflow
- the upstream repo, tag and commit in the attestation match the registry
- when the source archive is downloaded, it matches the digest in the attestation
- when the registry's policy label ends in a pinning policy that pywheels knows,
  every action reference in the signing workflow at the build commit is a full
  commit SHA

Not verified:

- **the wheel's dependencies**, which pip or uv resolve at install time
- **the safety of the upstream code.** "Built from unmodified upstream" is not
  "upstream is safe"
- **the builder's promises beyond the checks above.** A wheel's policy label is
  defined by the builds repo. `pywheels policy` lists which of them pywheels
  independently checks; everything else is a builder assertion
- **SBOM files inside a wheel.** They come from upstream, are found by file name
  only, and are not checked
- **the build container image and anything the build downloads**

Trusted: the owner of the builds repo, GitHub (Actions, releases) and Sigstore
(Fulcio, Rekor). A compromise of any of them is outside what this tool can detect.

## Known limitations

These are known and planned, not undisclosed:

- **Policy text is not bound to wheels yet.** As of 0.2.0, policy documents are
  not hashed into the attestations, and existing wheels were built before their
  policy was written down and frozen. Their policy label is a name only.
  Wheels built after the policy is published are intended to carry its hash.
- **The signer is pinned by repository name, not by immutable repository ID.**
  The attestation certificates carry the IDs; pywheels does not check them yet, so
  deleting and re-creating the repository name is a theoretical takeover path.
- **Release, tag and commit signature signals are informational.** They report what
  GitHub says, never change the verdict, and no signer key is pinned. A valid
  signature means "a key GitHub knows for that account", not "the maintainer's key".
- **The wheel is always downloaded.** There is no check that avoids fetching it.
- **pywheels itself is not yet attested.** Verify what you install from PyPI through
  PyPI's own mechanisms until that changes.

## Out of scope

Reports about the upstream projects being packaged, about `sigstore` or the `gh`
CLI (report those to their maintainers), about a wheel's dependencies, or about
denial of service by a slow network.

## Safe harbor

Good-faith research within this policy - testing against your own machine and the
public registry, without disrupting others or accessing anyone's data - is welcome,
and I won't pursue action over it.