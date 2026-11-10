# Metal on Adreno 710 (garnet): what works, what does not, and the evidence

**Written** 2026-11-10, after the A7xx compute port and the clear investigation.
**Scope** `hakios-metal` on the Adreno 710 (chip 0x07010000), `gpu=1 metal=1`.

## What works now (proven on hardware, `probe: ok`)

The probe (`hakios-metal/tools/probe.cpp`) was rebuilt with `--render --no-msaa` to skip
the stages that hang the GPU and reach the draw tests on a live GPU.

- **Kernel GPU plumbing**: CP_MEMCPY, fills of every awkward length, 16 MB at 885 MB/s.
- **Metal buffer compute, AIR to NIR to ir3**: `fill_id`, `clz_id`, `bytes_id`, `uniform_id`,
  `pack_id`, `undef_phi_id`, `width_id`, `ballot_id` -- all 1024/1024 words. (Before this
  session, every one of these was 0/1024.)
- **Metal 3D draws**: `tri_flat` 2016/2016, the pair 4096/4096, `tri_color` 4032/4032,
  `tri_attr` with vertex descriptors, all `clear without a draw` for 7 formats,
  `tri_tex` including ASTC/cube/3D/array/1D/bicubic, `depth` (8 variants),
  `fragment depth`, `dual source`, `shadow map`, `200 draws in ONE command buffer`.

## The two verified fixes

1. **A7xx compute-stage registers** (`a6xx.hpp` + `compute.cpp`). The Adreno 710 moved the
   HLSQ CS registers from 0xb9xx/0xbbxx to 0xa9xx, and `HLSQ_CS_CNTL_0` does not exist there
   (the regids go through `SP_CS_CNTL_0`; the grid through `HLSQ_CS_NDRANGE_0..6` +
   `LAST_LOCAL_SIZE`). `827616a` did this for VS/HS/DS/GS/FS and skipped CS.
   Mesa's own `tu_pipeline.cc:368` switches it by chip.
2. **Null page at IOVA 0** (`hakios.adreno.c`). A zeroed, writable 4 KB page in the 3D core's
   SMMU context. Prevents one bad fetch (a null vertex buffer, a descriptor that wrapped to 0)
   from stalling the command processor and killing the run. SMMU faults went from 1 to 0.

A third change (removing duplicate A6xx HLSQ writes from `render.cpp`) was tried and
reverted: it was correct hygiene but did not fix anything observed, and one wrong change in
a register stream is worse than none until the clear bug is understood.

## What is still broken

### 1. `clear then tri_flat` -- a Clear that a draw follows never lands (VERIFIED, live GPU)

    probe: FAIL clear then tri_flat T1: 2016 of 2016 interior pixels the colour,
           0 of 2016 exterior canary, 2080 pixels neither (first at 0,0: 0xa5a5a5a5)

`0xa5a5a5a5` is the pre-fill canary -- the clear did not happen. `clear without a draw`
passes (same `clearAll` to `clearStream`, same CCU sequence) because `encodeClear` follows
with `passTail`, whose own flush rescues the writes. `encodeDraw` does not.

**Five fixes tried, all bit-identical to the failure**:
- `CP_WAIT_FOR_IDLE` before the CCU clean
- `PC_CCU_FLUSH_COLOR_TS` (seqno) instead of `CCU_CLEAN_COLOR` (non-seqno) + `RB_DONE_TS`
- Removing `PC_CCU_INVALIDATE_COLOR` after the flush
- The same in `depthClearStream`
- Auditing `appendDeduped`'s packet walk -- it does pass `CP_BLIT` through

Bit-identical through five different hypotheses means **the clear is not reaching the GPU**,
not that it reaches and is discarded. Remaining suspects, in order:
1. `gmemClears_` (passed as `skipClears` to `encodeBegin` from `CommandBuffer::draw`) is
   nonzero when it should not be
2. Something else in `encodeBegin` that `encodeClear` does not do
3. The `head` stream (which holds the clear) appended in the wrong order relative to `body`
   (the draw), or `appendDeduped`'s `open_->regs` shadow dropping a state write the clear needs

**Next experiment (one line, one boot):** print `skipClears, anyColorClear(pass), pass.load`
in `encodeBegin`, run the probe, read the values for the failing test.

### 2. A 2D blit hangs the GPU (VERIFIED, live GPU)

    probe: FAIL mip, a copy of 16 x 8 at (8, 4) into another texture at (0, 0)
           (the GPU did not finish it in 5 s and is not answering)

Every test after this one in the probe is measured on a dead GPU -- including both
`iosurface` tests. **The `iosurface` FAILs in every recent run are dead-GPU artifacts, not
evidence that IOSurface rendering is broken.** We do not know whether it works.

**Next experiment:** add a probe flag to run `iosurface` (9m) before `mip, a copy`, or
reorder those stages. One boot, real answer.

### 3. The window server still hangs at submission 23-25 (VERIFIED, real phone)

`metal=1` flashes black/white (two alternating, uncleared drawables). Submission 25 is the
last, the core has done 19, `CP fault 0`, `protection 0` -- a stall, not a fault. Ruled out:
- **many draws** (`200 draws in ONE command buffer` passes with 4096/4096 pixels)
- **GMEM** (off; 0 passes in GMEM in every run)
- **A7xx render register set** (audited against Mesa: `RB_UNKNOWN_8812=0x3ff`,
  `RB_UNKNOWN_8E06=0x02080000`, `RB_UNKNOWN_8E09=0x4`, `GRAS_UNKNOWN_8007=0`,
  `GRAS_UNKNOWN_8110=0x2`, `RB_BLIT_CLEAR_MODE=SYSMEM` -- all match)

The flashing is most likely: CA clears its drawable (`loadOp=Clear`), the clear bug (1)
drops it, CA draws icons/text on top, and the compositor swaps two drawables whose
backgrounds never changed. **Fixing (1) may fix the flashing. That is the next thing.**

## Debugging aid (not a fix)

`hakios-metal/tools/probe.cpp` gained `--render` (skip compute + textures) and `--no-msaa`
(skip the multisample blocks). Keep them: without `--render --no-msaa` the probe never
reaches the draw tests on a live GPU, and every result after the hang is meaningless.

## Reproducing

    make -C hakios-metal ios MESA=1 HAKIOS=$PWD/xnu-deps/build/mesa/mesa-25.0.7
    METAL_PLUGIN=ca RC_JOBS=rc.jobs.metalprobe \
      EXTRA=$PWD/hakios-metal/out/ios/hakios-metal-probe userland/build.sh
    XL_CMDLINE="-v ufs=2 root_apfs=ufs_darwin gpu=1 bootcpu=7 cpus=8 cpufreq=1 gpu_log=1" \
      XL_SECS=240 ./xl run

## What went wrong this session (so the next one does not repeat it)

- The "IOSurface writes do not land" conclusion was built on the `iosurface` FAILs, but
  those run after `mip, a copy` has already hung the GPU. **No conclusion about IOSurface
  rendering is supported by any log.** The probe must reorder before that question is asked.
- Five fixes were tried on the clear bug before instrumenting it. The instrument (one line
  of stderr) should have come after the second failure, not the fifth.
