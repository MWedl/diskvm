import contextlib
import logging
import random
import string
import tempfile
from pathlib import Path
from typing import Optional, Union

from diskvm.data import VolumeInfo, DiskVmCreatorContext, DiskInfo
from diskvm.errors import DiskVmError
from diskvm.plugins.base import PluginSpec
from diskvm.plugins.generic import LvmMountPlugin
from diskvm.utils import run_process
from diskvm.vm.base import VirtualDiskBuilder, VirtualDiskPart


@contextlib.contextmanager
def master_key_file(key):
    with tempfile.NamedTemporaryFile('wb') as f:
        f.write(key)
        f.flush()
        yield f.name


def is_luks(volume_info: VolumeInfo):
    try:
        run_process(['cryptsetup', 'isLuks', volume_info.flat_mount])
        return True
    except DiskVmError:
        return False


def try_mount_luks(volume_info: VolumeInfo, decrypt_args) -> Optional[VolumeInfo]:
    try:
        # Mount LUKS
        volume_name = 'luks-' + ''.join(random.choices(string.ascii_letters, k=10))
        run_process(['cryptsetup', 'open', '--type=luks', *(['--readonly'] if volume_info.disk_info.readonly else []),
                     *decrypt_args, volume_info.flat_mount, volume_name])
        luks_device = VolumeInfo(
            disk_info=volume_info.disk_info,
            flat_mount=Path('/dev/mapper') / volume_name,
            parent=volume_info,
            additional_info={'luks': {'mounted': True}}
        )

        try:
            # Deactivate LVM volume groups that were automatically activated by crypsetup
            # LVM will be handled by another plugin
            if volume_group := LvmMountPlugin.list_physical_volumes().get(luks_device.flat_mount):
                LvmMountPlugin.deactivate_volume_group(volume_group)
        except DiskVmError:
            logging.exception('Error while handling LUKS LVM volume groups')

        return luks_device
    except DiskVmError:
        return None


def unmount_luks(mount_point: VolumeInfo) -> bool:
    if isinstance(mount_point, VolumeInfo) and mount_point.additional_info.get('luks', {}).get('mounted') and \
            mount_point.flat_mount and mount_point.flat_mount.exists():
        volume_name = mount_point.flat_mount.name
    else:
        return False

    run_process(['cryptsetup', 'close', volume_name])
    return True


def find_master_key(volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[bytes]:
    # Key already found on previous run
    if key := volume_info.additional_info.get('luks', {}).get('master_key'):
        return key

    for key in ctx.options.additional_options.get('master_keys', []):
        with master_key_file(key) as mk_filename:
            if mount_point := try_mount_luks(volume_info, ['--master-key-file', mk_filename]):
                unmount_luks(mount_point)
                return key
    return None


class LuksMountPlugin(PluginSpec):
    def mounted_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        if is_luks(volume_info):
            logging.info(f'Detected LUKS volume: {volume_info}')
            volume_info.additional_info.setdefault('luks', {})['enabled'] = True

    def mount(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[Union[Path, VolumeInfo, list[VolumeInfo]]]:
        if not volume_info.additional_info.get('luks', {}).get('enabled'):
            return None

        logging.info(f'Try mounting LUKS volume {volume_info}')

        if mk := find_master_key(volume_info=volume_info, ctx=ctx):
            with master_key_file(mk) as mk_filename:
                volume_info.additional_info['luks']['master_key'] = mk
                return try_mount_luks(volume_info, ['--master-key-file', mk_filename])

        logging.warning(f'Could not mount LUKS volume {volume_info}')
        return None

    def unmount_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        return unmount_luks(volume_info)


class LuksAddPasswordPlugin(LuksMountPlugin):
    NEW_PASSWORD = 'newpwd'

    def modify_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        if not volume_info.additional_info.get('luks', {}).get('enabled'):
            return

        logging.info(f'Try adding password to LUKS volume {volume_info}')
        if mk := find_master_key(volume_info=volume_info, ctx=ctx):
            with master_key_file(mk) as mk_filename:
                with tempfile.NamedTemporaryFile('w') as pw_file:
                    pw_file.write(self.NEW_PASSWORD)
                    pw_file.flush()
                    run_process(['cryptsetup', 'luksAddKey', '--master-key-file', mk_filename,
                                 volume_info.flat_mount, pw_file.name])
                    logging.info(f'Added LUKS password "{self.NEW_PASSWORD}" for {volume_info}')
                    volume_info.additional_info['luks'] |= {
                        'password': self.NEW_PASSWORD,
                        'master_key': mk
                    }
        else:
            logging.warning(f'Could not find a matching LUKS master key {volume_info}')


class LuksOnTheFlyDecryptPlugin(LuksMountPlugin):
    # NOTE: plugin does not work well with LVM: cannot mount LVM volume writeable from virtual disk
    #       because the same LVM volume is mounted read-only from the original disk image
    #       One cannot access two LVM devices with same volume group: PV xxx prefers device yyy

    def before_create_disk(self, disk_builder: VirtualDiskBuilder, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        for volume_info in disk_info.volumes:
            if not volume_info.additional_info.get('luks', {}).get('mounted') or \
                    not volume_info.parent.additional_info.get('luks', {}).get('enabled'):
                continue

            disk_builder.add_part(VirtualDiskPart(
                source_file=volume_info.flat_mount,
                source_offset=0,
                target_offset=volume_info.parent.offset,
                length=volume_info.size,
            ))
            # Set disk and volumes to mounted while the VM runs
            disk_info.keep_mounted = True

