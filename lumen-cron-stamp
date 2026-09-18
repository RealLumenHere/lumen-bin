#!/usr/bin/env bash
# lumen-cron-stamp — touch a dead-man's-switch stamp file for a systemd unit
# (wq#1184). Wire into a lumen-* service with:
#   ExecStartPost=-/home/lumen/bin/lumen-cron-stamp %n
# The leading `-` makes a stamp failure non-fatal to the unit itself.
set -euo pipefail
unit="${1:?usage: lumen-cron-stamp <unit-name>}"
name="${unit%.service}"
mkdir -p /home/lumen/logs
touch "/home/lumen/logs/.stamp.${name}"
