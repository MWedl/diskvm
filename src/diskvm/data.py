import dataclasses
from contextlib import ExitStack
from enum import Enum
from pathlib import Path
from typing import Optional, Any

from pyreadpartitions import get_disk_partitions_info

import diskvm
from diskvm.utils import SizeUnit
from diskvm.vm.base import VirtualMachineBuilder, VirtualMachine, FirmwareType


@dataclasses.dataclass
class DiskVmCreatorOptions:
    """
    Directory to write all VM configuration files.
    """
    out_dir: Path

    """
    The main disk image containing the operating system.
    """
    disk_image: Path

    """
    List of secondary disk images
    """
    additional_disk_images: list[Path] = dataclasses.field(default_factory=list)

    name: str = 'firstVm'

    """
    Identifier for the guest OS. Identifier depends on the underlying virtualization software.
    Can be auto-detected by plugins.
    """
    guest_os: Optional[str] = None

    """
    BIOS or EFI. Can be auto-detected by plugins.
    """
    firmware: Optional[FirmwareType] = None

    """
    VM memory in bytes
    """
    memory: int = 4 * SizeUnit.GB

    """
    Number of virtual CPUs for the VM
    """
    num_cpus: int = 2

    start_vm: bool = True

    """
    Additional options that may be required by plugins.
    """
    additional_options: dict = dataclasses.field(default_factory=dict)

    @property
    def all_disk_images(self):
        return [self.disk_image] + self.additional_disk_images


@dataclasses.dataclass()
class VolumeInfo:
    disk_info: 'DiskInfo' = dataclasses.field(repr=False)
    flat_mount: Optional[Path] = dataclasses.field(default=None, repr=True)
    filesystem_mount: Optional[Path] = dataclasses.field(default=None, repr=True)
    parent: Optional['VolumeInfo'] = dataclasses.field(default=None, repr=False)
    offset: int = dataclasses.field(default=0, repr=False)
    size: int = dataclasses.field(default=0, repr=False)
    volume_type: Optional[Any] = dataclasses.field(default=None, repr=False)
    volume_info: Optional[Any] = dataclasses.field(default=None, repr=False)
    additional_info: dict = dataclasses.field(default_factory=dict, repr=False)


class PartitionScheme(Enum):
    MBR = 'mbr'
    GPT = 'gpt'
    UNKNOWN = 'unknown'


@dataclasses.dataclass()
class DiskInfo:
    readonly: bool = dataclasses.field(repr=True)
    flat_mounted_disk: Path = dataclasses.field(repr=True)
    disk_info: Any = dataclasses.field(init=False, repr=False)
    volumes: list[VolumeInfo] = dataclasses.field(default_factory=list, repr=False)
    additional_info: dict = dataclasses.field(default_factory=dict, repr=False)
    keep_mounted: bool = dataclasses.field(default=False, repr=False)

    @property
    def partition_scheme(self) -> PartitionScheme:
        if self.disk_info.gpt:
            return PartitionScheme.GPT
        elif self.disk_info.mbr:
            return PartitionScheme.MBR
        else:
            return PartitionScheme.UNKNOWN

    @property
    def sector_size(self) -> int:
        if self.partition_scheme == PartitionScheme.GPT:
            return self.disk_info.gpt.lba_size
        elif self.partition_scheme == PartitionScheme.MBR:
            return self.disk_info.mbr.lba_size
        else:
            return 512

    def refresh_disk_info(self):
        with open(self.flat_mounted_disk, 'rb') as f:
            self.disk_info = get_disk_partitions_info(f)

    def __post_init__(self):
        self.refresh_disk_info()


@dataclasses.dataclass()
class DiskVmCreatorContext:
    instance: 'diskvm.runner.DiskVmCreator' = dataclasses.field(repr=False)
    options: DiskVmCreatorOptions = dataclasses.field(repr=False)
    vm_builder: Optional[VirtualMachineBuilder] = dataclasses.field(default=None, repr=False)
    vm: Optional[VirtualMachine] = dataclasses.field(default=None, repr=False)
    mount_contexts: ExitStack = dataclasses.field(default_factory=ExitStack, repr=False)
    additional_info: dict = dataclasses.field(default_factory=dict, repr=False)

