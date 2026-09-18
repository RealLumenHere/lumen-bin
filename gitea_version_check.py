#!/usr/bin/env python3
"""Check installed Gitea version (on Anubis) against latest upstream release.

Queries the Gitea API on Anubis for the running version, fetches the latest
stable release from the GitHub API, and alerts via the self-hosted ntfy
if the installed version is older than the latest release.

Fail-open: any error fetching remote data results in no alert (advisory check).
"""

import json
import os
import sys
import urllib.request

# --- Configuration -----------------------------------------------------------
# Gitea runs on Anubis, not on this host (Lumen) -- default to its tailnet IP.
GITEA_URL = os.environ.get("GITEA_URL", "http://100.121.17.5:3000")
GITEA_API = f"{GITEA_URL}/api/v1/version"
GITHUB_RELEASES_API = "https://api.github.com/repos/go-gitea/gitea/releases/latest"
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:8080/lumen-alerts")

# --- Helpers -----------------------------------------------------------------

def fetch_json(url: str, timeout: int = 15) -> dict:
    """Fetch JSON from a URL, raising on HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "lumen-gitea-version-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def send_ntfy(title: str, message: str) -> None:
    """Send a notification to the self-hosted ntfy endpoint."""
    payload = json.dumps({"topic": "lumen-alerts", "title": title, "message": message}).encode()
    req = urllib.request.Request(
        NTFY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                print(f"ntfy returned HTTP {resp.status}", file=sys.stderr)
    except Exception as exc:  # fail-open: never crash the check on notify failure
        print(f"ntfy send failed: {exc}", file=sys.stderr)


def parse_version(version: str) -> tuple:
    """Parse a version string like '1.27.3' into a comparable tuple."""
    version = version.lstrip("v")
    parts = []
    for part in version.split(".")[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)



def main() -> int:
    # 1. Get installed version from Gitea API (on Anubis)
    try:
        installed_data = fetch_json(GITEA_API)
        installed_version = installed_data.get("version", "")
    except Exception as exc:
        print(f"Could not fetch installed Gitea version from {GITEA_API}: {exc}")
        return 0

    if not installed_version:
        print("Gitea API returned no version field")
        return 0

    # 2. Get latest release from GitHub
    try:
        release_data = fetch_json(GITHUB_RELEASES_API)
        latest_version = release_data.get("tag_name", "")
    except Exception as exc:
        print(f"Could not fetch latest Gitea release: {exc}")
        return 0

    if not latest_version:
        print("GitHub API returned no tag_name")
        return 0

    # 3. Compare versions
    installed_tuple = parse_version(installed_version)
    latest_tuple = parse_version(latest_version)

    print(f"Installed Gitea (anubis): {installed_version} (tuple {installed_tuple})")
    print(f"Latest Gitea (upstream):  {latest_version} (tuple {latest_tuple})")

    if installed_tuple >= latest_tuple:
        print("Gitea is up to date.")
        return 0

    # 4. Alert via ntfy
    message = (
        f"Gitea on Anubis is out of date.\n"
        f"Installed: {installed_version}\n"
        f"Latest:    {latest_version}\n"
        f"Check github.com/go-gitea/gitea/security for advisories before upgrading."
    )
    send_ntfy("Gitea version outdated", message)
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
