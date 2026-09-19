#!/usr/bin/env bash
# overnight_boot_gate.sh — S4 pre-reboot safety gate (v2). Run by hand (or by whoever is
# about to reboot a node) before a planned reboot: checks dpkg isn't mid-operation, the
# current kernel has a matching initramfs, /boot has >50MB free, fstab verifies, netplan
# regenerates cleanly, the bootloader (grub or systemd-boot) parses, ssh is enabled, and
# there's no active apt op or shutdown-blocking inhibitor. Prints PASS/FAIL per check and
# a final verdict; exits 1 on any failure. No systemd unit calls it — standalone manual gate.
# Usage: bash overnight_boot_gate.sh   (run ON the target node — checks its own /boot and /)
PW="${FLEET_PW:-$(cat /home/lumen/.fleet_pw 2>/dev/null)}"
s() { echo "$PW" | sudo -S "$@" 2>/dev/null; }
fail=0
echo "=== BOOT GATE on $(hostname) @ $(date '+%H:%M:%S %Z') ==="
echo "running_kernel=$(uname -r)"
AUD=$(s dpkg --audit 2>/dev/null)
if [ -z "$AUD" ]; then echo "a_dpkg_audit=PASS"; else echo "a_dpkg_audit=FAIL"; echo "$AUD"|head -3; fail=1; fi
lk=$(ls -1 /boot/vmlinuz-* 2>/dev/null | sort -V | tail -1 | sed 's#.*/vmlinuz-##')
if [ -n "$lk" ] && [ -f "/boot/initrd.img-$lk" ]; then echo "b_initramfs=PASS (boot-kernel $lk has initrd)"; else echo "b_initramfs=FAIL (kernel=$lk initrd missing)"; fail=1; fi
fm=$(df -Pm /boot 2>/dev/null | awk 'NR==2{print $4}'); [ -z "$fm" ] && fm=$(df -Pm / | awk 'NR==2{print $4}')
if [ "${fm:-0}" -gt 50 ]; then echo "c_boot_free=PASS (${fm}MB)"; else echo "c_boot_free=FAIL (${fm}MB)"; fail=1; fi
if s findmnt --verify >/dev/null 2>&1; then echo "d_fstab=PASS"; else echo "d_fstab=FAIL"; s findmnt --verify 2>&1 | grep -iE 'err|warn' | head -3; fail=1; fi
if ls /etc/netplan/*.yaml >/dev/null 2>&1; then
  if s netplan generate >/dev/null 2>&1; then echo "e_netplan=PASS"; else echo "e_netplan=FAIL"; s netplan generate 2>&1 | head -3; fail=1; fi
else echo "e_netplan=SKIP (no netplan)"; fi
if [ -f /boot/grub/grub.cfg ]; then
  if s grub-script-check /boot/grub/grub.cfg >/dev/null 2>&1; then echo "f_bootloader=PASS (grub.cfg ok)"; else echo "f_bootloader=FAIL (grub.cfg syntax)"; fail=1; fi
elif s bootctl is-installed >/dev/null 2>&1; then
  if s bootctl status >/dev/null 2>&1; then echo "f_bootloader=PASS (systemd-boot)"; else echo "f_bootloader=FAIL (bootctl)"; fail=1; fi
else echo "f_bootloader=WARN (no grub.cfg, no systemd-boot)"; fi
SSHE=no; systemctl is-enabled ssh >/dev/null 2>&1 && SSHE=ssh; systemctl is-enabled sshd >/dev/null 2>&1 && SSHE=sshd
if [ "$SSHE" != no ]; then echo "g_ssh_enabled=PASS ($SSHE)"; else echo "g_ssh_enabled=FAIL"; fail=1; fi
TSE=no; systemctl is-enabled tailscaled >/dev/null 2>&1 && TSE=yes; echo "g_tailscaled_enabled=$TSE"
echo "g_ipinfo=$(ip -brief -4 addr | grep -vE '127.0.0.1' | head -3 | tr '\n' ' ')"
# i. no ACTIVE package operation (persistent u-u --wait-for-signal helper is fine)
ACT=$(pgrep -af 'unattended-upgrade|/usr/bin/dpkg|apt-get|aptitude' 2>/dev/null | grep -vE 'wait-for-signal|pgrep|overnight_boot_gate')
LOCK=$(s fuser /var/lib/dpkg/lock-frontend 2>/dev/null)
if [ -z "$ACT" ] && [ -z "$LOCK" ]; then echo "i_apt_idle=PASS"; else echo "i_apt_idle=FAIL (active package op)"; echo "$ACT"|head -2; fail=1; fi
# j. no shutdown BLOCK inhibitor
BI=$(s systemd-inhibit --list 2>/dev/null | awk '/shutdown/ && /block/')
if [ -z "$BI" ]; then echo "j_no_block_inhibitor=PASS"; else echo "j_block_inhibitor=FAIL"; echo "$BI"|head -2; fail=1; fi
[ -f /var/run/reboot-required ] && echo "reboot_required=YES ($(cat /var/run/reboot-required.pkgs 2>/dev/null | tr '\n' ',' | head -c 120))" || echo "reboot_required=NO"
echo "=== GATE VERDICT: $([ $fail = 0 ] && echo ALL-PASS || echo HAS-FAILURES) ==="
exit $fail
