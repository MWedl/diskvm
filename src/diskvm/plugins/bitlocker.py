import contextlib
import logging
import re
import struct
import tempfile
from enum import IntEnum
from pathlib import Path
from typing import Optional, Union

from diskvm.plugins import utils
from diskvm.plugins.base import PluginSpec
from diskvm.data import VolumeInfo, DiskVmCreatorContext, DiskInfo
from diskvm.utils import run_process, retry
from diskvm.vm.base import VirtualDiskBuilder, VirtualDiskPart

BITLOCKER_SIGNATURE = b'-FVE-FS-'


class BitLockerMode(IntEnum):
    AES256_XTS = 0x8005
    AES128_XTS = 0x8004
    AES256_CBC = 0x8003
    AES128_CBC = 0x8002
    AES256_CBC_DIFFUSER = 0x8001
    AES128_CBC_DIFFUSER = 0x8000


@contextlib.contextmanager
def fvek_file(mode: BitLockerMode, key: bytes):
    with tempfile.NamedTemporaryFile('wb') as f:
        f.write(struct.pack('<H', mode.value))
        # Pad key to 512 bit
        f.write(key.ljust(64, b'\x00'))
        f.flush()
        yield f.name


def is_bitlocker(volume: VolumeInfo):
    return utils.is_signature(volume.flat_mount, BITLOCKER_SIGNATURE)


def unmount_bitlocker(mount_point: Union[Path, VolumeInfo]):
    if isinstance(mount_point, Path) and mount_point.is_file():
        mount_file = mount_point
    elif isinstance(mount_point, Path) and mount_point.is_dir():
        mount_file = mount_point / 'dislocker-file'
    elif isinstance(mount_point, VolumeInfo) and mount_point.additional_info.get('bitlocker', {}).get('mounted') \
            and mount_point.flat_mount and mount_point.flat_mount.exists():
        mount_file = mount_point.flat_mount
    else:
        return False

    mount_dir = mount_file.parent
    if mount_dir.is_mount():
        # Target might be busy from previous umount operations. Retry some time until umount succeeds
        @retry(10, sleep=0.5)
        def run_unmount():
            run_process(['umount', str(mount_dir)])
        run_unmount()
    if mount_dir.is_dir():
        mount_dir.rmdir()
    return True


def try_mount_bitlocker(volume: VolumeInfo, decrypt_args) -> Optional[VolumeInfo]:
    mount_dir = Path(tempfile.mkdtemp())
    try:
        run_process(['dislocker-fuse', '--volume', str(volume.flat_mount),
                     *(['--readonly'] if volume.disk_info.readonly else []),
                     *decrypt_args, '--', str(mount_dir)])
        mount_file = mount_dir / 'dislocker-file'

        # Check if filesystem signature is NTFS
        # Dislocker is not able to check whether the given FVEK is correct or not.
        # When the FVEK is wrong, dislocker succeeds but dislocker-file contains garbage
        if utils.is_ntfs(mount_file):
            return VolumeInfo(
                disk_info=volume.disk_info,
                flat_mount=mount_file,
                parent=volume,
                additional_info={'bitlocker': {'mounted': True}}
            )
        else:
            unmount_bitlocker(mount_dir)
            return None
    except Exception:
        unmount_bitlocker(mount_dir)
        return None


def find_correct_fvek(volume: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[tuple[BitLockerMode, bytes]]:
    master_keys = ctx.options.additional_options.get('master_keys', [])
    if not master_keys:
        return None

    # Read cipher mode from metadata
    try:
        metadata_info = run_process(['dislocker-metadata', '-V', str(volume.flat_mount)]).decode()
        mode = BitLockerMode(int(re.search(r'Encryption Type:.*\((0x800[0-5])\)', metadata_info).group(1), 16))
    except Exception:
        return None

    # Try all provided keys
    for key in master_keys:
        with fvek_file(mode=mode, key=key) as fvek_filename:
            if mounted := try_mount_bitlocker(volume, ['--fvek', fvek_filename]):
                unmount_bitlocker(mounted)
                return mode, key
    logging.warning(f'Could not find correct FVEK: {volume}')
    return None


class BitLockerMountPlugin(PluginSpec):
    def mounted_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        if is_bitlocker(volume=volume_info):
            logging.info(f'Detected BitLocker volume: {volume_info}')
            volume_info.additional_info.setdefault('bitlocker', {})['enabled'] = True

    def mount(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[Union[Path, VolumeInfo, list[VolumeInfo]]]:
        if not volume_info.additional_info.get('bitlocker', {}).get('enabled', False):
            return None

        logging.info(f'Try mounting BitLocker volume: {volume_info}')

        if mounted := try_mount_bitlocker(volume_info, ['--clearkey']):
            logging.info(f'Successfully mounted BitLocker volume with clearkey')
            volume_info.additional_info['bitlocker']['clearkey'] = True
            return mounted
        elif fvek := find_correct_fvek(volume_info, ctx):
            logging.info(f'Found correct FVEK for BitLocker volume {fvek}')
            with fvek_file(*fvek) as fvek_filename:
                volume_info.additional_info['bitlocker']['fvek'] = fvek
                return try_mount_bitlocker(volume_info, ['--fvek', fvek_filename])

        return None

    def unmount_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        return unmount_bitlocker(volume_info)


class BitLockerOverridePasswordPlugin(BitLockerMountPlugin):
    # TODO: password is currently hard-coded in dislocker-pwreset.c
    #       add command line options --add-clearkey --add-password=newpwd
    NEW_PASSWORD = 'newpwd'

    def modify_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        bitlocker_info = volume_info.additional_info.get('bitlocker', {})
        if not bitlocker_info.get('enabled', False):
            return

        logging.info(f'Trying to modify the BitLocker header: add clear key and to override password {volume_info}')
        if fvek := find_correct_fvek(volume=volume_info, ctx=ctx):
            with fvek_file(*fvek) as fvek_filename:
                # Add clear key and default password
                run_process(['dislocker-pwreset', '--volume', str(volume_info.flat_mount),
                             '--fvek', fvek_filename,
                             '-vvvvvvvv'
                             ])
                volume_info.additional_info['bitlocker'] |= {
                    'clearkey': True,
                    'password': self.NEW_PASSWORD,
                }
                logging.info(f'Added BitLocker clear key and password "{self.NEW_PASSWORD}" {volume_info}')


class BitLockerOnTheFlyDecryptPlugin(BitLockerMountPlugin):
    def before_create_disk(self, disk_builder: VirtualDiskBuilder, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        for volume_info in disk_info.volumes:
            # Check if volume is BitLocker-encrypted and could be mounted
            if not volume_info.additional_info.get('bitlocker', {}).get('mounted', False) or \
                    not volume_info.parent.additional_info.get('bitlocker', {}).get('enabled', False):
                continue

            # Replace encrypted BitLocker volume with plaintext on-the-fly decrypted and mounted volume in virtual disk
            disk_builder.add_part(VirtualDiskPart(
                source_file=volume_info.flat_mount,
                source_offset=0,
                target_offset=volume_info.parent.offset,
                length=volume_info.size,
            ))
            # Set disk and volumes to mounted while the VM runs
            disk_info.keep_mounted = True
