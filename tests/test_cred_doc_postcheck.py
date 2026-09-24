"""wq#1619: cred-doc-postcheck fails while the rotator is still at the keyboard."""
import os
import subprocess
import sys

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "cred-doc-postcheck")


def run(*args):
    p = subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def doc(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_clean_scan_passes(tmp_path):
    doc(tmp_path, "a.md", "read the token from /home/lumen/.secrets/github-frank.token\n")
    rc, out = run("--old", "/home/lumen/agent/.env", "--scan", str(tmp_path))
    assert rc == 0 and "OK" in out


def test_stale_reference_fails_with_file_and_line(tmp_path):
    f = doc(tmp_path, "a.md", "intro\nread GITHUB_TOKEN from /home/lumen/agent/.env\n")
    rc, out = run("--old", "GITHUB_TOKEN", "--scan", str(tmp_path))
    assert rc == 1
    assert f"{f}:2:" in out and "STALE REFERENCE" in out


def test_near_only_counts_lines_that_also_carry_it(tmp_path):
    doc(tmp_path, "a.md", "GITHUB_TOKEN is set by the workflow\n")  # unrelated line
    assert run("--old", "GITHUB_TOKEN", "--near", "agent/.env", "--scan", str(tmp_path))[0] == 0
    doc(tmp_path, "b.md", "GITHUB_TOKEN in agent/.env\n")
    assert run("--old", "GITHUB_TOKEN", "--near", "agent/.env", "--scan", str(tmp_path))[0] == 1


def test_marker_documents_an_intentional_absence(tmp_path):
    doc(tmp_path, "a.md", "There is no GITHUB_TOKEN in agent/.env <!-- cred-postcheck:ok -->\n")
    assert run("--old", "GITHUB_TOKEN", "--scan", str(tmp_path))[0] == 0


def test_backups_archives_approvals_and_drafts_are_skipped(tmp_path):
    for name in ("x.md.bak.20260101", "claude_memory_archive.md", "approvals/x.md", "drafts/y.md"):
        doc(tmp_path, name, "the old /home/lumen/agent/.env path\n")
    assert run("--old", "/home/lumen/agent/.env", "--scan", str(tmp_path))[0] == 0


def test_binary_file_is_ignored(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00 GITHUB_TOKEN \xff")
    assert run("--old", "GITHUB_TOKEN", "--scan", str(tmp_path))[0] == 0


def test_output_shows_context_around_the_match_not_the_whole_line(tmp_path):
    doc(tmp_path, "a.md", "A" * 300 + " GITHUB_TOKEN " + "B" * 300 + "\n")
    rc, out = run("--old", "GITHUB_TOKEN", "--scan", str(tmp_path))
    assert rc == 1 and "A" * 200 not in out and "B" * 200 not in out


def test_missing_scan_path_is_a_usage_error(tmp_path):
    rc, out = run("--old", "x", "--scan", str(tmp_path / "nope"))
    assert rc == 2 and "does not exist" in out


def test_old_is_required():
    assert run()[0] == 2
