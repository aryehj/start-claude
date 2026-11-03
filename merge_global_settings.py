#!/usr/bin/env python3
"""Merge the repo's Claude Code preferences into a global ~/.claude/settings.json.

Shared by start-claude.sh and start-agent.sh, which run it on the macOS host:

    python3 merge_global_settings.py SETTINGS_PATH TEMPLATE_PATH

A missing SETTINGS_PATH starts from a copy of TEMPLATE_PATH; either way the
same rules are applied. The file is rewritten only when something changed, and
each change is reported as one "==> ..." line on stdout. Invalid settings JSON
or a missing template exits non-zero and leaves SETTINGS_PATH untouched.

Each managed key gets one treatment, kept in its own section below:

- forced:  overwritten every run
- merged:  entries added if missing, never removed
- seeded:  written only if absent

Stdlib-only, and must stay parseable by Python 3.9 (macOS system python3).
tests/test_global_settings_helper.py pins the interface.
"""
import copy
import json
import os
import sys

# ── forced: overwritten every run ────────────────────────────────────────────
FORCED = {
    "showThinkingSummaries": True,
    "coauthorTag": "none",
}

# ── removed: deleted whenever present ────────────────────────────────────────
# effortLevel is kept unpinned on purpose (ADR-017).
REMOVED = ("effortLevel",)

# ── promoted: replaced only when unset or set to a listed value ──────────────
# theme "auto" misreads Ghostty's background (ADR-040); explicit choices stay.
PROMOTED = {
    "theme": ((None, "auto"), "dark-ansi"),
}

# ── seeded: copied from the template only when absent ────────────────────────
SEEDED_FROM_TEMPLATE = ("permissions",)


def _apply_forced(data):
    messages = []
    for key, value in FORCED.items():
        if key not in data or data[key] != value or type(data[key]) is not type(value):
            data[key] = copy.deepcopy(value)
            messages.append("Set {} to {}".format(key, json.dumps(value)))
    return messages


def _apply_removed(data):
    messages = []
    for key in REMOVED:
        if key in data:
            del data[key]
            messages.append("Removed {}".format(key))
    return messages


def _apply_promoted(data):
    messages = []
    for key, (from_values, to_value) in PROMOTED.items():
        if data.get(key) in from_values:
            data[key] = to_value
            messages.append("Set {} to {}".format(key, json.dumps(to_value)))
    return messages


def _apply_merged(data, template):
    # No merged keys yet; the push/publish deny-list union lands here.
    return []


def _apply_seeded(data, template):
    messages = []
    for key in SEEDED_FROM_TEMPLATE:
        if key not in data and key in template:
            data[key] = copy.deepcopy(template[key])
            messages.append("Seeded {} from template".format(key))
    return messages


def apply_rules(data, template):
    """Apply every rule to ``data`` in place; return the change messages.

    ``template`` is never mutated, and anything copied from it is a deep copy.
    An empty list means ``data`` already conformed.
    """
    messages = []
    messages += _apply_forced(data)
    messages += _apply_removed(data)
    messages += _apply_promoted(data)
    # Seed before merging, so an absent block is seeded whole first.
    messages += _apply_seeded(data, template)
    messages += _apply_merged(data, template)
    return messages


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def merge_file(settings_path, template_path):
    """Merge into ``settings_path``; return the change messages.

    Raises (writing nothing) when the template is missing or the settings file
    is not valid JSON.
    """
    settings_path = os.fspath(settings_path)
    template = _load_json(template_path)
    if not isinstance(template, dict):
        raise ValueError("template {} is not a JSON object".format(template_path))

    messages = []
    if os.path.exists(settings_path):
        data = _load_json(settings_path)
        if not isinstance(data, dict):
            raise ValueError("{} is not a JSON object".format(settings_path))
    else:
        data = copy.deepcopy(template)
        messages.append("Created {} from template".format(settings_path))

    messages += ["{} in {}".format(m, settings_path) for m in apply_rules(data, template)]
    if messages:
        # Serialize before opening, so a failure can't truncate the file. Written
        # in place (not renamed over) so a file bind mount keeps its inode.
        text = json.dumps(data, indent=2) + "\n"
        with open(settings_path, "w") as f:
            f.write(text)
    return messages


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: merge_global_settings.py SETTINGS_PATH TEMPLATE_PATH\n")
        return 2
    settings_path, template_path = argv
    try:
        messages = merge_file(settings_path, template_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("merge_global_settings.py: {}\n".format(exc))
        return 1
    for message in messages:
        print("==> " + message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
