# Give iOS 64 GB on garnet by carving `darwin` off the end of `userdata`

**DONE — 2026-11-06.** The partition exists: `darwin`, 64 GiB at LBA `45131776..61908986`, entry 34
(zero-based) of LUN 0's GPT, `/dev/block/by-name/darwin` → `sda35` under Android. Android reboots and
its data is intact; `userdata` is 159.92 GiB, exactly its f2fs size, so `resize.f2fs` has nothing to
grow back. The read-back hashes of both table copies matched what the tool made. Constants are in
`devices/garnet/darwin.toml` (unique GUID `981690B0-E68C-44A7-B597-314A4BE41956`, `protected = 0`
until the data volume is written). What follows is the record of how, plus what is left.

`darwin-write-garnet.py` and `userdata-carve.py` both refuse to write unless the phone's own GPT
already matches the numbers in `devices/garnet/darwin.toml`. Review that file first — every value
below comes from it and from your phone's own partition log.

**Note on `check`:** the tool exits with `REFUSED: the phone said 1: adb: no devices/emulators found`
when no phone is attached. That is expected. Run the same command once the phone is on USB to see the
real identity check.

## What the tools are

| Tool | Purpose | Writes to phone? |
|---|---|---|
| `tools/backup-partitions.py` | copies the partition table + GPT head/tail and the partitions it names to the PC, with SHA-256 checked on both sides | no |
| `tools/userdata-carve.py` | plans the new GPT (shrink `userdata`, add `darwin`) and prints the exact `dd` commands; with `--wipe` it skips the f2fs shrink | no (you run the `dd` it prints, or it prints `--wipe` instructions) |
| `tools/android/f2fs-resize` | a 2.6 KB static binary run in `adb shell` to shrink `userdata`'s filesystem by 64 GB in place | yes, but only inside the same session (it is run once, and userdata is made again if `--wipe` is used instead) |
| `tools/darwin-write-garnet.py` | the guarded writer for the `darwin` partition; refuses every block outside `protected..BLOCKS-1`, and reads every write back | yes, only inside `darwin` |
| `hakios/drivers/storage/hakios.ufs.c` | the kernel's UFS driver; `darwin` shows up as `/dev/rufs_darwin` when `root_apfs=` is passed | no |
| `userland/src/init.c` | mounts the root from `root_apfs=` (the `darwin` partition) instead of a RAM image | no |

## The numbers for garnet (all from the phone's own GPT)

| Field | Value | Source |
|---|---|---|
| `userdata` LBA | `3209728..61908986` (229 GiB) | `logs/062938-run.log` |
| `darwin` LBA (after carve) | `45131264..61908986` (64 GiB) | `userdata-carve.py` math on the real table |
| `darwin` blocks | `16777723` | `LAST - FIRST + 1` |
| `protected` | `1374208` blocks at the start of `darwin` (the boot volume the writer never touches) | `devices/garnet/darwin.toml` |
| `partition number` | `34` (`/dev/block/sda34`) | `devices/garnet/darwin.toml` |
| `type` | `7C3457EF-0000-11AA-AA11-00306543ECAC` (Apple APFS container) | `devices/garnet/darwin.toml` |
| `unique` | `FD6854AD-24AA-4FC3-94FE-290116858D68` | `devices/garnet/darwin.toml` |
| `LUN` | `0` | `devices/garnet/darwin.toml` |
| `userdata` after shrink | `159.92 GiB` | `userdata-carve.py` |

## What to run first (read-only, nothing written)

On the PC with the phone on USB, before any write:

```sh
cd hakios
export DEVICE=garnet
# 1. back up the whole GPT plus the partitions the loader's log showed
python3 tools/backup-partitions.py backup-garnet --yes
# 2. read-only check: the GPT and the phone's own sysfs say the same thing about sda34
python3 tools/darwin-write-garnet.py check
# 3. what would be written, no write happens
python3 tools/darwin-write-garnet.py plan userland/system.apfs 1374208
```

`backup-garnet/` is the restore copy. `check` is the tool reading the GPT and the phone's sysfs;
it should print `darwin as it was`. `plan` prints the exact `dd` it would run — look at the LBA
range before you approve the next step.

If `check` says anything else (wrong partition, wrong size, wrong GUID), stop — the numbers in
`devices/garnet/darwin.toml` are wrong for this phone, and the file needs fixing before any write.

## Writing the partition (the only step that touches the phone)

Only after the three commands above all say the GPT is as expected:

```sh
# shrink userdata's filesystem by 64 GiB in place, then write the new GPT
adb push tools/android/f2fs-resize /data/local/tmp/f2fs-resize
python3 tools/userdata-carve.py backup-garnet 64 darwin
# it prints the f2fs-resize and dd commands for /dev/block/sda
# you run the shrink, then the two dd lines it prints
```

`userdata-carve.py` refuses to write unless `userdata` is the last partition, no partition is named
`darwin` yet, the f2fs's tail segments hold no data, and the new GPT passes CRC checks. The `dd`
commands it prints are the only bytes that change the disk. Both copies of the GPT are written
separately (the table before the header, so a cut write leaves a bad CRC and the backup is used).

## After the carve

The kernel mounts `darwin` with `root_apfs=darwin` (the same arg `userland/src/init.c` already uses).
The 2.4 GB `system.apfs` no longer goes into RAM, so the memory pressure that stalls the boot at the
Apple logo should go away.

## What to check in `devices/garnet/darwin.toml` before approving

1. `first`/`last`/`blocks`/`protected`/`partn`/`unique` match what `darwin-write-garnet.py check`
   reads from the phone (run `check` first — it is read-only).
2. `partn = 34` is what `/dev/block/sda34` resolves to on your phone.
3. `first`/`last` are the exact LBA `userdata-carve.py` produces for a 64 GiB carve on this table.

If any value differs from what `check` prints, the toml is wrong, not the phone.

## What changed in the tools

- `tools/darwin-write-garnet.py` is the same logic as `tools/darwin-write.py` but reads every
  constant from `devices/garnet/darwin.toml` instead of being hard-coded to redwood. It logs every
  attempt to `logs/darwin-write-garnet.log` and refuses anything outside `protected..BLOCKS-1`.
- `tools/userdata-carve.py` is already device-agnostic; it only needs `backup-garnet/` and a size.
- `tools/android/f2fs-resize` is built and tested (it's a 2.6 KB binary; `tools/android/build.sh`
  produces it for the phone's root shell).
