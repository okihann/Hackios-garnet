#!/usr/bin/env python3
"""Garnet variant of darwin-write.py: write an image into the phone's `darwin` partition, and nowhere else,
from Android (adb, root). Constants live in devices/garnet/darwin.toml, which is read-only here.

usage:
  darwin-write-garnet.py check              darwin's identity, read from the phone now                (writes nothing)
  darwin-write-garnet.py sum   BLOCK COUNT  sha256 of COUNT blocks of darwin from BLOCK                (writes nothing)
  darwin-write-garnet.py plan  IMAGE BLOCK  every check `write` makes, and the command it would run    (writes nothing)
  darwin-write-garnet.py write IMAGE BLOCK  with DARWIN_WRITE=yes: IMAGE at darwin blocks BLOCK.., read back and compared
  darwin-write-garnet.py zero  BLOCK COUNT  with DARWIN_WRITE=yes: zeros over COUNT blocks, read back and compared
  darwin-write-garnet.py selftest           the refusals, against this tool's own checks and a GPT read now (writes nothing)

Blocks are darwin's own, 4096 bytes, counted from the partition's start. What it takes to write:

  - darwin is what it was when the port was set up, read at the time of the write and compared with the
    constants in devices/garnet/darwin.toml, all of them: the GPT of logical unit 0 (header and table CRCs
    right; entry [partn] named darwin, its type and unique GUIDs, [first]..[last] LBA; no other entry over
    any of it), sda being logical unit 0 of the UFS controller, /dev/block/by-name/darwin being
    /dev/block/sda[partn], whose number, name, start and size in sysfs and whose size by blockdev say the
    same. Anything else: no write.
  - the blocks lie inside darwin and after the Darwin volume at its start (rootfs.apfs, never written):
    BLOCK >= [protected] and BLOCK + COUNT <= [blocks]. Anything else: no write.
  - the write goes to /dev/block/by-name/darwin only, by dd with seek and count in blocks of 4096, from a
    copy of the image on the phone whose sha256 has been checked; in the same shell, right before dd,
    the name is checked again to be sda[partn] of darwin's size. Never /dev/block/sda or anything else.
  - afterwards the blocks are read back and their sha256 has to be the image's.

Every attempt, refused or done, goes into logs/darwin-write-garnet.log.
"""
import binascii
import hashlib
import os
import struct
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, 'logs', 'darwin-write-garnet.log')
BOARD = os.path.join(ROOT, 'devices', 'garnet', 'darwin.toml')


def board():
    """devices/garnet/darwin.toml: the one place the board's numbers live."""
    v = {}
    for line in open(BOARD, encoding='utf-8'):
        line = line.split('#', 1)[0].strip()
        if not line or '=' not in line:
            continue
        k, x = line.split('=', 1)
        v[k.strip()] = x.strip().strip('"')
    return v


B = board()
NAME = B['name']
TYPE = B['type']
UNIQUE = B['unique']
LUN = int(B['lun'])
PARTN = int(B['partn'])
FIRST = int(B['first'])
LAST = int(B['last'])
BS = 4096
BLOCKS = LAST - FIRST + 1
PROTECTED = int(B['protected'])
DEV = '/dev/block/by-name/' + NAME
NODE = '/dev/block/sda%d' % PARTN
TMP = '/data/local/tmp/darwin-write.img'


class Refused(Exception):
    pass


def log(line):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a') as f:
        f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))
    print('darwin-write: ' + line)


def phone(script, binary=False):
    """Run a script in the phone's root shell (on its standard input, as flash-fingerprint.sh does)."""
    r = subprocess.run(['adb', 'shell', 'su -c sh'], input=script.encode(), capture_output=True, timeout=3600)
    if r.returncode != 0:
        raise Refused('the phone said %d: %s' % (r.returncode, (r.stdout + r.stderr).decode(errors='replace')[-400:]))
    return r.stdout if binary else r.stdout.decode(errors='replace')


def phone_bytes(command):
    r = subprocess.run(['adb', 'exec-out', 'su -c "%s"' % command], capture_output=True, timeout=600)
    if r.returncode != 0:
        raise Refused('reading from the phone: exit %d' % r.returncode)
    return r.stdout


def check_gpt(raw):
    """raw: LUN 0's blocks 1.. (header, then the table). Returns the problems found."""
    bad = []
    hdr = raw[:BS]
    if hdr[:8] != b'EFI PART':
        return ['no GPT header at LBA 1']
    hsize = struct.unpack_from('<I', hdr, 12)[0]
    hcrc = struct.unpack_from('<I', hdr, 16)[0]
    h = bytearray(hdr[:hsize])
    h[16:20] = b'\0\0\0\0'
    if binascii.crc32(bytes(h)) & 0xffffffff != hcrc:
        bad.append('GPT header CRC wrong')
    last_usable = struct.unpack_from('<Q', hdr, 48)[0]
    ent_lba, n, esize = struct.unpack_from('<QII', hdr, 72)
    tcrc = struct.unpack_from('<I', hdr, 88)[0]
    if ent_lba != 2 or esize != 128 or n * esize > len(raw) - BS:
        return bad + ['GPT table not where it was (LBA %d, %d entries of %d)' % (ent_lba, n, esize)]
    table = raw[BS:BS + n * esize]
    if binascii.crc32(table) & 0xffffffff != tcrc:
        bad.append('GPT table CRC wrong')
    if last_usable < LAST:
        bad.append('last usable LBA %d is before darwin\'s end' % last_usable)
    found = []
    for i in range(n):
        e = table[i * esize:(i + 1) * esize]
        if e[:16] == b'\0' * 16:
            continue
        name = e[56:128].decode('utf-16le', errors='replace').rstrip('\0')
        first, last = struct.unpack_from('<QQ', e, 32)
        if name == NAME:
            found.append((i + 1, str(uuid.UUID(bytes_le=e[:16])).upper(), str(uuid.UUID(bytes_le=e[16:32])).upper(),
                          first, last))
        elif first <= LAST and last >= FIRST:
            bad.append('entry %d (%s, LBA %d..%d) overlaps darwin' % (i + 1, name, first, last))
    if len(found) != 1:
        return bad + ['%d entries named darwin' % len(found)]
    num, typ, uniq, first, last = found[0]
    for what, got, want in (('number', num, PARTN), ('type', typ, TYPE), ('unique GUID', uniq, UNIQUE),
                            ('first LBA', first, FIRST), ('last LBA', last, LAST)):
        if got != want:
            bad.append('darwin\'s %s is %s, not %s' % (what, got, want))
    return bad


def identity():
    """darwin's identity, read now. Raises Refused on anything that is not as it was."""
    out = phone('''
echo "node=$(readlink -f %(dev)s)"
echo "lun=$(readlink -f /sys/class/block/sda/device)"
echo "partition=$(cat /sys/class/block/sda%(n)d/partition)"
echo "start=$(cat /sys/class/block/sda%(n)d/start)"
echo "size=$(cat /sys/class/block/sda%(n)d/size)"
echo "name=$(grep ^PARTNAME= /sys/class/block/sda%(n)d/uevent | cut -d= -f2)"
echo "bytes=$(blockdev --getsize64 %(dev)s)"
echo "ss=$(blockdev --getss /dev/block/sda)"
''' % {'dev': DEV, 'n': PARTN})
    have = {}
    for line in out.splitlines():
        if '=' in line:
            k, x = line.strip().split('=', 1)
            have[k] = x
    want = {'node': NODE, 'partition': str(PARTN), 'start': str(FIRST * BS // 512), 'size': str(BLOCKS * BS // 512),
            'name': NAME, 'bytes': str(BLOCKS * BS), 'ss': str(BS)}
    bad = []
    for k, x in want.items():
        if have.get(k) != x:
            bad.append('%s is %r, want %r' % (k, have.get(k), x))
    lun = have.get('lun', '')
    if lun and not lun.endswith('/0:0:0:%d' % LUN) and not lun.endswith(':%d' % LUN):
        bad.append('sda is %r, not logical unit %d of the UFS controller' % (lun, LUN))
    raw = phone_bytes('dd if=/dev/block/sda bs=%d skip=1 count=3 2>/dev/null' % BS)
    if raw[:8] != b'EFI PART':
        bad.append('the GPT could not be read')
    else:
        bad += check_gpt(raw)
    if bad:
        raise Refused('; '.join(bad))
    return '%s = %s, unit %d, LBA %d..%d, %d blocks of %d, type %s, unique %s' % (
        NODE, PARTN, LUN, FIRST, LAST, BLOCKS, BS, TYPE, UNIQUE)


def blocks_ok(block, count):
    if block < PROTECTED:
        raise Refused('block %d is inside the Darwin volume (blocks 0..%d), which is never written' % (block, PROTECTED - 1))
    if block + count > BLOCKS:
        raise Refused('blocks %d..%d go past darwin\'s end (%d blocks)' % (block, block + count - 1, BLOCKS))
    if count <= 0:
        raise Refused('count %d is nothing' % count)


def blocks_ok_read(block, count):
    if count <= 0 or block < 0 or block + count > BLOCKS:
        raise Refused('blocks %d..%d are not inside darwin' % (block, block + count - 1))


def guarded(command):
    """command, run only if the name still leads to darwin, checked in the same shell right before it."""
    return ('t=$(readlink -f %(dev)s); s=$(blockdev --getsize64 %(dev)s)\n'
            'if [ "$t" = %(node)s ] && [ "$s" = %(bytes)d ]; then %(cmd)s 2>&1; else echo "REFUSED: $t $s"; exit 3; fi\n'
            % {'dev': DEV, 'node': NODE, 'bytes': BLOCKS * BS, 'cmd': command})


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_zeros(n):
    h = hashlib.sha256()
    h.update(b'\0' * n)
    return h.hexdigest()


def phone_sum(block, count):
    out = phone('dd if=%s bs=%d skip=%d count=%d 2>/dev/null | sha256sum\n' % (DEV, BS, block, count))
    return out.split()[0]


def number(s):
    try:
        n = int(s, 0)
    except ValueError:
        raise Refused('not a number: %r' % s)
    if n < 0:
        raise Refused('a negative number: %r' % s)
    return n


def prepare(image, block):
    size = os.path.getsize(image)
    if size % BS:
        raise Refused('%s is %d bytes, not whole blocks of %d' % (image, size, BS))
    count = size // BS
    blocks_ok(block, count)
    return count


def cmd_plan(image, block, write=False):
    count = prepare(image, block)
    who = identity()
    digest = sha256_file(image)
    dd = 'dd if=%s of=%s bs=%d seek=%d count=%d conv=notrunc,fsync' % (TMP, DEV, BS, block, count)
    log('%s: %s (%d blocks, sha256 %s) at darwin blocks %d..%d (bytes %d..%d); %s; would run: %s' % (
        'write' if write else 'plan', image, count, digest, block, block + count - 1, block * BS,
        (block + count) * BS - 1, who, dd))
    if not write:
        return
    if os.environ.get('DARWIN_WRITE') != 'yes':
        raise Refused('DARWIN_WRITE=yes is not set: nothing written')
    r = subprocess.run(['adb', 'push', image, TMP], capture_output=True, text=True)
    if r.returncode != 0:
        raise Refused('adb push: ' + r.stderr.strip())
    try:
        there = phone('sha256sum %s\n' % TMP).split()[0]
        if there != digest:
            raise Refused('the copy on the phone has sha256 %s, not %s' % (there, digest))
        identity()                                   # once more, right before the write
        out = phone(guarded(dd))
        log('dd: ' + ' '.join(out.split()))
        back = phone_sum(block, count)
        log('read back blocks %d..%d: sha256 %s -- %s' % (block, block + count - 1, back,
                                                          'the same' if back == digest else 'DIFFERENT'))
        if back != digest:
            raise Refused('what was read back is not what was written')
    finally:
        phone('rm -f %s\n' % TMP)


def cmd_zero(block, count):
    blocks_ok(block, count)
    who = identity()
    dd = 'dd if=/dev/zero of=%s bs=%d seek=%d count=%d conv=notrunc,fsync' % (DEV, BS, block, count)
    log('zero: darwin blocks %d..%d; %s; runs: %s' % (block, block + count - 1, who, dd))
    if os.environ.get('DARWIN_WRITE') != 'yes':
        raise Refused('DARWIN_WRITE=yes is not set: nothing written')
    out = phone(guarded(dd))
    log('dd: ' + ' '.join(out.split()))
    back, want = phone_sum(block, count), sha256_zeros(count * BS)
    log('read back blocks %d..%d: %s' % (block, block + count - 1, 'zeros' if back == want else 'NOT ZEROS ' + back))
    if back != want:
        raise Refused('what was read back is not zeros')


def cmd_selftest():
    """The refusals: each must refuse, and none of them reaches the phone's dd."""
    ok = True
    for block, count, why in ((PROTECTED - 1, 256, 'one block into the Darwin volume'), (0, 1, 'the Darwin volume\'s superblock'),
                              (BLOCKS - 100, 256, 'past darwin\'s end'), (BLOCKS, 1, 'right after darwin'),
                              (-1, 1, 'before darwin'), (PROTECTED, 0, 'nothing'), (10 ** 12, 1, 'far away')):
        try:
            blocks_ok(block, count)
            log('selftest: %s (blocks %d+%d) was NOT refused' % (why, block, count))
            ok = False
        except Refused as e:
            log('selftest: %s refused: %s' % (why, e))
    for s in ('-5', '1e6', '0x100', ''):
        try:
            number(s)
            ok = False
            log('selftest: block %r was NOT refused' % s)
        except Refused as e:
            log('selftest: block %r refused: %s' % (s, e))
    raw = phone_bytes('dd if=/dev/block/sda bs=%d skip=1 count=3 2>/dev/null' % BS)
    log('selftest: the GPT read now: %s' % ('; '.join(check_gpt(raw)) or 'darwin as it was'))
    ok &= not check_gpt(raw)
    # the same GPT changed in one place at a time: each must be refused
    def entry_off(field):
        return BS + (PARTN - 1) * 128 + field
    for what, off, value in (('a type GUID byte', entry_off(0), None), ('a unique GUID byte', entry_off(20), None),
                             ('the first LBA', entry_off(32), struct.pack('<Q', FIRST - 8)),
                             ('the last LBA', entry_off(40), struct.pack('<Q', LAST + 1)),
                             ('the name', entry_off(56), 'Darwin'.encode('utf-16le'))):
        bad = bytearray(raw)
        if value is None:
            bad[off] ^= 0x01
        else:
            bad[off:off + len(value)] = value
        # with the table's CRC made right again, so that the field itself is what is caught
        n = struct.unpack_from('<I', bad, 80)[0]
        struct.pack_into('<I', bad, 88, binascii.crc32(bytes(bad[BS:BS + n * 128])) & 0xffffffff)
        h = bytearray(bad[:struct.unpack_from('<I', bad, 12)[0]])
        h[16:20] = b'\0\0\0\0'
        struct.pack_into('<I', bad, 16, binascii.crc32(bytes(h)) & 0xffffffff)
        problems = check_gpt(bytes(bad))
        log('selftest: GPT with %s changed: %s' % (what, '; '.join(problems) if problems else 'NOT refused'))
        ok &= bool(problems)
    bad = bytearray(raw)
    bad[BS + (PARTN - 1) * 128 + 40] ^= 0x01                 # without fixing the CRC: the CRC catches it
    problems = check_gpt(bytes(bad))
    log('selftest: GPT with a byte changed and the CRC left: %s' % ('; '.join(problems) or 'NOT refused'))
    ok &= bool(problems)
    log('selftest: %s' % ('every refusal refused, the GPT as it was' if ok else 'FAILED'))
    return ok


def main():
    a = sys.argv[1:]
    try:
        if a == ['check']:
            log('check: ' + identity())
        elif len(a) == 3 and a[0] == 'sum':
            block, count = number(a[1]), number(a[2])
            identity()
            log('sum: darwin blocks %d..%d: sha256 %s (zeros would be %s)' % (block, block + count - 1,
                                                                            phone_sum(block, count),
                                                                            sha256_zeros(count * BS)))
        elif len(a) == 3 and a[0] in ('plan', 'write'):
            cmd_plan(a[1], number(a[2]), write=(a[0] == 'write'))
        elif len(a) == 3 and a[0] == 'zero':
            cmd_zero(number(a[1]), number(a[2]))
        elif a == ['selftest']:
            sys.exit(0 if cmd_selftest() else 1)
        else:
            sys.exit(__doc__)
    except Refused as e:
        log('REFUSED %s: %s' % (' '.join(a), e))
        sys.exit(2)


if __name__ == '__main__':
    main()
