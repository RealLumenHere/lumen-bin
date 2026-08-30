#!/usr/bin/env python3
# eplug-fetch.py - fetch E-series (Athom/ESPHome) plug state from HA on Bes.
# E-plugs are HA-only by design: segregated from the Tapo P-series / plug-ctl
# KLAP path so the flaky Athom units never touch the stable tooling. Emits a
# JSON list of plug dicts matching plug-ctl shape, plus a series=E marker.
# Registry: display-name -> HA slug root (entity IDs still read pN_* from onboarding).
# Fails soft: on any HA error, returns [] so the P-series snapshot still publishes.
import json
import sys
import urllib.request

HA = "http://192.168.8.177:8123"
TOKEN_FILE = "/home/lumen/.secrets/bes_ha_token"
EPLUGS = {"E1": "p12"}   # E1 = ex-P12 Athom, powers gameboy-pc (battery-backed)
EDEVICE = {"E1": "gameboy-pc"}  # display labels; canonical = lumen_state power_plug facts


def ha(path):
    tok = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(HA + path, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def one(name, slug):
    sw = ha("/api/states/switch." + slug + "_switch")
    pw = ha("/api/states/sensor." + slug + "_power")
    on = str(sw.get("state") == "on")
    try:
        watts = round(float(pw.get("state")), 1)
    except (TypeError, ValueError):
        watts = 0.0
    return {"name": name, "device": EDEVICE.get(name, "?"), "ip": "HA",
            "state": on, "watts": watts, "series": "E"}


def main():
    out = []
    for name, slug in EPLUGS.items():
        try:
            out.append(one(name, slug))
        except Exception:
            continue
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
