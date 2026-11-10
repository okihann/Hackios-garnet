# Porting HakiOS (iOS 16.7.16) to garnet

garnet is the Redmi Note 13 Pro 5G / POCO X6 5G: Qualcomm SM7435 ("parrot"), Adreno 710, 8 cores
(4× Cortex-A55 + 4× Cortex-A78). This file records how the port boots today, what was fixed to get
there, and the commands to run it.

**Nothing is ever flashed.** Every run uses `fastboot boot` (from RAM). The phone's storage is
only written by the loader's own log (a few KB in the `logdump` partition) and, only with
`ufs_write=1`, by the kernel into a `darwin` partition you create yourself. Reboot returns Android.

## Current state (2026-11-06, evening)

| Stage | State |
|---|---|
| Mu-Silicium UEFI → XNU loader | works (`devices/garnet/prebuilt/SILICIUM_UEFI.fd`, FD at `0xA7100000`) |
| Loader → kernel, live log back over `fastboot oem log` | works |
| Kernel init (VM, IOKit, BSD, cores) | works with `CONFIG_CPUS=4` |
| System image as root (`md0`) | works when uploaded with `./xl ramdisk` first |
| iPhone identity (`iPhone10,3`, `D22`) | works (`identity.h` built from the IPSW) |
| launchd and ~50 daemons | run |
| backboardd opens the framebuffer | works |
| Apple logo | shown; stalls there |
| **`darwin` partition on the UFS** | **created 2026-11-06**: 64 GiB off the end of `userdata`, f2fs shrunk in place, Android's data intact; `sda35` under Android; constants in `devices/garnet/darwin.toml` |
| Home screen | **not yet**: memory pressure (until the root moves to `darwin`) |

## Layout of the workspace

```
HackiOS-garnet/
  hakios/                     the HakiOS tree (kernel, loader, xl, userland)
    devices/garnet/           device repo: device.conf, configs/, soc/, prebuilt/, uefi.conf
    xnuloader/loader.c        the UEFI loader that starts XNU
    logs/                     every run's log (<HHMMSS>-run.log)
  Mu-Silicium/                UEFI source; Platforms/Xiaomi/garnetPkg
  device-tree.dtb             the phone's own Android device tree (source of every address)
  iPhone10,3,..._Restore.ipsw your own IPSW (identity and Darwin user space come from it)
  reports/                    this file, research report, diffs in evidence/
```

## Hardware facts used (all from `device-tree.dtb`)

| What | Value |
|---|---|
| GIC v3 distributor / redistributors | `0x17200000` / `0x17260000` (stride `0x20000`) |
| UFS host controller / PHY | `0x1d84000` / `0x1d80000` |
| Debug UART (GENI, not PL011) | `0x98c000` |
| Timer | `arm,armv8-timer`, 19.2 MHz |
| CPU MPIDRs | `0x000`..`0x700`, affinity field 1 (`core_shift = 8`); A55 = 0-3, A78 = 4-7 |
| Framebuffer (splash) | `0xB8000000`, 1220 × 2712 (UEFI GOP) |
| RAM bank the kernel uses | `0x8C0000000`, 3 GB (UEFI "RAM Entry 6") |

## Fixes made to get here

All in `hakios/xnuloader/loader.c` unless noted. Diffs: `reports/evidence/`.

1. **Clobbered constants.** An earlier bulk edit had replaced about 22 unrelated hex constants with
   `0xB7000000`. Restored from HEAD: `KERNEL_LIVE_MAGIC`/`MAGIC2` (`"XNULIVE!"`/`"XNULIVE2"`), `HC_PA`
   (`0x1d84000`), `BLOCK_MAGIC`, `LOADER_BATTERY_MAGIC`, EFI status codes, FNV hash constants,
   `FW_CFG_BASE`, `unique-chip-id`, `PAYLOAD_MAX_SIZE` (64 MiB) and the 1 MiB offsets.
   Symptom before: only `2 s`, `4 s` … after `jumping to`; the loader could not find the kernel's log.
2. **Identity.** `xnuloader/identity.h` had been generated as a placeholder (no IPSW). Regenerated with
   `xnuloader/mkidentity.sh` from `sysimage/DeviceTree.d22ap.im4p` (taken from your IPSW).
   Symptom before: `initproc failed to start … failed to query hardware target: 22`.
3. **memSize past the end of RAM.** `boot_args.physBase` is the kernel's load address, which is
   112 MB into the claimed window, but `memSize` was the whole window, so XNU believed RAM ran
   112 MB past `0x980000000`. Now `memSize = win_size - (kern_lo - phys_base)`.
   Symptom before: `Kernel data abort` (`esr 0x96000010`) in `memmove`/`libdeflate` during an APFS
   page-in from SpringBoard.
4. **MAX_MEM_SIZE.** Restored to `0x180000000` (it had become `0xB7000000`, capping the window at
   2,928 MB of the 3,072 MB block). **Built, not yet tested on the phone.**
5. **Cores.** `devices/garnet/soc/configs/sm7435.config`: `CONFIG_CPUS=4` (was 8). With 8, three
   cores (`0x300`, `0x200`, `0x100`) registered but never ran (`PC=0` in panic dumps). Temporary:
   return to 8 once secondary-core start-up is fixed.

Still uncommitted and unreviewed in the same diff: `KERNEL_VIRT_BASE`, `KERNEL_LINK_OFFSET`,
`BASE_ALIGN` (2 MB), `PAGE_SIZE_MAX`, `RESIDENT_RD_*`, `virtBase/physBase = vm_lo/kern_lo`,
`allocate_pages(AllocateAnyPages)`, and the garnet `struct platform` entry. These boot, but have
not been checked line by line.

## One-time setup

```sh
cd hakios
sysimage/unpack.sh ../iPhone10,3,iPhone10,6_16.7.16_20H392_Restore.ipsw   # Darwin files + DeviceTree.d22ap.im4p
xnuloader/mkidentity.sh                     # identity.h from the IPSW; must NOT say "placeholder"
xnuloader/build.sh                          # loader.efi
make garnet_defconfig                       # .config: common + sm7435 + garnet
```

Check the identity before every loader build: `head -1 xnuloader/identity.h` must read
`Generated by mkidentity.py from the device tree`, not `--placeholder`.

## Running a boot (on the Linux PC, phone plugged into it)

The phone must be in Android with USB debugging on, or in fastboot. `xl` takes it from there.

```sh
cd hakios
export DEVICE=garnet                        # without it xl uses redwood's firmware and fails
./xl ramdisk userland/system.apfs           # 2.4 GB into RAM, about 80 s; every loader session
XL_SECS=300 XL_CMDLINE="$(tools/hakios-config.py args)" ./xl run
./xl reboot                                 # back to Android when done
```

- `DEVICE=garnet` is required. `xl` does not read the device from `.config`.
- Upload the system image again after every reboot. If you forget, the log shows
  `exec of /sbin/launchd failed, errno 2`.
- `XL_CMDLINE` replaces the config's arguments; keep `$(tools/hakios-config.py args)` in it, or
  the run loses `cpus=`/`bootcpu=`/`ufs=`.
- The result line names the log: `result: returned N 0  [logs/HHMMSS-run.log]`.

## Booting from a Windows laptop (phone with you, PC at home)

The Linux PC stays the build machine. The Windows laptop only drives the phone: it needs a copy of
the HakiOS tree with the built files, Git Bash, and Google's platform-tools. HakiOS supports this
already (`hakios/docs/windows.md`). Nothing builds on the laptop.

### On the laptop, once

1. **platform-tools**: unpack to `C:\platform-tools` (`adb.exe`, `fastboot.exe`).
2. **Git for Windows** (gives Git Bash). `xl` is a bash script and runs there, not in PowerShell.
3. **Python 3** from python.org, with "Add python.exe to PATH" ticked. `xl` runs
   `xnuloader/mkpayload.py` and `mkxnuimg.py` before every boot. If Git Bash only finds `python`,
   add `alias python3=python` to `~/.bashrc`.
4. **rsync for Git Bash.** Git Bash has no rsync. Use the one in **MSYS2** (`pacman -S rsync`), then
   copy `rsync.exe` and its `msys-*.dll` files into `C:\Program Files\Git\usr\bin\`. That copy has to
   be done by hand. The alternative is **WSL** (`sudo apt install rsync`) for the copying only; the
   phone is still driven from Git Bash, because WSL cannot see USB devices.
5. **SSH to the PC**: Windows has OpenSSH built in. Test it with `ssh okihann@PC` (PC = its LAN IP,
   or its Tailscale name when away from home). The PC already runs `sshd`.

### Copy the run files to the laptop

Only what `xl` needs, not the 5.8 GB tree: the kernel and IPSW sources, the unpacked IPSW and
`.git` stay on the PC. Run in Git Bash (or WSL); the first copy is about 2.5 GB, mostly
`system.apfs`, and later copies only send what changed.

```sh
mkdir -p /c/hakios
rsync -avz --progress \
  --exclude .git/ --exclude xnu-src/ --exclude xnu-deps/ --exclude sysimage/ \
  --exclude qemu/ --exclude logs/ --exclude '*.img' \
  okihann@PC:/run/media/okihann/Games/HackiOS-garnet/hakios/ /c/hakios/
```

This copies `xl`, `hakios-kernel`, `xnuloader/loader.efi` (with the iPhone identity built in),
`.config`, `devices/garnet/prebuilt/` (UEFI, BootShim, device tree), `userland/system.apfs` and the
tools `xl` calls. After rebuilding anything on the PC, rerun the same command.

If you build in WSL, copy into WSL's own filesystem (`~/hakios`), not `/mnt/c`. Then copy only
`userland/*.apfs` from there into `C:\hakios\userland\`. The unpacked IPSW has file names NTFS
cannot hold (`docs/windows.md`), which is why `sysimage/` is excluded above.

### Boot, from Git Bash with the phone on the laptop's USB

```sh
cd /c/hakios
export PATH="$PWD/tools/win-bin:/c/platform-tools:$PATH"   # flock stand-in + adb/fastboot
export DEVICE=garnet
./xl ramdisk userland/system.apfs           # about 80 s, every loader session
XL_SECS=300 XL_CMDLINE="$(tools/hakios-config.py args)" ./xl run
./xl reboot                                 # back to Android
```

The commands are the same as on Linux. The phone is booted from RAM with `fastboot boot`; nothing is
flashed.

If `xl` stops with `timeout: command not found` or a `readlink` error, the Git Bash in use lacks a
coreutils program. Install the full Git for Windows (not the portable "MinGit"); it ships both.

### Send the logs back to the PC

```sh
rsync -avz /c/hakios/logs/ okihann@PC:/run/media/okihann/Games/HackiOS-garnet/hakios/logs/
```

The log files keep their names (`HHMMSS-run.log`), so the PC copy gets them all next to its own.
Tell me the file name, as before.

The logs mix text with binary stackshot data at the end. Read them with `strings -n 6 FILE | less`.

### Away from home: start the PC's build over SSH

When a fix needs a rebuild (for example the loader), it is done on the PC:

```sh
ssh okihann@PC 'cd /run/media/okihann/Games/HackiOS-garnet/hakios && xnuloader/build.sh'
```

Then rerun the copy above to get the new `loader.efi`. This copies only that file, plus whatever
else changed.

## Reading a log

| You see | Means |
|---|---|
| only `2 s`, `4 s` … after `jumping to` | loader cannot read the kernel's live log (check `KERNEL_LIVE_MAGIC`) |
| `== KERNEL LOG ==` then kernel lines | the kernel is running and its log came back |
| `exec of /sbin/launchd failed, errno 2` | system image not uploaded: run `./xl ramdisk` |
| `failed to query hardware target: 22` | placeholder `identity.h`: rerun `mkidentity.sh`, rebuild the loader |
| `Kernel data abort` with `esr: 0x96000010` | access to memory that is not there; check the memory window |
| `memorystatus: killing_…` repeating | out of RAM (current blocker) |

Symbolicate a panic address: `llvm-nm -n hakios-kernel > /tmp/kernel.syms`, then look up each `lr:`
against it (the address is in the function whose start is the largest one below it).

On-screen text is printed with each character doubled (`PPaanniicc`). The log is not doubled. The
cause has not been looked into.

## Open problems, in order

1. **Move the root to `darwin`.** The partition exists (`sda35`, 64 GiB, verified by Android).
   Next: `tools/darwin-write-garnet.py check` (read-only), then write the system image with the
   guarded writer (`reports/garnet-darwin-carve.md`), then boot with `root_apfs=`. That removes the
   2.4 GB RAM image — the cause of the memory-pressure stall at the Apple logo.
2. **Secondary cores** `0x100`–`0x300` never start (PSCI CPU_ON or their entry path).
3. **Review** of the remaining uncommitted loader changes listed above.
4. **Drivers** after the home screen: touch (Goodix/Focaltech SPI), buttons (PMK8350), GPU (Adreno
   710 has no driver; framebuffer only), Wi-Fi (WPSS/icnss). See `workstream-C-driver-research.md`.

## The `darwin` carve (what was done, 2026-11-06)

1. `tools/backup-partitions.py backup-garnet --yes` — boot/identity partitions + both GPT copies, proven by SHA-256.
2. f2fs superblock and `segment_info` saved; `f2fs-resize /data 41921536` shrunk the f2fs in place.
   Gotcha: the ioctl takes blocks and reports "done", but writes the new superblock to f2fs's
   **second** copy (byte `0x1400`) — read 2 blocks to see it. `userdata-carve.py` now scans both.
3. `userdata-carve.py backup-garnet 64 darwin` planned and CRC-checked the new GPT; its three `dd`
   commands written the primary and backup table; read-back hashes matched (`0750176f…` head).
4. Android rebooted: boots, data intact, `/dev/block/by-name/darwin` → `sda35`.
5. `devices/garnet/darwin.toml` holds the partition's constants: unique GUID
   `981690B0-E68C-44A7-B597-314A4BE41956`, LBA `45131776..61908986` (16,777,211 blocks of 4096),
   `protected = 0` (fresh partition) until the data volume is written.
