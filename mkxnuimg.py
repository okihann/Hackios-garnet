#!/usr/bin/env python3
"""
Pack the device's UEFI (with the hook that starts the XNU loader) and an optional payload
into an Android boot image for `fastboot boot`.

  kernel = gzip(BootShim + UEFI FD + ["XNUP" container]) + DTB

The boot shim copies the FD to its base and the payload to PAYLOAD_BASE.

The UEFI pieces are taken from a Mu-Silicium tree built here (Mu-Silicium/, see uefi/README.md)
or, when there is none, from devices/redwood/prebuilt/.

usage: mkxnuimg.py [-d DEVICE] [-p payload.bin] [-o out.img]
"""
import argparse, gzip, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
MU = os.path.join(HERE, 'Mu-Silicium')
if not os.path.isdir(MU):
    # the usual case: Mu-Silicium is checked out next to the hakios directory
    alt = os.path.abspath(os.path.join(HERE, '..', 'Mu-Silicium'))
    if os.path.isdir(alt):
        MU = alt
PAYLOAD_BASE, PAYLOAD_MAX = 0xB7000000, 0x01000000


def read_fd_config(device):
    """FD_BASE and FD_SIZE for the device, from devices/<device>/uefi.conf when there is one.

    The BootShim copies exactly FD_SIZE bytes from where the boot image put the UEFI to FD_BASE,
    so these have to be the values the device's UEFI was built with (Mu-Silicium's
    Resources/Configs/<device>.toml: uefi_fd.base and uefi_fd.size). Without a uefi.conf the
    reference device's values are kept: 0x9FD00000 / 0x200000, which redwood's UEFI uses."""
    base, size = 0xB7000000, 0x01000000
    conf = os.path.join(HERE, 'devices', device, 'uefi.conf')
    if os.path.isfile(conf):
        for line in open(conf):
            line = line.split('#', 1)[0].strip()
            if '=' not in line:
                continue
            k, v = line.split('=', 1)
            try:
                n = int(v.strip(), 0)
            except ValueError:
                continue
            if k.strip() == 'FD_BASE':
                base = n
            elif k.strip() == 'FD_SIZE':
                size = n
    return base, size

ap = argparse.ArgumentParser()
ap.add_argument('-d', '--device', default=os.environ.get('DEVICE', 'redwood'),
                help='device directory under devices/ (default: $DEVICE, else redwood)')
ap.add_argument('-p', '--payload')
ap.add_argument('-o', '--output', default=None)
ap.add_argument('--mode', default='RELEASE')
args = ap.parse_args()
if args.output is None:
    args.output = os.path.join(HERE, f'xnu-{args.device}.img')
PREBUILT = os.path.join(HERE, 'devices', args.device, 'prebuilt')
FD_BASE, FD_SIZE = read_fd_config(args.device)

fd_path = os.path.join(MU, f'Build/{args.device}Pkg/{args.mode}_CLANGPDB/FV/SILICIUM_UEFI.fd')
if os.path.isfile(fd_path):
    shim_dir = os.path.join(MU, 'BootShim')
    pass  # keep pre-built shim
    pass
    parts = {'shim': os.path.join(shim_dir, 'BootShim.bin'), 'fd': fd_path,
             'dtb': os.path.join(MU, f'Resources/DTBs/{args.device}.dtb'), 'ramdisk': os.path.join(MU, 'Resources/ramdisk'),
             'mkbootimg': os.path.join(MU, 'Resources/Scripts/mkbootimg.py')}
else:
    parts = {'shim': os.path.join(PREBUILT, 'BootShim.bin'), 'fd': os.path.join(PREBUILT, 'SILICIUM_UEFI.fd'),
             'dtb': os.path.join(PREBUILT, 'device-tree.dtb'), 'ramdisk': os.path.join(PREBUILT, 'ramdisk'),
             'mkbootimg': os.path.join(PREBUILT, 'mkbootimg.py')}
for name, path in parts.items():
    if not os.path.isfile(path):
        hint = ''
        if name == 'fd':
            hint = ("\n  The UEFI image is not in the repository: it carries the phone vendor's own drivers, which are not\n"
                    "  redistributed here. Build it from your own phone's firmware: uefi/README.md")
        sys.exit(f'mkxnuimg.py: {path} not found{hint}')
shim = open(parts['shim'], 'rb').read()
fd = open(parts['fd'], 'rb').read()
assert len(fd) == FD_SIZE, hex(len(fd))

blob = shim + fd
if args.payload:
    data = open(args.payload, 'rb').read()
    # the payload file is a complete "XNUP" container (see xnuloader/mkpayload.py)
    assert data[:4] == b'XNUP' and len(data) <= PAYLOAD_MAX - 0x1000, 'bad or oversized payload'
    blob += data
    blob += b'\0' * (-len(blob) % 16)
    print(f'payload: {len(data)} bytes, bytesum={sum(data) & 0xffffffff:08x}')

dtb = open(parts['dtb'], 'rb').read()
kernel = args.output + '.kernel~'
open(kernel, 'wb').write(gzip.compress(blob, 6) + dtb)
try:
    subprocess.check_call([sys.executable, parts['mkbootimg'], '--kernel', kernel, '--ramdisk', parts['ramdisk'],
                           '--header_version', '1', '-o', args.output])
finally:
    os.unlink(kernel)
print(f'{args.output}: {os.path.getsize(args.output)} bytes (shim {len(shim)}, uncompressed {len(blob)})')
