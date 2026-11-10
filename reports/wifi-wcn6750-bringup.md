# Wi-Fi bring-up on garnet (WCN6750 / ath11k): status and the next test

**Device** Redmi Note 13 Pro 5G / POCO X6 5G (`garnet`, SM7435)
**Driver** Linux ath11k 7.0.14, compiled unmodified against FreeBSD LinuxKPI and linked into
XNU through `wifi/lx` (`tools/mk-ath11k.sh`)
**Radio firmware** `WLAN.HL.3.4.3-00347.2-QCAHLSWMTPLZ-1.44046.4.47379.2` (wpss.mdt/b02..b08 from
the phone's own `firmware_mnt`)
**Written** 9 November 2026

---

## 1. Where the exchange gets to

A run with `wifi=1 wifi_loud=1 wifi_lx=2` (ath11k's own `qmi.c`, registers written for real)
completes, in order and without error:

| Step | Result |
|---|---|
| WLFW QMI service connects | ok |
| Indication register (`fw_ready_enable`, `cal_done_enable`, `fw_init_done_enable`, `msa_ready_enable`) | accepted |
| Host capability | accepted |
| MSA info `0x82a00000` / `0x300000` | accepted |
| MSA ready (+ MSA_READY_IND 0x2B) | accepted |
| Target capability, device info | ok (device info returns error 94; port falls back to aperture `0x22800000`) |
| BDF (`board.bin`, 26328 bytes, 5 chunks) | downloaded to 0 |
| **then** | **nothing.** No `FW_READY_IND` (0x21), no `FW_INIT_DONE_IND` (0x38) |

The driver's last line is `BDF download complete` and the exchange stops there.

## 2. What the diagnostics established (run logs/210652-run.log)

A `wifi_diag=1` boot argument was added to `drivers/wifi/hakios.wifi.c`: it starts a thread while
`step_driver()` is blocked in `hakios_lx_start()` (up to 60 s) and reads the radio's SMMU fault
registers every 3 s. Result, over the whole wait:

    wifi: diag: +3s .. +57s: SMMU clean (context 6 fsr 0x400; sgfsr 0): the radio has not faulted
    wifi: diag: stopped: 0 faults, 19 clean checks

`fsr 0x400` is the SMMU's reset value; `0x400 & SMMU_FSR_ALL == 0`, so it is **not** a fault.
Conclusion: **the firmware is not faulting on memory and not dying — it is idle and silent.**

This rules out the earlier hypothesis that the MSA or a ring was unmapped. That was checked
against the device tree as well:

    wlan_fw_mem: wlan_fw_region@82a00000 { reg = <0x00 0x82a00000 0x00 0x300000>; }
    wpss_mem:    wpss_region@85300000     { reg = <0x00 0x85300000 0x00 0x1900000>; }

and `qcom,wlan-msa-fixed-region = <0x492>` (phandle of `wlan_fw_region`). The address `qmi.c`
sends (`0x82a00000`, 3 MB) is the device tree's own; `drivers/wifi/hakios.wifi.c`'s
`WPSS_MEMORY_SM7435 = 0x85300000` (25 MB) is the firmware body. Both correct.

## 3. What was tried, and what it said

### 3a. Cold-boot calibration (WLAN_MODE(7)) — refused

After the BDF, ask the firmware for cold-boot calibration, as the stand-in
(`drivers/wifi/hakios.wlfw.c`) once saw this family of firmware accept:

    ath11k (debug): wlan mode req mode 7
    ath11k WARNS: wlan mode request failed (mode: 7): 1 90

Error 90 is `QMI_ERR_INCOMPATIBLE_STATE_V01`. Per the state machine worked out for `wpss.b06`:

| Request | Required firmware state |
|---|---|
| `WLAN_MODE(7)` cold boot | 9 |
| `WLAN_CFG_REQ` | 9 **or** 10 |
| `WLAN_MODE(0)` mission | 11 |

So at the time of the request the firmware is **not at state 9**.

### 3b. Reverted the artificial event post

The resumption prompt's plan — remove the old artificial `FW_INIT_DONE` post and wait for the
firmware's own 0x21 — was carried out and then measured. It does not work on this firmware: 0x21
never arrives (section 1). The prompt's assumption that the firmware emits 0x21 unprompted after
BDF is **not true for WLAN.HL.3.4.3-00347.2**.

## 4. RESOLVED: mission mode reached (9 November, 22:46)

`WLFW_CAL_REPORT_REQ` (0x0026) was the missing message, confirmed by Android's own boot log
(icnss2 sends it, then the firmware runs resident calibration and emits FW_READY ~5 s later).
The exact encoding icnss2 uses (`wlan_firmware_service_v01.c`, authoritative) differs from
ath10k's copy on three points -- `meta_data_len` is QMI_DATA_LEN of `sizeof(u8)` so the encoder
writes 1 byte not 2, `meta_data` elements are `QMI_SIGNED_4_BYTE_ENUM`, and
`QMI_WLFW_MAX_NUM_CAL_V01` is 5. Corrected in `qmi.h` and `qmi.c`. The first attempt at
`0x0026` with the wrong encoding got `QMI_ERR_MALFORMED_MSG` (1 1) -- the firmware parsed the
TLV header, saw the wrong width, and rejected it.

Result of the corrected run (`logs/224214-run.log`):

    ath11k (debug): cal report request succeeded     (55.423406)
    ath11k (debug): firmware ready                    (60.576197, +5.15 s)
    ath11k (debug): wlan cfg req                      (60.582080, accepted)
    ath11k (debug): wlan mode req mode 0              (60.585473)   ← MISSION MODE

Five point one five seconds, against the Android dmesg's 4.95 s. Every earlier symptom -- state
8, CFG refused 90, mode 7 refused 90, SMMU clean for 57 s, no 0x21 -- is explained by the
missing CAL_REPORT: the firmware waits for the host to say it has no external calibration
before running resident calibration. Once told, it announces ready and mission mode follows.

## 4a. The panic that came next: first MMIO to the WLAN registers aborts

Immediately after mode 0 was accepted, on the very first register write to the WLAN's MMIO
block:

    lx: about to write the chip's 0x1b80048 (at +0x100048 of its block): 0xa2c00040
    Unclassified kernel abort (fault_code=0x10)
    panic: Kernel data abort at pc 0xfffffff007afebc8
      esr 0x96000010 → DFSC 0x10 = synchronous external abort
      far 0xfffffff940274048     (kernel VA = ab->mem + 0x100048)

The address is right on paper: `0x22800000 + 0x100048 = 0x22900048`, inside the DT's
`qcom,icnss@22800000 { reg = <0x22800000 0x800000>; }`. The write value `0xa2c00040` is the
MSI page `hakios_wifi_radio_msi()` handed us. The mapping is 8 MB (0x800000) and 0x100048 is
inside it. So the MMU translated, and the *bus* rejected: the block does not answer. This is
the first access to the aperture the port has ever made -- all prior Wi-Fi traffic went over
GLINK/QRTR, never through the register window.

Likely causes, in order:

1. **Regulators/clocks not voted.** Android's icnss2 `icnss_hw_power_on()` enables five
   regulators for adrastea (`vdd-cx-mx` 752 mV, `vdd-1.8-xo` 1.8 V, `vdd-1.3-rfa` 1.304 V,
   `vdd-3.3-ch0/ch1` 3.312 V) and `cxo_ref_clk_pin` before ioremap. Our port does none of
   this; TrustZone's PAS start brings the DSP up but may not power the analog side.
2. **Wrong CE window offset.** If the fallback aperture 0x22800000 in qmi.c is only correct
   for qcacld's usage and not ath11k's window scheme, the CE window at +0x100000 lands on
   unassigned bus space.
3. **AHB clock gate.** The port's own comment at `hakios.wifi.c:1361` says
   `gcc_wpss_ahb_clk` "is not looked at: a read there is answered with an external abort.
   They are TrustZone's." If that bus clock is off (which PAS does not enable), the WPSS
   MMIO slave doesn't respond.

Next experiment: read (do not write) one word at offset 0x0 of the aperture right after the
driver's ioremap, before any CE write. If that aborts too, the aperture is dead and cause 1 or 3
is it; if it succeeds, the CE window offset is wrong (cause 2) and a second read at 0x100000
confirms.

## 5. The change now built (kept for reference)



`xnu-deps/build/ath11k/src/ath11k/qmi.c`, in `ath11k_qmi_event_server_arrive()`, right after
`ath11k_qmi_event_load_bdf()`:

    if (ab->qmi.cal_done == 0) {
        ath11k_dbg(ab, ATH11K_DBG_QMI,
            "poke: no 0x21 after BDF; posting FW_READY to try WLAN_CFG then WLAN_MODE(0)\n");
        ath11k_qmi_driver_event_post(qmi, ATH11K_QMI_EVENT_FW_READY, NULL);
    }

This posts exactly the event a real `FW_READY_IND` would post. The normal handler runs
`ath11k_core_qmi_firmware_ready()`, which sends `WLAN_INI -> WLAN_CFG -> WLAN_MODE(0)` in order —
the order that matches the state machine (cfg accepts at 9 or 10, mode 0 needs 11). Nothing else
in the driver is changed.

The firmware's own answer to `WLAN_CFG` separates the two remaining possibilities:

| Result | Firmware state | Meaning |
|---|---|---|
| `WLAN_CFG` accepted, `WLAN_MODE(0)` accepted | 10 | helium finished; 0x21 was simply not delivered. **WiFi reaches mission mode.** |
| `wlan config request failed: 1 90` | 8 | helium never finished. The blocker is inside the firmware's `platform_helium_service_map_and_ce_cfg`, upstream of QMI. |

## 5. How to run it

`root.apfs` must be the ordinary driver image (no `GL=test`), built with `userland/build.sh` and
nothing else after it, since each build overwrites `root.apfs`.

    cd /run/media/okihann/Games/HackiOS-garnet/hakios
    export DEVICE=garnet
    XL_CMDLINE="-v cpus=8 bootcpu=7 ufs=2 wifi=1 wifi_loud=1 wifi_diag=1 root_apfs=ufs_darwin wifi_nocal=1 wifi_keep=1" XL_SECS=60 ./xl run

Read the log from `BDF download complete` onward. What decides it:

- `poke: no 0x21 after BDF; posting FW_READY ...`
- `wlan cfg req` -> accepted, or `wlan config request failed: 1 90`
- `wlan mode req mode 0` -> accepted or refused
- after that, any `HTC`, `ce`, `copy engine`, `unified ready` lines
- `wifi: diag:` lines (should stay clean either way)

## 6. Build state at the time of writing

| File | mtime |
|---|---|
| `drivers/wifi/hakios.wifi.c` (diagnostic thread, `wifi_diag=1`) | 2026-11-09 20:33 |
| `xnu-deps/build/ath11k/src/ath11k/qmi.c` (FW_READY post after BDF) | 2026-11-09 21:12 |
| `xnu-deps/build/ath11k/out/ath11k.o` (768540 bytes on disk; `mk-ath11k` reports 514574 bytes of code and data, 0 stubs) | 2026-11-09 21:12 |
| `hakios-kernel` | 2026-11-09 21:12 |

`ath11k.o` must be rebuilt (`tools/mk-ath11k.sh`) before the kernel is relinked
(`tools/make-kernel.sh`) whenever `qmi.c` changes; the kernel links the pre-built object and does
not compile the driver itself. Verified in the linked kernel:
`strings hakios-kernel | grep -c "posting FW_READY"` -> 1.

## 7. Unchanged, for the record

- `drivers/gpu/hakios.adreno.c`: `gpu_exact` now defaults to 0, so submissions no longer insert
  `CP_WAIT_FOR_IDLE` (a full pipeline drain per submit). Not yet measured on hardware.
- `hakios-metal`: verified to build correctly with its AIR compiler (`make ios MESA=1`); the
  archive is arm64 and `build.sh` links Mesa's archives (its `nm` is the Mach-O-aware one from
  `xnu-env.sh`). No change made.
- Wi-Fi diagnostic code is inert without `wifi_diag=1`.

## 8. If the post-FW_READY path also refuses

Then the firmware is at state 8: the helium platform service never completed. The next things to
look at, in order, are the firmware's own DIAG output (the `wpss.b06` strings include
`platform_helium_service_map_and_ce_cfg.c`, `platform_proc_msa_ready_req` and the
`qmi_plat_check_state` messages), the `WLAN_INI` request (`fw_wmi_diag_event`), and whether the
firmware expects a `CE` configuration before helium runs rather than after.
