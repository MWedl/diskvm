import abc
import copy
import dataclasses
import logging
from datetime import datetime
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Type, Optional

from diskvm.errors import UnsupportedDiskTypeError, InvalidDiskPartError, VirtualizationSoftwareNotAvailable
from diskvm.utils import SizeUnit


class FirmwareType(Enum):
    BIOS = 'bios'
    EFI = 'efi'


class DiskType(Enum):
    IDE = 'ide'
    SATA = 'sata'
    NVMe = 'nvme'


class VirtualDisk:
    def __init__(self, virtualization_software, path: Path, type: DiskType):
        self.virtualization_software = virtualization_software
        self.path = path
        self.type = type

    def mount(self, readonly=True) -> Path:
        return self.virtualization_software.mount_disk(self, readonly=readonly)

    def unmount(self):
        self.virtualization_software.unmount_disk(self)


@dataclasses.dataclass
class VirtualDiskPart:
    source_file: Optional[Path]
    length: int
    source_offset: int = 0
    target_offset: int = 0


class VirtualDiskBuilder(abc.ABC):
    """
    Representation of a virtual disk
    """
    def __init__(self, virtualization_software, name: str):
        self.virtualization_software = virtualization_software
        self.name = name
        self.disk_parts: list[VirtualDiskPart] = []

        # Disk properties
        self.type: DiskType = DiskType.SATA
        self.sector_size = 512

    def add_part(self, part: VirtualDiskPart):
        """
        Add a part of the virtual disk (e.g. header, volume) stored in an external file
        """
        if not part.source_file.is_file() and not part.source_file.is_block_device():
            raise InvalidDiskPartError(part, "Invalid file")
        if part.target_offset % self.sector_size != 0 or (part.target_offset + part.length) % self.sector_size != 0:
            raise InvalidDiskPartError(part, "Not aligned to sectors")

        # Merge disk parts
        for p in list(self.disk_parts):
            if (p.target_offset < part.target_offset and p.target_offset + p.length <= part.target_offset) or \
                    (p.target_offset >= part.target_offset + part.length):
                # No overlapping areas
                pass
            elif p.target_offset >= part.target_offset and p.target_offset + p.length <= part.target_offset + part.length:
                # p:    | _____ | _aaa_ | _____ |
                # part: | _____ | bbbbb | _____ |
                # Completely replace part
                self.disk_parts.remove(p)
            elif p.target_offset < part.target_offset < p.target_offset + p.length <= part.target_offset + part.length:
                # p:    | __aaa | aa___ | _____ |
                # part: | _____ | bbbbb | _____ |
                overlap_len = (p.target_offset + p.length) - part.target_offset
                p.length -= overlap_len
            elif part.target_offset < p.target_offset < part.target_offset + part.length < p.target_offset + p.length:
                # p:    | _____ | __aaa | aaa__ |
                # part: | _____ | bbbbb | _____ |
                overlap_len = (p.target_offset + p.length) - (part.target_offset + part.length)
                p.target_offset += overlap_len
                p.source_offset += overlap_len
                p.length -= overlap_len
            elif p.target_offset < part.target_offset and p.target_offset + p.length > part.target_offset + part.length:
                # p:    | ___aa | aaaaa | aa___ |
                # part: | _____ | bbbbb | _____ |
                p2 = copy.copy(p)

                p.length = part.target_offset - p.target_offset

                p2.length = (p2.target_offset + p2.length) - (part.target_offset + part.length)
                p2.target_offset = part.target_offset + part.length
                p2.source_offset += p.length + part.length
                self.disk_parts.append(p2)

        self.disk_parts.append(part)
        self.disk_parts.sort(key=lambda p: p.target_offset)

    @abc.abstractmethod
    def write(self, out_dir: Path) -> VirtualDisk:
        """
        Write virtual disk to the filesystem
        """
        pass


class VirtualMachine(abc.ABC):
    def __init__(self, virtualization_software):
        self.virtualization_software = virtualization_software

    def start(self):
        return self.virtualization_software.start(self)

    def is_running(self):
        return self.virtualization_software.is_running(self)

    def snapshot(self, name=None):
        logging.info(f'Creating VM snapshot. name={name}')
        return self.virtualization_software.snapshot(self, name=name)

    @property
    @abc.abstractmethod
    def disks(self) -> list[VirtualDisk]:
        pass


class VirtualMachineBuilder(abc.ABC):
    def __init__(self, virtualization_software, name: str):
        self.virtualization_software = virtualization_software
        self.name = name
        self.disks = []

        self.guest_os = None
        self.firmware: Optional[FirmwareType] = None
        self.memory = 4 * SizeUnit.GB
        self.cpus = 2
        self.time = datetime.now()

    @property
    @abc.abstractmethod
    def disk_builder_type(self) -> Type[VirtualDiskBuilder]:
        pass

    def new_disk(self) -> VirtualDiskBuilder:
        return self.disk_builder_type(virtualization_software=self.virtualization_software, name=f'{self.name}-disk{len(self.disks) + 1}')

    def add_disk(self, disk_builder):
        if not isinstance(disk_builder, self.disk_builder_type):
            raise UnsupportedDiskTypeError()

        self.disks.append(disk_builder)

    @abc.abstractmethod
    def write(self, out_dir: Path) -> VirtualMachine:
        pass


class VirtualizationSoftware(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        pass

    @cached_property
    def is_available(self) -> bool:
        try:
            self.check_available()
            return True
        except VirtualizationSoftwareNotAvailable:
            return False

    @abc.abstractmethod
    def check_available(self):
        """
        Check if virtualization software is installed on the host system
        :raise VirtualizationSoftwareNotAvailable
        """
        pass

    @abc.abstractmethod
    def start(self, vm: VirtualMachine):
        pass

    @abc.abstractmethod
    def is_running(self, vm: VirtualMachine):
        pass

    @abc.abstractmethod
    def snapshot(self, vm: VirtualMachine, name: Optional[str] = None):
        pass

    @abc.abstractmethod
    def mount_disk(self, disk: VirtualDisk, readonly=True) -> Path:
        pass

    @abc.abstractmethod
    def unmount_disk(self, disk: VirtualDisk):
        pass

    @abc.abstractmethod
    def builder(self, name: str) -> VirtualMachineBuilder:
        pass
