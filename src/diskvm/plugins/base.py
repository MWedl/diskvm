import logging
from pathlib import Path
from typing import Optional, Union

from diskvm.data import DiskInfo, DiskVmCreatorContext, VolumeInfo
from diskvm.vm.base import VirtualDiskBuilder


class PluginSpec:
    def mounted_disk(self, disk_info: DiskInfo, ctx: DiskVmCreatorContext) -> None:
        pass

    def mounted_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> None:
        pass

    def mount(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[Union[Path, VolumeInfo, list[VolumeInfo]]]:
        """
        This function is called when mounting volumes.
        If the volume cannot be mounted, None should be returned.
        If the filesystem was successfully mounted, the Path of the mount point should be returned.
        If the volumes is a container and contains other (virtual) volumes (e.g. encrypted volume),
        a list of new volumes should be returned.
        """
        return None

    def unmount_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        """
        This function unmounts a filesystem.
        If the filesystem was successfully unmounted, True should be returned.
        """
        return False

    def unmount_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        """
        This function unmounts a volume.
        If the volume was successfully unmounted, True should be returned.
        If the volume was not processed, False should be returned.
        """
        return False

    def mounted_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> None:
        pass

    def before_create_disk(self, disk_builder: VirtualDiskBuilder, disk_info: DiskInfo, ctx: DiskVmCreatorContext) -> None:
        """
        This function is called, before a new disk is added to the virtual machine.
        Allows analyzing disk and filesystem contents and modify the virtual disk builder.
        """
        pass

    def before_create_vm(self, ctx: DiskVmCreatorContext) -> None:
        """
        This function is called, before the VM is created and written to the filesystem.
        Disks are already added to the VM.
        Allows modifying VM options.
        """
        pass

    def modify_disk(self, disk_info: DiskInfo, ctx: DiskVmCreatorContext) -> None:
        pass

    def modify_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> None:
        pass

    def modify_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> None:
        pass


class PluginManager:
    def __init__(self):
        self.plugins = []
        self.fallback_plugins = []

    @property
    def all_plugins(self):
        return self.plugins + self.fallback_plugins

    def add(self, *plugins: PluginSpec):
        self.plugins.extend(plugins)

    def dispatch_all(self, name, **kwargs):
        logging.debug(f'Calling {name} with {kwargs}')
        for p in self.all_plugins:
            if cb := getattr(p, name):
                cb(**kwargs)

    def dispatch_until_result(self, name, **kwargs):
        logging.debug(f'Calling {name} with {kwargs}')
        for p in self.all_plugins:
            if cb := getattr(p, name):
                if res := cb(**kwargs):
                    return res
        return None
