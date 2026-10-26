"""wq#4600: cmd-secret-lint, and lumen-pr refusing to publish a secret literal. Fake tokens are assembled at
runtime so this file is not itself a secret-scanner hit."""
import os, subprocess, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
LINT = str(ROOT / "cmd-secret-lint")
LUMEN_PR = str(ROOT / "lumen-pr")
GH = "ghp_" + "aB3xY9" * 6
CFAT = "cfat_" + "Qw7Er5" * 5
CFUT = "cfut_" + "Zx8Cv2" * 5
ANT = "sk-ant-" + "api03-Kj" * 4
SK = "sk-" + "proj9Ab" * 5
HEX64 = "0123456789abcdef" * 4
HEX40 = "0123456789abcdef0123" * 2
# hermetic throwaway repos: same LUMEN_PR_SCOPE_CHECK=0 convention as test_lumen_pr_dirty_guard.py
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "LUMEN_PR_SCOPE_CHECK": "0"}


def lint(text, *args):
    p = subprocess.run([sys.executable, LINT, *args], input=text, capture_output=True, text=True)
    return p.returncode, p.stderr


@pytest.mark.parametrize("secret,cls", [(GH, "github-token"), (CFAT, "cloudflare-account-token"), (CFUT, "cloudflare-user-token"),
                                        (ANT, "anthropic-key"), (SK, "api-key-sk"), ("AKIA" + "ABCDEFGH12345678", "aws-access-key")])
def test_prefixed_tokens_always_flagged(secret, cls):
    rc, err = lint("ssh lumen@host 'umask 077; printf %%s %s > /tmp/x'\n" % secret)
    assert rc == 1 and cls in err and secret not in err


def test_frank_pattern_hex_token_into_secrets_file():
    rc, err = lint("ssh lumen@100.117.66.102 'printf %%s %s > ~/.secrets/gitea.token'\n" % HEX40)
    assert rc == 1 and "hex-secret-in-credential-context" in err and HEX40 not in err


def test_hex_without_credential_context_is_left_alone():
    assert lint("git checkout %s\nsha256sum fw.bin # %s\n" % (HEX40, HEX64))[0] == 0


def test_bearer_url_password_pem_jwt():
    for t in ('curl -H "Authorization: Bearer %s" x' % ("Ab1Cd2Ef3Gh4" * 3),
              "git clone http://lumen:%s@host/r.git" % ("S3cretPw9" * 2),
              "-----BEGIN " + "OPENSSH PRIVATE KEY-----",   # assembled: a literal header trips the server-side secret-scan
              "T=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"):
        assert lint(t + "\n")[0] == 1, t


@pytest.mark.parametrize("t", ['curl -H "Authorization: Bearer $TOKEN" x', "printf %s <token> > f", "T=ghp_<your token>",
                               'X="${GITEA_TOKEN}"', "key=REDACTED", "tok=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                               "http://user:${PW}@host", "T=" + GH + "  # cmd-secret-lint: ok"])
def test_placeholders_and_marker_are_clean(t):
    assert lint(t + "\n")[0] == 0, t


def test_reports_line_and_label_without_the_value(tmp_path):
    f = tmp_path / "p.txt"; f.write_text("fine\nsecond %s\n" % GH)
    p = subprocess.run([sys.executable, LINT, "--name", "PR body", str(f)], capture_output=True, text=True)
    assert p.returncode == 1 and "PR body:2:" in p.stderr and GH not in p.stdout + p.stderr and "fp=" in p.stderr


def test_missing_file_is_exit_2():
    assert subprocess.run([sys.executable, LINT, "/no/such"], capture_output=True).returncode == 2


# ---- lumen-pr refuses to publish one ---------------------------------------------------------
BODY = "## What\nx\n## Why\nx\n## How\nx\n## Testing\nx\n## Risk / rollback\nx\n"


def _repo(tmp_path):
    r = tmp_path / "r"; r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], env=ENV, check=True)
    o = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(o)], env=ENV, check=True)
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin", str(o)], env=ENV, check=True)
    return r


def _open(tmp_path, title, body):
    r = _repo(tmp_path); bf = tmp_path / "body.md"; bf.write_text(body)
    p = subprocess.run([LUMEN_PR, "open", str(r), "--title", title, "--body-file", str(bf)], capture_output=True, text=True, env=ENV)
    return p.returncode, p.stdout + p.stderr


def test_lumen_pr_open_refuses_a_secret_in_the_body(tmp_path):
    rc, out = _open(tmp_path, "feat(x): add y", BODY + "token is %s\n" % GH)
    assert rc != 0 and "PR body carries a secret literal" in out and GH not in out


def test_lumen_pr_open_refuses_a_secret_in_the_title(tmp_path):
    rc, out = _open(tmp_path, "fix(x): use %s" % GH, BODY)
    assert rc != 0 and "PR title carries a secret literal" in out and GH not in out


def test_lumen_pr_open_clean_body_is_not_blocked_by_the_lint(tmp_path):
    rc, out = _open(tmp_path, "feat(x): add y", BODY)
    assert "secret literal" not in out


def test_lumen_pr_comment_refuses_a_secret(tmp_path):
    r = _repo(tmp_path); bf = tmp_path / "c.md"; bf.write_text("see %s\n" % CFAT)
    p = subprocess.run([LUMEN_PR, "comment", str(r), "7", "--body-file", str(bf)], capture_output=True, text=True, env=ENV)
    assert p.returncode != 0 and "secret literal" in p.stderr and CFAT not in p.stderr
