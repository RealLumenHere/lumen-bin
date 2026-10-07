#!/usr/bin/env python3
"""Phase 1.5: ingest device RE facts into lumen_state. Run on Lumen. Usage: ingest_device_re.py [--dry]"""
import subprocess, sys

DRY = "--dry" in sys.argv
LS = "/home/lumen/minerva/lumen_state.py"
VF = "2026-10-01"
NEWP = {"soc", "serial", "firmware", "dump_location"}
WHY = "Frank-specified device RE schema (soc/serial/firmware/dump_location/status), Phase 1.5 2026-10-01"
CM = "claude_memory"

F = [
 # (subject, predicate, value, source, confidence)
 ("turing-tvts", "soc", "HiSilicon Hi3516DV300 (dual Cortex-A7, NNIE). Thermal sensor Melexis MLX90640BAB 32x24 array, NOT single-point. 4 units", CM, None),
 ("turing-tvts", "firmware", "BusyBox 1.26.2, Linux 4.9.37, ONVIF HASX/HAV/v2.1.3. Board F01_16DV300_V1.2 FSAN_02 HuAn", "lumen_state turing-scanner.hardware", None),
 ("turing-tvts", "dump_location", "/home/lumen/turing-backup (372M: mmcblk0p1.bin, p1-p7.gz, inventory.txt, check-and-pull.sh). claude_memory lists project dir /home/lumen/projects/turing-scanner/ but it does not exist on Lumen as of 2026-10-01", "lumen disk 2026-10-01", 0.8),
 ("turing-tvts", "status", "Model TVTS-OP-BTTS7. Rooted via U-Boot Ctrl+C on cold boot (smart plug power cycle) then init=/bin/sh rw. UART J66 3.3V, Waveshare FT232RL with FTDI latency timer 1ms (Pico/CP2102 failed)", CM, None),

 ("echo-gen1", "soc", "TI DM3725 (OMAP3630/Cortex-A8) + TMS320C64x+ DSP. Model SK705DI. 5 units", CM, None),
 ("echo-gen1", "firmware", "Linux kernel 2.6.37 (Tailscale incompatible). Gen1 only, soldering required", CM, None),

 ("rk3288-face-terminal", "soc", "UNVERIFIED: RK3288 inferred from generic 8in Shenzhen terminal template + Android 5.1.1. RK3399 possible. Confirm via ro.board.platform on ADB recon", "chat 168723b8 (2026-10-01)", 0.4),
 ("rk3288-face-terminal", "firmware", "Android 5.1.1, app firmware 4.01.5X (from box/screens, unverified on-device)", "chat 168723b8 (2026-10-01)", 0.5),
 ("rk3288-face-terminal", "status", "8in face-recognition access terminal + clip-on forehead thermal module. Board vendor Fine Triumph (OUI of 301F9A80A57D). No ADB recon or dump yet. Plan: ADB over Ethernet, fallback read-only maskrom dump via rkdeveloptool (no flashing)", "chat 168723b8 (2026-10-01)", 0.6),

 ("miko2", "soc", "MediaTek MT8167. 2 units", CM, None),
 ("miko2", "serial", "Unit1 B020112AFB9010080D9 (Sep 2020 fw). Unit2 B917011AFB901003C24 (May 2019 fw)", CM, None),
 ("miko2", "firmware", "Android 7.0, kernel 4.4.22", "lumen_state miko2.hardware", None),
 ("miko2", "dump_location", "/home/lumen/re/miko/miko2/dump_miko2_20260907 (8.9G, Unit2 WiFi dump). ROM build on Thor ~/projects/miko2/rom_build/", "lumen disk 2026-10-01", None),
 ("miko2", "status", "Unit1 root via su 0, connected to Thor. Unit2 power defect, WiFi adb only (port 5555 baked into build.prop). mtkclient on MT8167: unplug USB, start mtkclient, then plug in and power on", CM, None),

 ("miko3", "soc", "Two boards: head MT8167, base GD32F303 MCU. WiFi/BT MT7668 SDIO on mmc2 (not internal consys)", CM, None),
 ("miko3", "serial", "miko3_unlocked MIKO1070XXM3E0414F0; miko3_nopin MIKO1280XXM3B02DA79; miko3_fifa MIKO3250XXM3Q06747A (3 recorded units; more units exist)", "lumen_state miko3_* subjects", None),
 ("miko3", "firmware", "Android 9", CM, None),
 ("miko3", "dump_location", "/home/lumen/re/miko/miko3/dump_* (eMMC dumps per unit, 108G total dir). GD32F303 dump: /home/lumen/re/miko/miko3/gd32/gd32_dump.bin", "lumen disk 2026-10-01", None),
 ("miko3", "status", "Inter-board UART 460800 8N1, 50+ commands, not a debug console. GD32F303 dump via custom OpenOCD cfg (CPUTAPID 0x2ba01477, cortex_m). ST-Link V2 3V3 must NOT connect to Miko board (brownout), only SWDIO/SWCLK/GND. Jadx work at re/miko/miko3/", CM, None),

 ("quest1", "soc", "Qualcomm Snapdragon 835, 4GB RAM", "web: roadtovr/uploadvr/cnx-software 2026-10-01", None),
 ("quest1", "firmware", "OTA package /home/lumen/re/quest-firmware/q1_49845030443200410.zip (filename-based id). Extracted partitions in re/quest-firmware/partitions", "lumen disk 2026-10-01", 0.8),
 ("quest2", "soc", "Qualcomm Snapdragon XR2, 6GB RAM", "web: roadtovr/uploadvr/cnx-software 2026-10-01", None),
 ("quest2", "firmware", "OTA package /home/lumen/re/quest-firmware/q2_50163300257300150.zip (filename-based id)", "lumen disk 2026-10-01", 0.8),

 ("winnovo-m8", "soc", "MediaTek MT8167 (ro.board.platform=mt8167), armeabi-v7a 32-bit only, 1GB RAM", "lumen disk re/winnovo-m8/getprop.txt", None),
 ("winnovo-m8", "serial", "VKM8US0000001525", "lumen disk re/winnovo-m8/getprop.txt", None),
 ("winnovo-m8", "firmware", "Winnovo_M8_V1.5, Android 10 (API 29), fingerprint Winnovo/M8/M8:10/QP1A.190711.020/1522785980:user/release-keys", "lumen disk re/winnovo-m8/getprop.txt", None),
 ("winnovo-m8", "dump_location", "/home/lumen/re/winnovo-m8/ (getprop.txt, partitions/hwparam.json, seccfg_unlocked.bin, Magisk.apk). No full image dump on Lumen", "lumen disk 2026-10-01", None),
 ("winnovo-m8", "status", "Debloated 166->123 pkgs (adups.fota removed), FUTO Keyboard + Chrome PWAs. Google Messages mirroring needs Android 13+ (not possible)", "lumen_state winnovo-m8.hardware", None),

 ("rk3399-scanner", "soc", "Rockchip RK3399 (Hugsun COVID-scanner kiosk), dual cameras, IR illuminators", "lumen_state rk3399-scanner.what", None),
 ("rk3399-scanner", "serial", "ADB serial 73MR5ZEL4H", "lumen_state rk3399-scanner.what", None),
 ("rk3399-scanner", "firmware", "Android 7.1.2. Kiosk launcher bitSHUTTLE (com.general.mediaplayer.player), stock Launcher3 underneath", "lumen_state rk3399-scanner.what", None),
 ("rk3399-scanner", "dump_location", "/home/lumen/re/rk3399-scanner/dump (9.4G: boot/system/recovery/kernel/resource/misc/etc imgs, rk3399_loader.bin, apks)", "lumen disk 2026-10-01", None),
 ("rk3399-scanner", "status", "Connected via Maksoor. Kiosk launcher disabled via ADB. See /areas/rockchip-re.md", "lumen_state rk3399-scanner.what", None),
]

ok = fail = 0
for subj, pred, val, src, conf in F:
    cmd = ["python3", LS, "ingest", subj, pred, val, "--valid-from", VF, "--source", src]
    if pred in NEWP:
        cmd += ["--new-predicate", WHY]
    if conf is not None:
        cmd += ["--confidence", str(conf)]
    if DRY:
        print("DRY", subj, pred, len(val)); continue
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip().splitlines()
    tag = "OK  " if r.returncode == 0 else "FAIL"
    ok += r.returncode == 0; fail += r.returncode != 0
    print(f"{tag} {subj}.{pred}: {out[-1][:140] if out else ''}")
print(f"done ok={ok} fail={fail}")
sys.exit(1 if fail else 0)
