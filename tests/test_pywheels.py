"""VerifyReport.ok is the actual trust decision pywheels exists to make -
these tests exist to catch a future edit that quietly loosens it, not to
exercise sigstore/gh (those need real subprocesses and are out of scope
for a fast unit-test layer)."""

from pathlib import Path

from pywheels.verify import AttestationResult, BUILD_PROVENANCE_PREDICATE, VerifyReport

UPSTREAM_PREDICATE = "https://example.com/attestations/upstream-source/v1"


def _report(*attestations, archive_ok=None):
    report = VerifyReport(wheel=Path("dummy.whl"))
    report.attestations = list(attestations)
    if archive_ok is not None:
        report.archive_checked = True
        report.archive_ok = archive_ok
    return report


def test_ok_when_both_required_attestations_verify():
    report = _report(
        AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=True, detail=""),
        AttestationResult(UPSTREAM_PREDICATE, {}, verified=True, detail=""),
    )
    assert report.ok


def test_not_ok_missing_build_provenance():
    report = _report(
        AttestationResult(UPSTREAM_PREDICATE, {}, verified=True, detail=""),
    )
    assert not report.ok


def test_not_ok_missing_upstream_source():
    report = _report(
        AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=True, detail=""),
    )
    assert not report.ok


def test_not_ok_when_an_attestation_failed_verification():
    report = _report(
        AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=False, detail="signature mismatch"),
        AttestationResult(UPSTREAM_PREDICATE, {}, verified=True, detail=""),
    )
    assert not report.ok


def test_ignored_attestation_never_counts_even_if_marked_verified():
    # Defensive: an ignored attestation must not be able to stand in for
    # either required check, even if something upstream mistakenly set
    # verified=True on it.
    report = _report(
        AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=True, detail=""),
        AttestationResult(UPSTREAM_PREDICATE, {}, verified=True, detail="", ignored=True),
    )
    assert not report.ok


def test_failed_archive_digest_blocks_ok_even_with_valid_attestations():
    report = _report(
        AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=True, detail=""),
        AttestationResult(UPSTREAM_PREDICATE, {}, verified=True, detail=""),
        archive_ok=False,
    )
    assert not report.ok


def test_archive_not_checked_does_not_block_ok():
    report = _report(
        AttestationResult(BUILD_PROVENANCE_PREDICATE, {}, verified=True, detail=""),
        AttestationResult(UPSTREAM_PREDICATE, {}, verified=True, detail=""),
    )
    assert report.archive_checked is False
    assert report.ok