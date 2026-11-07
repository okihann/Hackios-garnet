#!/usr/bin/env python3
"""Print what unpack.sh needs to know about an IPSW: its build, and the disk images' names by role.

usage: ipsw-info.py IPSW          KEY=value lines, shell-quoted, for `eval`
       ipsw-info.py IPSW KEY      just that value

Keys:
  VERSION          the iOS version            (16.5, 16.7.16)
  BUILD            the build                  (20F66, 20H392)
  CACHE            the dyld shared cache directory name  (<BUILD>__iPhone10,3_6)
  DEVICE_TREE      the device tree inside the IPSW       (Firmware/all_flash/DeviceTree.d22ap.im4p)
  OS_DMG           the root filesystem image   (BuildManifest role "OS")
  CRYPTEX_OS_DMG   the OS cryptex              (role "Cryptex1,SystemOS")
  APP_DMG          the App cryptex             (role "Cryptex1,AppOS")

The names are read out of BuildManifest.plist, never hardcoded: they change with every build
(16.5 20F66: 096-04088-077.dmg / 096-04666-078.dmg / 096-03536-083.dmg;
 16.7.16 20H392: 087-86698-044.dmg / 087-86593-047.dmg / 087-86715-047.dmg). The roles do not.
An IPSW whose manifest has no cryptex roles (pre-iOS 16) is refused: the system image needs them.
"""
import plistlib
import shlex
import sys
import zipfile

ROLES = (('OS_DMG', 'OS'), ('CRYPTEX_OS_DMG', 'Cryptex1,SystemOS'), ('APP_DMG', 'Cryptex1,AppOS'))


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__.split('\n\n')[1])
    ipsw = sys.argv[1]
    try:
        archive = zipfile.ZipFile(ipsw)
    except (OSError, zipfile.BadZipFile) as error:
        sys.exit(f'ipsw-info.py: {ipsw}: {error}')
    with archive:
        try:
            manifest = plistlib.loads(archive.read('BuildManifest.plist'))
        except KeyError:
            sys.exit(f'ipsw-info.py: {ipsw}: no BuildManifest.plist (is this an IPSW?)')
        names = set(archive.namelist())

    build = manifest.get('ProductBuildVersion')
    version = manifest.get('ProductVersion')
    identities = manifest.get('BuildIdentities') or []
    if not build or not identities:
        sys.exit(f'ipsw-info.py: {ipsw}: the manifest names no build')
    # refuse another phone's IPSW before 6 GB is copied out of it: the system image is built from the
    # iPhone X's userland, and a newer phone's would unpack and then make an image Darwin cannot run
    phones = set(manifest.get('SupportedProductTypes') or [])
    if phones and not phones & {'iPhone10,3', 'iPhone10,6'}:
        sys.exit(f'ipsw-info.py: {ipsw}: for {" ".join(sorted(phones))}, not the iPhone X (iPhone10,3/10,6)')

    out = {'VERSION': version, 'BUILD': build, 'CACHE': f'{build}__iPhone10,3_6'}
    # the disk images, by role; they are the same in every build identity of one IPSW
    for key, role in ROLES:
        path = None
        for identity in identities:
            info = identity.get('Manifest', {}).get(role, {}).get('Info', {})
            path = path or info.get('Path')
        if not path:
            sys.exit(f'ipsw-info.py: {ipsw}: no "{role}" in the manifest (iOS 16 or newer is needed)')
        if path not in names:
            sys.exit(f'ipsw-info.py: {ipsw}: the manifest names "{role}" {path}, which is not in the archive')
        out[key] = path
    # the iPhone X's tree; d22ap is iPhone10,3/10,6, d221ap the other iPhone X model
    for tree in ('Firmware/all_flash/DeviceTree.d22ap.im4p', 'Firmware/all_flash/DeviceTree.d221ap.im4p'):
        if tree in names:
            out['DEVICE_TREE'] = tree
            break

    if len(sys.argv) == 3:
        key = sys.argv[2]
        if key not in out:
            sys.exit(f'ipsw-info.py: no {key} (one of: {" ".join(out)})')
        print(out[key])
        return
    for key, value in out.items():
        print(f'{key}={shlex.quote(str(value))}')


if __name__ == '__main__':
    main()
