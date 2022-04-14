import logging
import tempfile
from pathlib import Path
from typing import Optional, Union

from diskvm.errors import DiskVmError
from diskvm.plugins.base import PluginSpec
from diskvm.data import VolumeInfo, DiskVmCreatorContext
from diskvm.utils import run_process


class GenericMountPlugin(PluginSpec):
    @staticmethod
    def run_mount(volume_info, fs_mount, options=None):
        return run_process(['mount', '--source', str(volume_info.flat_mount), '--target', str(fs_mount),
                            '--types', 'auto', *(['--read-only'] if volume_info.disk_info.readonly else []),
                           *(['-o', ','.join(map(lambda t: f'{t[0]}={t[1]}' if t[1] is not None else t[0], options.items()))] if options else [])],
                           stderr=True)

    def mount(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[Union[Path, VolumeInfo, list[VolumeInfo]]]:
        fs_mount = Path(tempfile.mkdtemp())
        try:
            stdout, stderr = self.run_mount(volume_info, fs_mount)

            # # TODO: after ntfsfix, Windows cannot boot correctly (Recovery Screen, error code 0xc0000001)
            # # Fix NTFS filesystem when Windows was running or hibernated
            # if b'Falling back to read-only mount because the NTFS partition is in an\nunsafe state.' in stderr and \
            #         not volume_info.disk_info.readonly:
            #     run_process(['umount', str(fs_mount)])
            #     run_process(['ntfsfix', str(volume_info.flat_mount)])
            #     self.run_mount(volume_info, fs_mount)

            return fs_mount
        except DiskVmError:
            fs_mount.rmdir()
            return None

    def unmount_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        if not volume_info.filesystem_mount.is_mount():
            return False

        try:
            run_process(['umount', str(volume_info.filesystem_mount)])
            if volume_info.filesystem_mount.is_dir():
                volume_info.filesystem_mount.rmdir()
            return True
        except DiskVmError:
            logging.exception('Error while unmounting')
            return False

    def unmount_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        if 'loop' in volume_info.flat_mount.name:
            try:
                run_process(['losetup', '--detach', str(volume_info.flat_mount)])
                return True
            except DiskVmError:
                pass
        return False


class LvmMountPlugin(PluginSpec):
    # TODO: cannot access two LVM devices with same volume group
    #       PV xxx prefers device yyy (can LVM devices be split, even with same VG)
    @staticmethod
    def _run_colon_output(args):
        return [l.strip().split(':') for l in run_process(args).decode('utf-8').splitlines()]

    @staticmethod
    def list_physical_volumes() -> dict[Path, str]:
        return {Path(pv[0]): pv[1] for pv in LvmMountPlugin._run_colon_output(['pvdisplay', '--colon'])}

    @staticmethod
    def list_logical_volumes():
        return {Path(lv[0]): lv[1] for lv in LvmMountPlugin._run_colon_output(['lvdisplay', '--colon'])}

    @staticmethod
    def activate_volume_group(volume_group: str, readonly: bool):
        run_process(['vgchange', '--activate', 'y', '--yes', volume_group])

    @staticmethod
    def deactivate_volume_group(volume_group: str):
        run_process(['vgchange', '--activate', 'n', volume_group])

    def mount(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[Union[Path, VolumeInfo, list[VolumeInfo]]]:
        if volume_group := self.list_physical_volumes().get(volume_info.flat_mount):
            logging.info(f'Activating LVM volume group {volume_group} on {volume_info}')
            self.activate_volume_group(volume_group, readonly=volume_info.disk_info.readonly)

            logical_volumes = []
            for lv, vg in self.list_logical_volumes().items():
                if vg == volume_group:
                    logical_volumes.append(VolumeInfo(
                        disk_info=volume_info.disk_info,
                        flat_mount=lv,
                        parent=volume_info,
                        additional_info={'lvm': {
                            'active': True,
                            'volume_group': volume_group,
                        }}
                    ))
            return logical_volumes
        return None

    def unmount_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        if volume_info.additional_info.get('lvm', {}).get('active') and \
                (vg := volume_info.additional_info.get('lvm').get('volume_group')):
            logging.info(f'Deactivating LVM volume group {vg} on {volume_info}')
            self.deactivate_volume_group(volume_group=vg)
            return True
        return False
