import contextlib
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from diskvm.data import DiskVmCreatorOptions, DiskVmCreatorContext, DiskInfo, VolumeInfo, PartitionScheme
from diskvm.errors import InvalidDiskError, DiskVmError
from diskvm.plugins.base import PluginManager
from diskvm.plugins.generic import GenericMountPlugin, LvmMountPlugin
from diskvm.utils import run_process, groupby, size_blockdevice
from diskvm.vm.base import VirtualizationSoftware, VirtualDisk, VirtualMachine, VirtualDiskPart


class DiskVmCreator:
    def __init__(self, virtualization_software: VirtualizationSoftware):
        self.virtualization_software = virtualization_software

        # Initialize plugin system
        self.plugins = PluginManager()
        self._init_default_plugins()

    def _init_default_plugins(self):
        self.plugins.fallback_plugins.append(GenericMountPlugin())
        self.plugins.fallback_plugins.append(LvmMountPlugin())

    def _create_context(self, options: DiskVmCreatorOptions):
        return DiskVmCreatorContext(instance=self, options=options)

    def _validate_options(self, options: DiskVmCreatorOptions):
        # Ensure virtualization software is installed on host system
        self.virtualization_software.check_available()

        # Check that disk images exist and are either files or attached disks (block devices)
        for disk_image in options.all_disk_images:
            if not disk_image.exists() or (not disk_image.is_file() and not disk_image.is_block_device()):
                raise InvalidDiskError(disk_image)

        # Create output directory
        options.out_dir.mkdir(exist_ok=True)

    def _add_virtual_disk(self, disk_image: Path, ctx: DiskVmCreatorContext):
        with contextlib.ExitStack() as mounts:
            logging.info(f'Begin building virtual disk for {disk_image}')

            logging.info(f'Mounting disk read-only {disk_image}')
            disk_info = mounts.enter_context(self.mount_disk_image(disk_image, ctx))

            disk_builder = ctx.vm_builder.new_disk()
            disk_builder.sector_size = disk_info.sector_size
            disk_builder.add_part(VirtualDiskPart(source_file=disk_image, length=size_blockdevice(disk_image)))

            # Mount volumes and filesystems and analyze contents before adding disk to VM
            logging.info(f'Mounting partitions and filesystems of disk image {disk_image}')
            self.plugins.dispatch_all('mounted_disk', disk_info=disk_info, ctx=ctx)

            # Keep disk mounted
            mounts.enter_context(self.mount_partitions_and_filesystems(disk_info=disk_info, ctx=ctx))
            self.plugins.dispatch_all('before_create_disk', disk_builder=disk_builder, disk_info=disk_info, ctx=ctx)

            logging.info(f'Finished building virtual disk for {disk_info}')
            ctx.vm_builder.add_disk(disk_builder)

            if disk_info.keep_mounted:
                # Keep disks and volumes mounted while VM is running
                # Transfer to mount context manager to outer context manager
                ctx.mount_contexts.enter_context(mounts.pop_all())

    def _create_vm(self, ctx: DiskVmCreatorContext) -> VirtualMachine:
        logging.info('Initializing VM builder')
        ctx.vm_builder = self.virtualization_software.builder(ctx.options.name)
        ctx.vm_builder.memory = ctx.options.memory
        ctx.vm_builder.cpus = ctx.options.num_cpus
        ctx.vm_builder.guest_os = ctx.options.guest_os
        ctx.vm_builder.firmware = ctx.options.firmware

        # Add disk images
        for disk_image in ctx.options.all_disk_images:
            self._add_virtual_disk(disk_image=disk_image, ctx=ctx)

        # Write VM to filesystem
        self.plugins.dispatch_all('before_create_vm', ctx=ctx)
        logging.info('Writing VM configuration')
        vm = ctx.vm_builder.write(out_dir=ctx.options.out_dir)
        ctx.vm_builder = None
        logging.info('Successfully created VM')
        return vm

    @contextlib.contextmanager
    def mount_disk(self, disk: VirtualDisk, readonly: bool, ctx: DiskVmCreatorContext):
        disk_info = None
        try:
            disk_info = DiskInfo(flat_mounted_disk=disk.mount(readonly=readonly), readonly=readonly)

            self.plugins.dispatch_all('mounted_disk', disk_info=disk_info, ctx=ctx)
            if not disk_info.readonly:
                self.plugins.dispatch_all('modify_disk', disk_info=disk_info, ctx=ctx)
                disk_info.refresh_disk_info()

            yield disk_info
        finally:
            if disk_info and disk_info.flat_mounted_disk.exists():
                disk.unmount()
                disk_info.flat_mounted_disk.parent.rmdir()

    @contextlib.contextmanager
    def mount_disk_image(self, disk_image: Path, ctx: DiskVmCreatorContext):
        mount_point = None
        try:
            fd, mount_point = tempfile.mkstemp()
            mount_point = Path(mount_point)
            os.close(fd)
            run_process(['mount', '--read-only', '--bind', str(disk_image), str(mount_point)])

            disk_info = DiskInfo(flat_mounted_disk=mount_point, readonly=True)
            self.plugins.dispatch_all('mounted_disk', disk_info=disk_info, ctx=ctx)

            yield disk_info
        finally:
            if mount_point and mount_point.exists():
                try:
                    run_process(['umount', str(mount_point)])
                except DiskVmError:
                    pass
                mount_point.unlink()

    @contextlib.contextmanager
    def mount_partitions_and_filesystems(self, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        try:
            self._mount_partitions(disk_info=disk_info, ctx=ctx)
            self._mount_filesystems(disk_info=disk_info, ctx=ctx)
            yield disk_info
        finally:
            self.unmount_partitions_and_filesystems(disk_info=disk_info, ctx=ctx)

    def unmount_partitions_and_filesystems(self, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        def volume_depth(v: VolumeInfo):
            return 0 if not v.parent else 1 + volume_depth(v.parent)

        # Unmount filesystems and volumes
        # First unmount nested (child) volumes, then parent volumes
        for depth, volumes in groupby(disk_info.volumes, key=volume_depth, reverse=True):
            for v in volumes:
                if v.filesystem_mount and v.filesystem_mount.exists():
                    try:
                        if self.plugins.dispatch_until_result('unmount_filesystem', volume_info=v, ctx=ctx):
                            v.filesystem_mount = None
                        else:
                            logging.warning(f'Could not unmount filesystem {v}')
                    except DiskVmError:
                        logging.exception(f'Error while unmounting filesystem {v}')

            for v in volumes:
                if v.flat_mount and v.flat_mount.exists():
                    try:
                        if self.plugins.dispatch_until_result('unmount_volume', volume_info=v, ctx=ctx):
                            v.flat_mount = None
                        else:
                            logging.warning(f'Could not unmount volume {v}')
                    except DiskVmError:
                        logging.exception(f'Error while unmounting volume {v}')

    def _add_partition_info(self, disk_info: DiskInfo, offset: int, size: int,
                            volume_type: Any, volume_info: Optional[Any], ctx: DiskVmCreatorContext):
        loop_dev = Path(run_process([
            'losetup', '--find', '--show', *(['--read-only'] if disk_info.readonly else []),
            '--offset', str(offset), '--sizelimit', str(size),
            str(disk_info.flat_mounted_disk)
        ]).decode().strip('\n'))

        volume_info = VolumeInfo(
            disk_info=disk_info, flat_mount=loop_dev, filesystem_mount=None, parent=None,
            offset=offset, size=size, volume_type=volume_type, volume_info=volume_info,
        )
        volume_info.disk_info.volumes.append(volume_info)

        self.plugins.dispatch_all('mounted_volume', volume_info=volume_info, ctx=ctx)
        if not volume_info.disk_info.readonly:
            self.plugins.dispatch_all('modify_volume', volume_info=volume_info, ctx=ctx)

    def _mount_partitions(self, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        logging.info(f'Mounting partitions of {disk_info}')

        if disk_info.partition_scheme == PartitionScheme.GPT:
            for p in disk_info.disk_info.gpt.partitions:
                self._add_partition_info(
                    disk_info=disk_info,
                    offset=p.first_lba * disk_info.sector_size,
                    size=(p.last_lba - p.first_lba + 1) * disk_info.sector_size,
                    volume_type=p.guid,
                    volume_info=p,
                    ctx=ctx
                )
        elif disk_info.partition_scheme == PartitionScheme.MBR:
            for p in disk_info.disk_info.mbr.partitions:
                self._add_partition_info(
                    disk_info=disk_info,
                    offset=p.lba * disk_info.sector_size,
                    size=p.sectors * disk_info.sector_size,
                    volume_type=p.type,
                    volume_info=p,
                    ctx=ctx
                )

    def _mount_filesystems(self, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        logging.info(f'Mounting filesystems of {disk_info}')

        idx = 0
        while idx < len(disk_info.volumes):
            p = disk_info.volumes[idx]
            if p.flat_mount:
                res = self.plugins.dispatch_until_result('mount', volume_info=p, ctx=ctx)
                if isinstance(res, VolumeInfo):
                    res = [res]
                if isinstance(res, Path):
                    # Filesystem mounted
                    p.filesystem_mount = res
                    self.plugins.dispatch_all('mounted_filesystem', volume_info=p, ctx=ctx)
                    if not disk_info.readonly:
                        self.plugins.dispatch_all('modify_filesystem', volume_info=p, ctx=ctx)
                elif isinstance(res, list):
                    # New (virtual) volumes discovered
                    for new_p in res:
                        if not new_p.parent:
                            new_p.parent = p
                        if not new_p.size:
                            new_p.size = size_blockdevice(new_p.flat_mount)
                        disk_info.volumes.append(new_p)

                        self.plugins.dispatch_all('mounted_volume', volume_info=new_p, ctx=ctx)
                        if not disk_info.readonly:
                            self.plugins.dispatch_all('modify_volume', volume_info=new_p, ctx=ctx)
                else:
                    # Could not mount
                    logging.warning(f'Could not mount filesystem on volume {p}')
                    pass
            idx += 1

    def run(self, options: DiskVmCreatorOptions):
        self._validate_options(options=options)
        ctx = self._create_context(options=options)
        with ctx.mount_contexts:
            ctx.vm = self._create_vm(ctx=ctx)

            # Create initial snapshot to not accidentally modify disk images
            ctx.vm.snapshot('Initial')

            # Modify disk
            for d in ctx.vm.disks:
                with self.mount_disk(disk=d, readonly=False, ctx=ctx) as disk_info:
                    with self.mount_partitions_and_filesystems(disk_info=disk_info, ctx=ctx):
                        # modify_* plugin hooks called during mounting
                        logging.info(f'Changes to virtual disk done {disk_info}')

            ctx.vm.snapshot('InitFinished')

            if ctx.options.start_vm:
                # Start VM
                ctx.vm.start()
                # Wait until finished
                while ctx.vm.is_running():
                    time.sleep(5)

