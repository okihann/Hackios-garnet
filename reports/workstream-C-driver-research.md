# Workstream C — Driver / Compatibility Research (Garnet)

**Base stack:** HakiOS (`hakios/`). Reference device: POCO X5 Pro (`redwood`, SC7280).
**Target:** Redmi Note 13 Pro 5G / POCO X6 5G `garnet` (SoC **SM7435** "parrot", Adreno 710).
**Evidence source:** the phone's own firmware device tree `device-tree.dtb` (root `/tmp/garnet-fdt.dts`), plus `hakios/` code. No hardware addresses guessed — every number below is from the vendor tree.

---

## 0. Blocking finding (affects every workstream)

The uncommitted changes to `hakios/xnuloader/loader.c` (and `mkxnuimg.py`, `qemu/run.sh`) replaced
about 22 unrelated hex constants with `0xB7000000`. That looks like a global find-and-replace gone wrong.
Saved for review: `reports/evidence/uncommitted-loader-mkxnuimg.diff`.

Constants it overwrote include:

| Constant | Was (HEAD) | Now |
|---|---|---|
| `KERNEL_LIVE_MAGIC` / `MAGIC2` | `"XNULIVE!"` / `"XNULIVE2"` | `0xB7000000` |
| `HC_PA` (UFS host controller) | `0x1d84000` | `0xB7000000` |
| `BLOCK_MAGIC`, `LOADER_BATTERY_MAGIC` | `"RDWBLOCK"`, `"RDWBATT!"` | `0xB7000000` |
| EFI status codes (`INVALID_PARAMETER`, `ACCESS_DENIED`, `VOLUME_CORRUPTED`) | `0x8000…02/0F/0A` | `0xB7000000` |
| FNV hash prime and offset | `0x100000001b3`, `0xcbf29ce484222325` | `0xB7000000` |
| `PAYLOAD_MAX_SIZE`, the 1 MiB chunk size, `FW_CFG_BASE`, `unique-chip-id` | various | `0xB7000000` |

The built `loader.efi` (11-03 19:33) does not contain the `XNULIVE!` magic, but the kernel
(`hakios-kernel`, `run-payload.bin`) does. A loader that can't find that structure prints bare
`2 s`, `4 s`, … and nothing else (`note_kernel_progress` with `live == NULL`). That matches every
garnet log in `hakios/logs/`, so the "silent hang" may be the loader failing to read the kernel's log,
not the kernel failing. The logs (19:02 and earlier) predate this build, so I can't prove which
loader produced them. A clean rebuild settles it.

Also in the same diff, and to be checked in Workstream B rather than reverted blindly:
`KERNEL_VIRT_BASE` (`…F004000000` → `…F000000000`), `KERNEL_LINK_OFFSET`, `BASE_ALIGN`
(32 MB → 2 MB, while HEAD's comment says the 32 MB is the L2 block size with 16K pages), `PAGE_SIZE_MAX`,
`RESIDENT_RD_*`, `ba->virtBase/physBase`, and `allocate_pages(AllocateAnyPages)` replacing `AllocateAddress`.
The last one can return a block other than the one `choose_window` measured.

---

## 1. How drivers load in the HakiOS boot chain

Boot path (confirmed in `xnuloader/loader.c`):

```
fastboot boot <boot.img>
  → BootShim copies SILICIUM_UEFI.fd → FD_BASE (0xA7100000), payload → PAYLOAD_BASE (0xB7000000)
  → Mu-Silicium UEFI runs, supplies GOP (framebuffer) + memory map
  → xnuloader/loader.efi runs (== XNULOADER run ==)
  → parses "XNUP" payload: entries `kernel`, `ramdisk`, `cmdline`, `kernel.ver`
  → choose_window(): picks largest EfiConventionalMemory block, allocate_pages() claims it
  → loads mach-o segments at pa = va + slide   (log: "kernel loaded at pa 8d0004000..8d11c0000")
  → builds a synthetic XNU device tree + boot_args (cpus, gic, ramdisk, video, identity)
  → psci CPU_ON for each core, jumps to entry (log: "jumping to 8d04194a8")
```

Drivers are **built into the kernel** (no kext loading in this chain). Selection is config-driven:
`make garnet_defconfig` → `#include common_defconfig + sm7435.config + garnet.config`.
Each `CONFIG_HAKIOS_<DRIVER>` compiles one file under `drivers/<subsystem>/`; a `conf` file maps it
to `bsd/conf/files` lines, and `tools/hakios-config.py` generates them. Devices are wired into the
**synthetic DT** the loader writes, not probed from the vendor DT — so every address the kernel
needs is baked either into the loader's `struct platform` or into the driver's own constants.

Boot args are the runtime switch (`devices/.../configs/*.config` → `tools/hakios-config.py args`).

## 2. Log-capture channel (the reason HakiOS is the right base)

Kernel output is written to a shared page the loader reads **while the kernel is running** and after
it returns. The kernel exposes a `kernel_live` struct (magic `"XNULIVE!"`, heartbeat, pointers to a
head+ring log). The loader finds it by scanning the loaded image for that magic, then every 2 s for
the first `NOTE_SECONDS` (24 s) it dumps the tail into the phone's log partition; `fastboot oem log`
(`./xl log`, `./xl run` → `logs/*.live`) returns it. If it can't find the magic, each tick is a bare
`2 s`, `4 s`, … — **exactly what the garnet logs show**, and consistent with the clobbered
`KERNEL_LIVE_MAGIC` in §0. So the channel exists and needs no UART; the current silence is probably
the loader side, not a dead kernel. (UART `serial=3` is only added when `PLAT->uart != 0`; garnet's
Geni-SE UART at `0x98c000` isn't wired and isn't a PL011 anyway.)

## 3. Hardware inventory vs. available drivers

| Subsystem | Garnet hardware (from DTB) | Redwood driver | Fits? |
|---|---|---|---|
| Console/serial | Geni-SE UART `0x98c000` (`qcom,msm-geni-console`) | loader text/fb console | no UART in XNU; use fb/log |
| Interrupt | `arm,gic-v3` `17200000/17260000` | platform_garnet already `17200000/17260000` | **matches** |
| Storage | `qcom,ufshc` `1d84000`, QMP UFS phy v4 `1d80000` | `hakios.ufs.c`, `hakios.ufshc.c` | needs reg/clock port (UFS verified enumerating) |
| GPU | `qcom,adreno-gpu-gen7-3-0`, model **Adreno710v1**, `3d00000` | `hakios.adreno.c` (Adreno 642L = gen6) | **different gen — no ready driver** |
| Display | Novatek NT36672E video-mode DSI + DSC; SDE panel | UEFI GOP framebuffer path | first-boot = GOP only |
| Touch | `xiaomi-touch` + `goodix,n16-9916r-spi`,`focaltech,n16-3683g-spi` on SPI (`irq-gpio 0x147:0x62`) | `hakios.goodix.c` (Goodix/Focaltech over SPI) | plausible — same vendor pair |
| Wi-Fi | `qcom,icnss` membase `22800000`, WPSS region `0x85300000` (radio model **not named** in the tree; the node also lists `qcom,wcn3990`) | `hakios.lx`/`hakios.wlfw` ath11k shim (redwood = WCN6750 via WPSS) | same WPSS/icnss style; chip model unconfirmed, read it from Android (`/vendor/firmware`) |
| PMIC | PM8350B (gpio, swr-haptics), PMK8350 (pwrkey, resin, rtc), PM6150L (wled, flash), PMR735A, PM6450, WL2866D | `hakios.pmic.c`,`hakios.buttons.c` | PON/pwrkey on PMK8350 — needs remap |
| Audio | WSA883x amps, WCD938x codec, LPASS | `hakios.adsp/audio/sound` | big, defer |
| Cellular | MPSS mem `8bc00000`, IPA base `3e40000`, GSI `3e04000` (IPA 4.11-ish addresses differ) | `hakios.mpss/ipa/qmi/nas` | addresses in soc.args, defer |
| Camera | qcom cam-* stack (cam-isp, icp, ipe680) | `hakios.camera.c` | defer |
| Haptic | `qcom,pm8350b-swr-haptics` / `qcom,hv-haptics` | `hakios.haptic.c` (AW86927) | different controller |
| Fingerprint | `goodix` SPI (`fingerprint_goodix_*` pinctrl) | none | defer |

## 4. LinuxKPI / driver-compat shims present in the ecosystem

- **Wi-Fi:** `wifi/lx/` is a real LinuxKPI-style shim — `lx_base.c` (threads, work, memory, DMA pool),
  `lx_bus.c` (bus/ring/IRQ), `lx_80211.c` (mac80211-ish frames), `lx_wpa.c` (WPA2-PSK). Drives **ath11k**;
  the audit in `docs/driver-review/wifi-stack.md` flags security gaps (W1–W11) but it *works* on redwood's
  WCN6750. Garnet's `qcom,icnss` + WCN6750 means the same ath11k firmware path *should* apply — the best
  driver-reuse story on the board. `tools/mk-ath11k.sh` + `mk-wlfw.sh` fetch the driver+firmware.
- **Touch:** `ShengNovatekTouch`/`ShengHidInput`/`QcomGeniSpi` exist in the **UniXNU** tree (not hakios).
  Hakios has `hakios.goodix.c` for the Goodix/Focaltech-SPI combo — matches garnet's pair better.
- **Display:** no DSI-panel driver is needed for first boot; the GOP framebuffer is enough.
  UniXNU's `GopFramebuffer`/Novatek work is relevant later for the native panel.
- **UFS:** UniXNU `ShengUfs`/`community-native-ufs-*` exist; hakios `hakios.ufs.c` is the nearer match and
  already enumerates garnet's 127 partitions + 6 units (per the log).

## 5. Risk assessment for a first boot

| # | Risk | Blocking? | Note |
|---|---|---|---|
| R0 | **Corrupted loader constants** (§0) | **yes** | Restore the clobbered constants, rebuild `loader.efi`, rerun. Do this before any kernel work. |
| R1 | Live log unreadable when the kernel hangs | was thought blocking | Likely a symptom of R0. The loader does read the kernel's log during a run when the magic is right. A UART is not needed. |
| R2 | SoC addresses differ from redwood (SM7435 "parrot" vs SC7280 "yupik"); any driver constant copied from redwood will fault | medium | audit `soc.args` and each driver's registers against the garnet DTB (Workstream B) |
| R3 | Memory window (~`0x8c0000000` per the kernel load address) and the uncommitted `virtBase/physBase`/`BASE_ALIGN` changes | high | verify in Workstream B against the UEFI memory map |
| R4 | GPU is Adreno 710 (gen7) — no open driver today; only GOP | not for first boot | accept framebuffer-only, Metal later |
| R5 | PMIC PON/pwrkey + resin on PMK8350 — wrong button/power addressing can wedge reboot paths | low for first boot | map before enabling CONFIG_HAKIOS_BUTTONS |

**No driver gap blocks a first boot to verbose console.** Blockers are diagnostic (R1) and correctness
(R2/R3) — both in the kernel/loader patch domain (Workstream B), not missing-driver domain.

## 6. Open items needing the board's real numbers (do not guess)

- Native panel bring-up (NT36672E + DSC + SDE) — needs the panel timing block, out of scope for first boot.
- Confirm `dram-base/dram-size` XNU is told matches usable RAM (compare UEFI `get_memory_map` to the
  DTB `memory` node's many banks).
- Confirm `boot-epoch`/panic-log base (`PANIC_LOG_BASE`) doesn't collide with a reserved region.

*All addresses above are read from `device-tree.dtb`; none are assumed.*
