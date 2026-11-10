# WCN6750 MMIO aperture: why 0x22800000 does not answer, and what is left

**Device** garnet (SM7435), WCN6750 over AHB
**Status** QMI protocol works (mission mode reached, every run). The driver's first MMIO write
aborts. The remaining question is whether that aperture can be powered from this port's kernel,
and reading the vendor sources says no.
**Written** 9 November 2026, after the attempts below.

---

## 1. The state that is proven and repeatable

With `wifi=1 wifi_loud=1 wifi_diag=1` (and **no** `wifi_mmio`, **no** `wifi_rails`, **no**
`wifi_en`), the driver reaches mission mode every time:

    ath11k (debug): cal report request succeeded
    ath11k (debug): firmware ready
    ath11k (debug): wlan mode req mode 0
    lx: THE WLAN APERTURE IS NOT TOUCHED (wifi_mmio=1 makes the accesses)
    wifi: 15: the driver's start returns 5
    wifi: THE RADIO DID NOT COME UP: step 15 failed, 10 s in

No abort, no panic, kernel keeps booting, log produced. That is the reference state. Everything
below is what happens when we try to cross from "protocol works" to "registers reachable".

## 2. Why the aperture cannot be reached (source-verified)

The MMIO block at `qcom,icnss@22800000 { reg = <0x22800000 0x800000>; }` does not answer from
this boot. The read of three source trees says why.

### 2.1 No SCM call

`drivers/firmware/qcom_scm.h` defines the whole PAS family this port uses:

    QCOM_SCM_PIL_PAS_INIT_IMAGE      0x01
    QCOM_SCM_PIL_PAS_MEM_SETUP       0x02
    QCOM_SCM_PIL_PAS_AUTH_AND_RESET  0x05
    QCOM_SCM_PIL_PAS_SHUTDOWN        0x06
    QCOM_SCM_PIL_PAS_IS_SUPPORTED    0x07
    QCOM_SCM_PIL_PAS_MSS_RESET       0x0a

Nothing else for WPSS. There is no SCM command for AHB enable, for a regulator, for the WLAN
aperture, or for handing the RSC to EL1. The `SVC_PWR` commands (`PWR_IO_DISABLE_PMIC_ARBITER`,
`PWR_IO_DEASSERT_PS_HOLD`, `PWR_MMU_SYNC`) go the other way.

### 2.2 The remoteproc path votes the DSP's rails, not the aperture's

`drivers/remoteproc/qcom_q6v5_pas.c`, `adsp_start()`, in order:

    do_bus_scaling(adsp, true)             /* icc_set_bw, an interconnect vote */
    adsp_pds_enable(active_pds)            /* node power domains */
    adsp_pds_enable(proxy_pds)
    adsp_toggle_load_state(qmp, ...)       /* AOSS mailbox, which this port does */
    clk_prepare_enable(xo)
    clk_prepare_enable(aggre2_clk)
    enable_regulators(adsp)                /* ONLY the wpss node's own supplies */
    scm_pas_enable_bw()
    qcom_scm_pas_auth_and_reset(pas_id)    /* which this port does */
    scm_pas_disable_bw()

`adsp->regs` comes from the wpss node's supplies. The DT there names `cx-supply` and `mx-supply`
only. **The five regulators the WLAN block needs** (`vdd-cx-mx`, `vdd-1.8-xo`, `vdd-1.3-rfa`,
`vdd-3.3-ch0`, `vdd-3.3-ch1`) are declared on the **icnss** node and voted by icnss2's
`icnss_hw_power_on()`, which this port does not implement.

### 2.3 icnss2's `icnss_hw_power_on` needs a driver this port does not have

The five regulators are PM7325/PM8350C devices on the SPMI bus. Enabling them goes through the
PMIC arbiter. `drivers/spmi/spmi-pmic-arb.c` on this SoC reports version 7
(`PMIC_ARB_VERSION_V7_MIN 0x70000000`, which the port's own survey logs as `0x70000000`), and the
port's channel layout is version 5's. The v7 layout renumbers channels, APIDs, owners and the
per-APID register base (`pmic_arb_apid_map_offset_v7` returns `0x2000 + 4n`, the v5 one
`0x900 + 4n`); porting it is a real project and not what a WiFi bring-up should be holding.

## 3. The RPMh attempt that reset the phone to EDL (do not repeat)

Reading `rpmh-rsc.c` after the fact explains both the crash and the wrong choice.

The AP is allowed to trigger **active-only** TCSs and no others:

    /* Note that the AP is only in charge of triggering active-only transfers.
     * The AP never triggers sleep/wake values using this function. */
    static void __tcs_set_trigger(struct rsc_drv *drv, int tcs_id, bool trigger)

The bootloader's own TCS dump (step 3) labels which is which:

    TCS 0 (at once): ...    TCS 1 (at once): ...    <- active-only
    TCS 2 (asleep): ...     TCS 3 (asleep): ...
    TCS 4 (asleep): ...     TCS 5 (awake): ...     <- sleep/wake set
    TCS 6 (awake): ...      TCS 7 (awake): ...

The failed attempt wrote to **TCS 5**, a wake TCS. That is exactly what the RSC driver says the AP
must never do. The phone reset to Qualcomm EDL (`05c6:900e`), the loader never regained control,
and no log was produced. The correct TCS would have been TCS 1. But even that is not obviously
safe, because the Linux driver also does, before any trigger, work this port does not:
`rpmh_tcs_mutex` per TCS, `tcs_is_free()`, `tcs_invalidate()`, `RSC_DRV_IRQ_ENABLE`, per-command
`RSC_DRV_CMD_WAIT_FOR_CMPL`, and the `CMD_MSGID_RESP_REQ` reply. Writing raw TCS registers from
EL1 with none of that is what we tried, and it produced a hardware reset rather than a kernel
panic.

**Do not add back a `wifi_rails`-style TCS write without either (a) the full RSC driver logic,
or (b) a documented statement from the vendor or U-Boot tree that a raw TCS 1 trigger from EL1
is supported.**

## 4. What was tried and what each attempt ruled out

| Attempt | Result | Rules out |
|---|---|---|
| `wifi_mmio=1` (aperture accesses allowed) | kernel panic, `esr 0x96000010` external abort, `far = ab->mem + 0x100048`, log written | nothing by itself; the write path is correct, the bus is not |
| `wifi_mmio` unset (gate held) | driver fails cleanly, `start returns 5`, kernel keeps booting | the panic was the aperture, not the driver |
| `wifi_en=1` (gpio84 output-high) | pin asserts `OUTPUT HIGH`, abort unchanged | `cnss_wlan_en` alone is not the enable |
| `wifi_rails=1` on TCS 5 | phone resets to EDL, no log | TCS 5 is a wake TCS; do not write there |
| RPMh vote on a truncation bug | panic `far 0x255a50`, `esr 0x96000045` translation | a driver bug, fixed (`vm_offset_t base`) |
| `wifi_rails=1` on the fixed kernel | phone resets to EDL again | the crash is the TCS write, not the truncation |

## 5. What remains possible

There are three real paths left, and none is small.

1. **Port `spmi-pmic-arb.c`'s v7 layout.** This is the *correct* way to enable the five rails: the
   driver needs the v7 channel layout and APID/owner tables to write a PMIC register that turns a
   regulator on. Once that exists, `enable_regulators` can be called for the adrastea list and the
   aperture may answer. Large, but the source is available and safe (SPMI transactions are not
   privileged in the same way RSC writes are).
2. **Find whether the phone's UEFI / ABL already powers the WLAN block at boot.** If the block is
   supposed to be powered by the bootloader and our port is the first thing that touches it, the
   failure may be that UEFI does not (device-dependent) or that it is powered by the time the
   kernel runs and our access is the first abort for another reason. Worth checking by reading the
   UEFI sources or observing a Linux session on the same phone (if such a thing exists).
3. **Use the RPMh driver as Linux uses it** — port `rpmh-rsc.c`'s TCS allocation, IRQ handling and
   per-command reply machinery, and use TCS 1 (an active-only TCS). Only if (1) and (2) do not
   pan out, because it is the path that reset the phone.

## 6. Recommendation

The protocol work is finished; the WiFi protocol layer reaches mission mode reproducibly. What is
missing is a driver-side mechanism to power a block on the SPMI bus, which needs either the PMIC
arbiter v7 port or the RPMh driver. Neither belongs inside a WiFi bring-up; both are their own
piece of the platform.

Until one of those exists, the kernel behaviour to keep is:

    XL_CMDLINE="... wifi=1 wifi_loud=1 wifi_diag=1 ..." XL_SECS=90 ./xl run

with **no** `wifi_mmio`, **no** `wifi_rails`, **no** `wifi_en`. The driver reaches mission mode,
reports the gate, fails cleanly, and produces a log. Any further attempt on the aperture needs a
demonstrated-safe power path first.

The current state of `drivers/wifi/hakios.wifi.c` reflects this: `wifi_rails` is renamed
`wifi_rails_unsafe`, and its announce string says so.

