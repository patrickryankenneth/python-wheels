"""Registry selection, version validation and the policy table. Like the
verify tests, these guard behavior that is easy to loosen by accident."""

import sys

import pytest
from packaging import tags as _tags
from packaging.specifiers import SpecifierSet

from pywheels import cli, registry
from pywheels.policies import WORKFLOW_ACTION_PINS, checks_for
from pywheels.registry import Asset, NoCompatibleWheel, Registry, RegistryError

HERE = str(next(_tags.sys_tags()))          # a tag this interpreter supports
NOPE = "py3-none-plat_that_does_not_exist"


def _asset(version="2.0.5", tag="dbt-oss-v2.0.5", wheel_tag=HERE, policy="2026-09-sha-pinned-v1", revision=0):
    return Asset(
        builds_repo=registry.CANONICAL_REPO, tag=tag, revision=revision,
        filename=f"dbt_oss-{version}-{tag}.whl", name="dbt-oss", version=version, wheel_tag=wheel_tag,
        sha256="0" * 64, requires_python="", signer_workflow=".github/workflows/w.yml",
        policy_version=policy, predicate_types=(), upstream_repo="https://github.com/o/dbt-oss",
        upstream_tag=f"v{version}", upstream_commit="a" * 40,
    )


def test_sha_pinned_label_enables_the_pin_check():
    assert WORKFLOW_ACTION_PINS in checks_for("2026-09-sha-pinned-v1")
    assert checks_for("2026-09-something-else") == frozenset()


def test_invalid_version_fails_loudly_instead_of_sorting_as_zero():
    entry = {"name": "dbt-oss", "version": "2.0.5.post1.post1", "wheel_tag": HERE, "sha256": "x",
             "signer_workflow": "w", "provenance": {"policy_version": "p"},
             "upstream": {"repo": "r", "tag": "t", "commit": "c"}}
    with pytest.raises(RegistryError):
        registry._asset_from_entry(registry.CANONICAL_REPO, "t", 0, "f.whl", entry)


def test_newest_version_wins_only_if_it_runs_here():
    old, new_elsewhere = _asset("2.0.4"), _asset("2.0.5", wheel_tag=NOPE)
    sel = Registry(2, "u", assets=[old, new_elsewhere]).select("dbt-oss")
    assert sel.chosen is old


def test_newer_post_release_with_other_platforms_only_does_not_hide_this_platform():
    base = _asset(tag="dbt-oss-v2.0.5")
    post = _asset(tag="dbt-oss-v2.0.5.post1", wheel_tag=NOPE)
    sel = Registry(2, "u", assets=[base, post]).select("dbt-oss", SpecifierSet("==2.0.5"))
    assert sel.chosen is base


def test_same_platform_prefers_higher_policy_then_revision():
    a = _asset(tag="t1", policy="2026-01-x", revision=5)
    b = _asset(tag="t2", policy="2026-09-x", revision=0)
    c = _asset(tag="t3", policy="2026-09-x", revision=2)
    assert Registry(2, "u", assets=[a, b, c]).select("dbt-oss").chosen is c


def test_nothing_compatible_raises_with_the_available_list():
    with pytest.raises(NoCompatibleWheel) as exc:
        Registry(2, "u", assets=[_asset(wheel_tag=NOPE)]).select("dbt-oss")
    assert len(exc.value.available) == 1


def test_naming_a_wheel_skips_the_platform_check():
    a = _asset(wheel_tag=NOPE)
    assert Registry(2, "u", assets=[a]).select("dbt-oss", filename=a.filename).chosen is a


def test_policy_command_states_the_unhashed_policy_caveat(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_registry", lambda: Registry(2, "u", assets=[_asset()]))
    assert cli._cmd_policy(None) == 0
    out = capsys.readouterr().out
    assert "2026-09-sha-pinned-v1" in out
    assert "not hashed" in out