#!/usr/bin/env python3
"""wq#2902 — keyed .env reader + secret-masking boundary for credential-holding scripts in bin.

Why: litellm-budget-alert / litellm-provision-keys read litellm-master.env as a ONE-line
file (`read_text().split("=",1)[1]`). When wq#2627 appended GEMINI_API_KEY the master key
value became "<master>\nGEMINI_API_KEY=<gemini>", urllib raised "Invalid header value ..."
carrying both secrets, and `f"...{e}"` printed + ntfy'd them nine times. Two classes:
  (a) a k=v file consumed positionally instead of by key name  -> read_env / env_value
  (b) exception text that carries request headers, emitted unmasked -> mask()
Import from any script in /home/lumen/bin (sys.path[0] is the script dir):
    from lumen_env import env_value, mask
"""
import os, subprocess

MASK_SECRETS = "/home/lumen/bin/mask-secrets"   # wq#2316 boundary (sed, lockstep with foreman masking.py)


def read_env(path):
    """Parse a KEY=VALUE file by key name. Ignores blank/comment lines, strips one layer of quotes.
    Later duplicates win (same as docker env_file / systemd EnvironmentFile)."""
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def env_value(path, key, required=True):
    """One value by key. Raises KeyError (naming the KEY, never a value) when required and absent."""
    v = read_env(path).get(key)
    if v is None or v == "":
        if required:
            raise KeyError(f"{key} not set in {path}")
        return None
    return v


def mask(text):
    """Route text through the wq#2316 mask-secrets boundary before print/ntfy/aq.
    Fail-closed: if the masker is missing or fails, the text is suppressed rather than emitted raw."""
    text = str(text)
    if not text:
        return text
    try:
        r = subprocess.run([MASK_SECRETS], input=text, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.rstrip("\n")
    except Exception:
        pass
    return f"<{len(text)} chars suppressed: {MASK_SECRETS} unavailable>"


if __name__ == "__main__":
    import sys
    # smoke: `lumen_env.py <file> <KEY>` prints sha12 of the value, never the value
    import hashlib
    v = env_value(sys.argv[1], sys.argv[2])
    print(f"{sys.argv[2]} present sha12={hashlib.sha256(v.encode()).hexdigest()[:12]} len={len(v)}")
