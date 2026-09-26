# Security Policy

## Reporting a vulnerability

If you find a security issue in `python-wheels` (the `pywheels` CLI) -
including but not limited to a way to bypass wheel/attestation
verification, a signer-identity check that can be spoofed, or a path
where an unverified or tampered wheel could be recommended for install -
please report it privately rather than opening a public issue.

- Preferred: open a private [GitHub Security Advisory](https://github.com/patrickryankenneth/python-wheels/security/advisories/new)
- Alternative: email patrickryankenneth@gmail.com with "SECURITY" in the subject

Please include a description of the issue and its impact, steps to
reproduce (or a minimal example), and the affected version(s).

## Response

This is a small, early-stage project maintained in spare time, so there's
no formal SLA - but security reports get priority over other issues, and
I'll acknowledge receipt as soon as I can.

## Scope

The core security property `python-wheels` provides is: it never recommends a
wheel as "verified" unless both the SLSA build-provenance attestation and
the upstream-source attestation check out against the expected repo,
workflow, and ref. Reports that undermine that guarantee are the highest
priority.

Issues in upstream dependencies (`sigstore`, the `gh` CLI) should be
reported to those projects directly, not here.