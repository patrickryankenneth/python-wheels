"""What THIS pywheels enforces for a wheel's policy label - and nothing else.

A wheel's `policy_version` is a label the builds repo attaches to its own
promises. Those promises are documented (and frozen per version) by the
builds repo; this module deliberately does NOT restate them. It answers only
one question: which of them can this pywheels independently check?

Anything not listed here is "label only": pywheels compares the label to the
registry and the attestation, but cannot confirm what it stands for. Add a
check here in the same release that adds the code performing it, so this
table never claims more than the code does.
"""

from __future__ import annotations

# Checks pywheels can perform, by name.
WORKFLOW_ACTION_PINS = "workflow-action-pins"

_CHECK_DESCRIPTIONS = {
    WORKFLOW_ACTION_PINS: "every action ref in the signing workflow, at the build commit, is a full commit SHA",
}


def checks_for(policy_version: str) -> frozenset:
    """The checks this pywheels performs for `policy_version`."""
    found = set()
    if "sha-pinned" in policy_version:
        found.add(WORKFLOW_ACTION_PINS)
    return frozenset(found)


def describe(check: str) -> str:
    return _CHECK_DESCRIPTIONS.get(check, check)


# Shown by `pywheels policy`. Remove or reword when policy documents are
# hashed into the attestations (see CHANGELOG) - it describes today's gap.
POLICY_NOTICE = (
    "As of this release, policy documents are not hashed into the attestations, and the wheels in the\n"
    "registry were built before their policy was written down and frozen. Their policy label is a name\n"
    "only: pywheels cannot confirm that any document describes it."
)