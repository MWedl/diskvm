import math
from pathlib import Path

from diskvm.vm.base import VirtualDisk, DiskType, VirtualDiskBuilder


class VmdkBuilder(VirtualDiskBuilder):
    # Constants values, not relevant anymore:
    #   https://kb.vmware.com/s/article/1026254
    #   http://sanbarrow.com/vmdk-basics.html#syntax
    BYTES_PER_SECTOR = 512
    NUM_SECTORS = 63
    NUM_CYLINDERS = 255

    @property
    def adapter_type(self) -> str:
        return 'ide' if self.type == DiskType.IDE else \
               'lsilogic'

    def write(self, out_dir: Path) -> VirtualDisk:
        # VMDK specification: https://www.vmware.com/support/developer/vddk/vmdk_50_technote.pdf
        out_path = out_dir / (self.name + '.vmdk')
        with open(out_path, 'w') as f:
            vmdk = []
            vmdk.extend([
                '# Disk Descriptor File',
                'version=1',
                'CID=fffffffe',  # Newly created base disk
                'parentCID=ffffffff',  # No parent disk
                f'createType="monolithicFlat"',
                '',
            ])

            # Add disk parts from external files
            vmdk.append('# Extent description')
            current_sector = 0
            for p in self.disk_parts:
                # Unallocated space
                if current_sector * self.sector_size != p.target_offset:
                    num_padding_sectors = (p.target_offset // self.sector_size) - current_sector
                    current_sector += num_padding_sectors
                    vmdk.append(f'RW {num_padding_sectors} ZERO')

                # Add file
                size_in_sectors = p.length // self.sector_size
                current_sector += size_in_sectors
                if p.source_file is None:
                    vmdk.append(f'RW {size_in_sectors} ZERO')
                else:
                    # Relative path if possible
                    path = p.source_file.absolute()
                    if p.source_file.is_relative_to(out_dir):
                        p = p.source_file.relative_to(out_dir)
                    vmdk.append(f'RW {size_in_sectors} FLAT "{path}" {p.source_offset // self.sector_size}')

            # RW <sectors> ZERO
            vmdk.append('')

            # Disk properties
            vmdk.extend([
                '# DDB - Disk Data Base',
                f'ddb.adapterType="{self.adapter_type}"',
                f'ddb.geometry.sectors="{self.NUM_SECTORS}"',
                f'ddb.geometry.heads="{self.NUM_CYLINDERS}"',
                f'ddb.geometry.cylinders="{math.ceil(current_sector / (self.NUM_CYLINDERS * self.NUM_SECTORS))}"',
                'ddb.virtualHWVersion="18"',  # VMware Workstation/Player 16.x
            ])
            f.write('\n'.join(vmdk))

        return VirtualDisk(self.virtualization_software, path=out_path, type=self.type)

