"""`instincts-render` — wq#958 (found by the R1 instincts efficacy audit).

claude_context.sh --core injected `head -c 4096` of instincts.yaml. The file is 23,064 bytes:
the cut delivered the first 22 of 57 rules and dropped 35, including the FULL AUTONOMY grant and
THOR IS THE ONLY HARD RULE, while the superseded reboot-gate rule sat inside the cut and was
served as live law. The cut landed mid-rule, so the injected text was not parseable YAML.

These tests pin the two properties a byte cap cannot have: rules leave whole, and an omission is
stated in the output instead of being silent. Same subprocess-against-a-scratch-file pattern as
test_wq_reap.py.
"""
import os
import subprocess

import pytest
import yaml

# wq#886 pattern: overridable so a pre-commit gate can test the STAGED copy, not the deployed one
RENDER_BIN = os.environ.get("INSTINCTS_RENDER_BIN", "/home/lumen/bin/instincts-render")

LIVE_FILE = "/home/lumen/instincts.yaml"

EXIT_EMPTY = 3
EXIT_UNUSABLE = 4


def write(tmp_path, body, name="instincts.yaml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def render(path, *args):
    return subprocess.run([RENDER_BIN, "--file", path] + list(args),
                          capture_output=True, text=True)


def rules_of(stdout):
    return yaml.safe_load(stdout)["rules"]


def file_of(*rules):
    return "rules:\n" + "".join(f'  - "{rule}"\n' for rule in rules)


def test_every_rule_is_delivered_by_default(tmp_path):
    path = write(tmp_path, file_of(*[f"rule {n}" for n in range(1, 41)]))
    done = render(path)
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == [f"rule {n}" for n in range(1, 41)]
    assert "40 of 40" in done.stdout
    assert "OMITTED" not in done.stdout


def test_output_parses_and_round_trips_hostile_text(tmp_path):
    """The 4KB cut emitted corrupt YAML. Quoting is the renderer's job, not the file's."""
    hostile = ['says "no": always', "back\\slash and # hash", "colon: in: text", "- dash start",
               "trailing space ", "unicode — em dash, 90%", "{brace}, [bracket], *star, &amp"]
    path = write(tmp_path, yaml.safe_dump({"rules": hostile}, allow_unicode=True))
    done = render(path)
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == hostile


def test_budget_cuts_on_a_rule_boundary_and_says_so(tmp_path):
    """A byte cap would land mid-rule. A budget must drop the whole rule and report it."""
    rules = [f"rule {n} " + "x" * 96 for n in range(1, 11)]
    path = write(tmp_path, file_of(*rules))
    done = render(path, "--budget-tokens", "75")
    assert done.returncode == 0, done.stderr
    kept = rules_of(done.stdout)
    assert 0 < len(kept) < 10
    assert kept == rules[:len(kept)]
    assert f"{len(kept)} of 10" in done.stdout
    assert "OMITTED" in done.stdout and "token budget" in done.stdout
    assert LIVE_FILE not in done.stdout  # the pointer names the file it read


def test_tier_marker_comments_order_the_output(tmp_path):
    """Position matters, so a hard constraint written last must still be injected first."""
    path = write(tmp_path, "rules:\n"
                           '  - "doctrine one"\n'
                           "  # tier: 2\n"
                           '  - "domain one"\n'
                           "  # tier: 0\n"
                           '  - "THOR IS THE ONLY HARD RULE"\n')
    done = render(path)
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == ["doctrine one", "THOR IS THE ONLY HARD RULE", "domain one"]
    assert "# --- tier 0 — hard constraints ---" in done.stdout


def test_max_tier_drops_and_reports(tmp_path):
    path = write(tmp_path, "rules:\n"
                           "  # tier: 0\n"
                           '  - "hard"\n'
                           "  # tier: 2\n"
                           '  - "domain a"\n'
                           '  - "domain b"\n')
    done = render(path, "--max-tier", "0")
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == ["hard"]
    assert "OMITTED" in done.stdout and "2 above the requested tier" in done.stdout


def test_per_rule_tier_metadata_beats_the_marker(tmp_path):
    """wq#959 moves tiers into the rules themselves; the renderer already reads that form."""
    path = write(tmp_path, "rules:\n"
                           "  # tier: 2\n"
                           '  - text: "carries its own tier"\n'
                           "    tier: 0\n"
                           "    added: 2026-08-20\n"
                           '  - "inherits the marker"\n')
    done = render(path, "--max-tier", "0")
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == ["carries its own tier"]


def test_unclassified_rules_are_never_dropped_by_tier(tmp_path):
    """Unclassified means 'not known to be droppable' — it must not be read as low priority."""
    path = write(tmp_path, file_of("no marker anywhere"))
    done = render(path, "--max-tier", "0")
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == ["no marker anywhere"]


def test_a_dash_inside_a_rule_does_not_shift_the_tiers_after_it(tmp_path):
    path = write(tmp_path, "rules:\n"
                           "  # tier: 0\n"
                           "  - |\n"
                           "    a block rule\n"
                           "    - with a dash line\n"
                           "  # tier: 2\n"
                           '  - "domain"\n')
    done = render(path, "--max-tier", "0")
    assert done.returncode == 0, done.stderr
    assert rules_of(done.stdout) == ["a block rule\n- with a dash line\n"]


@pytest.mark.parametrize("body,expected", [
    ('rules: ["flow style", "hides the items"]\n', 2),
    ('rules:\n  # tier: 2\n  - "a rule wrapped\n  - onto two lines"\n', 1),
])
def test_a_scan_the_parser_disagrees_with_delivers_everything(tmp_path, body, expected):
    """When the line scan cannot see the same rules the parser did, guessing which rule a marker
    covers would misfile it — so no rule is classified, and none is dropped."""
    done = render(write(tmp_path, body), "--max-tier", "0")
    assert done.returncode == 0, done.stderr
    assert len(rules_of(done.stdout)) == expected
    assert "OMITTED" not in done.stdout


@pytest.mark.parametrize("body", ["", "rules: []\n", "rules:\n"])
def test_nothing_to_render_exits_3_with_no_stdout(tmp_path, body):
    done = render(write(tmp_path, body))
    assert done.returncode == EXIT_EMPTY
    assert done.stdout == ""


def test_missing_file_exits_3_with_no_stdout(tmp_path):
    done = render(str(tmp_path / "absent.yaml"))
    assert done.returncode == EXIT_EMPTY
    assert done.stdout == ""


@pytest.mark.parametrize("body", ['rules:\n  - "unterminated\n', "not: a rules file\n",
                                  "rules:\n  - 17\n"])
def test_unusable_file_exits_4_with_no_stdout(tmp_path, body):
    """Half a file is what the byte cap shipped. Emit none of it instead."""
    done = render(write(tmp_path, body))
    assert done.returncode == EXIT_UNUSABLE
    assert done.stdout == ""


@pytest.mark.skipif(not os.path.exists(LIVE_FILE), reason="live instincts.yaml not on this host")
def test_live_file_delivers_every_rule_including_the_ones_the_cut_dropped():
    done = render(LIVE_FILE)
    assert done.returncode == 0, done.stderr
    kept = rules_of(done.stdout)
    assert kept == yaml.safe_load(open(LIVE_FILE, encoding="utf-8"))["rules"]
    assert any("THOR IS THE ONLY HARD RULE" in rule for rule in kept)
    assert any("FULL AUTONOMY" in rule for rule in kept)
