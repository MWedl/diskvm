import logging
from pathlib import Path
from typing import Optional

from Registry.Registry import Registry
from Registry.RegistryParse import RegistryException

from diskvm.data import DiskVmCreatorContext, DiskInfo, PartitionScheme, VolumeInfo
from diskvm.plugins.base import PluginSpec
from diskvm.vm.base import VirtualDiskBuilder, FirmwareType
from diskvm.vm.vmware import Vmware


class DetectOperatingSystemPlugin(PluginSpec):
    CONTEXT_KEY = 'auto_detected_guest_os'
    WIN_GUESTOS_VMWARE = {
        'Windows 11': 'windows9',
        'Windows 10': 'windows9',
        'Windows 8': 'windows8',
        'Windows 7': 'windows7',
    }

    def detect_windows(self, fs_root: Path) -> Optional[str]:
        # Read Windows version from Registry
        # Tested with Windows 7 and Windows 10
        reg_file = fs_root / 'Windows' / 'System32' / 'config' / 'SOFTWARE'
        if reg_file.exists():
            try:
                reg = Registry(reg_file).open(r'Microsoft\Windows NT\CurrentVersion')
                win_product_name = reg.value('ProductName').value()
                is_64bit = 'amd64' in reg.value('BuildLabEx').value().lower()

                return [v for n, v in self.WIN_GUESTOS_VMWARE.items() if win_product_name.startswith(n)][0] +\
                    ('-64' if is_64bit else '')
            except (RegistryException, IndexError):
                pass

        return None

    def detect_linux(self, fs_root: Path) -> Optional[str]:
        if (fs_root / 'etc' / 'passwd').exists():
            # VMware Workstation can boot any Linux distribution with guestOS = "otherlinux"
            return 'otherlinux'
        return None

    def detect_os(self, fs_root: Path) -> Optional[str]:
        return self.detect_windows(fs_root) or \
               self.detect_linux(fs_root)

    def before_create_disk(self, disk_builder: VirtualDiskBuilder, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        # The current implementation only supports vmware
        if not isinstance(ctx.instance.virtualization_software, Vmware):
            return

        logging.info('Begin OS detection')

        for volume in disk_info.volumes:
            if volume.filesystem_mount:
                logging.debug(f'Detecting OS on {volume}')
                if os := self.detect_os(volume.filesystem_mount):
                    # Successfully detected OS
                    logging.info(f'Detected Operating System: {os}')
                    ctx.additional_info[self.CONTEXT_KEY] = os
                    return

        logging.warning('Could not detect OS')

    def before_create_vm(self, ctx: DiskVmCreatorContext) -> None:
        if os := ctx.additional_info.get(self.CONTEXT_KEY):
            ctx.vm_builder.guest_os = os


class DetectEfiPlugin(PluginSpec):
    TYPE_EFI_SYSTEM_PARTITION_GPT = 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B'
    TYPE_EFI_SYSTEM_PARTITION_MBR = 0xEF

    @staticmethod
    def is_efi_system_partition(p: VolumeInfo):
        return (p.disk_info.partition_scheme == PartitionScheme.GPT and p.volume_type == DetectEfiPlugin.TYPE_EFI_SYSTEM_PARTITION_GPT) or \
               (p.disk_info.partition_scheme == PartitionScheme.MBR and p.volume_type == DetectEfiPlugin.TYPE_EFI_SYSTEM_PARTITION_MBR)

    def before_create_disk(self, disk_builder: VirtualDiskBuilder, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        for p in disk_info.volumes:
            if self.is_efi_system_partition(p):
                ctx.additional_info['is_efi'] = True
                logging.info(f'Detected EFI system partition {p}')

    def before_create_vm(self, ctx: DiskVmCreatorContext):
        if not isinstance(ctx.instance.virtualization_software, Vmware):
            return

        ctx.vm_builder.firmware = FirmwareType.EFI if ctx.additional_info.get('is_efi') else FirmwareType.BIOS

