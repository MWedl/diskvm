import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from diskvm import utils
from diskvm.errors import VirtualizationSoftwareNotAvailable
from diskvm.utils import SizeUnit
from diskvm.vm.base import VirtualMachineBuilder, VirtualizationSoftware, VirtualMachine, VirtualDisk, DiskType, \
    FirmwareType
from diskvm.vm.vmdk import VmdkBuilder


class VmwareVirtualMachine(VirtualMachine):
    DISK_PATH_PATTERN = re.compile(rf'({"|".join([v.value for v in DiskType])})[0-9]+:[0-9]+\.fileName\s*=\s*"([^"]+)"')

    def __init__(self, virtualization_software, vmx: Path):
        super().__init__(virtualization_software)
        self.vmx = vmx

    @property
    def disks(self) -> list[VirtualDisk]:
        """
        Parse paths of VMDK files from VMX file
        When creating snapshots, new VMDK files referencing the previous one are created
        and the paths of virtual disks inside the VMX file are updated.
        """
        out = []
        with open(self.vmx, 'r') as f:
            for m in self.DISK_PATH_PATTERN.findall(f.read()):
                path = Path(m[1])
                if not path.is_absolute():
                    path = self.vmx.parent / path
                out.append(VirtualDisk(self.virtualization_software, path=path, type=DiskType(m[0])))
        return out


class VMwareVirtualMachineBuilder(VirtualMachineBuilder):
    disk_builder_type = VmdkBuilder

    def write(self, out_dir):
        # VMX specification: http://sanbarrow.com/vmx.html
        vmx = []
        vmx.extend([
            '# Static Values',
            '.encoding = "UTF-8"',
            'config.version = "8"',
            'virtualHW.version = "18"',  # VMware Workstation/Player 16.x
            f'displayName = "{self.name}"',

            # Memory and CPUs
            f'memsize = "{self.memory // SizeUnit.MB}"',
            f'numvcpus = "{self.cpus}"',
            'cpuid.coresPerSocket = "1"',

            # Firmware
            f'firmware = "{(self.firmware or FirmwareType.EFI).value}"',

            # Network adapter (disabled by default)
            'ethernet0.present = "TRUE"',
            'ethernet0.startConnected = "FALSE"',
            'ethernet0.connectionType = "nat"',
            'ethernet0.addressType = "generated"',
            # 'ethernet0.virtualDev = "e1000e',

            # Set time and disable time sync
            f'rtc.starttime = "{int(self.time.timestamp())}"',
            'tools.syncTime = "FALSE"',
            'time.synchronize.continue = "FALSE"',
            'time.synchronize.restore = "FALSE"',
            'time.synchronize.resume.disk = "FALSE"',
            'time.synchronize.resume.memory = "FALSE"',
            'time.synchronize.shrink = "FALSE"',
            'time.synchronize.tools.startup = "FALSE"',
            # Prevent user from enabling time synchronization
            'isolation.tools.setOption.disable = "TRUE"',

            # disable snapshots on vmware workstation to prevent accidental modification of original image
            'snapshot.disabled = "TRUE"',

            'floppy0.present = "FALSE"',

            # PCI bridge for PCIe support (required for NVMe)
            # https://communities.vmware.com/t5/VMware-Workstation-Pro/NO-PCIe-PCI-slots-available-when-attempting-to-add-a-NIC-to-a/td-p/2823330
            'pciBridge0.present = "TRUE"',
            'pciBridge4.present = "TRUE"',
            'pciBridge4.virtualDev = "pcieRootPort"',
            'pciBridge4.functions = "8"',
            'pciBridge5.present = "TRUE"',
            'pciBridge5.virtualDev = "pcieRootPort"',
            'pciBridge5.functions = "8"',
            'pciBridge6.present = "TRUE"',
            'pciBridge6.virtualDev = "pcieRootPort"',
            'pciBridge6.functions = "8"',
            'pciBridge7.present = "TRUE"',
            'pciBridge7.virtualDev = "pcieRootPort"',
            'pciBridge7.functions = "8"',
        ])

        # Guest OS
        vmx.append(f'guestOS = "{self.guest_os or "other-64"}"')

        # Virtual disks
        for i, db in enumerate(self.disks):
            # Write VMDK file
            d = db.write(out_dir)

            # Create VMX entry for disk
            disk_name = f'{d.type.value}0:{i}'
            vmx.extend([
                f'{disk_name}.present = "TRUE"',
                f'{disk_name}.fileName = "{d.path}"',
            ])

            # Add disk adapter once
            disk_adapter = f'{d.type.value}0.present = "TRUE"'
            if disk_adapter not in vmx:
                vmx.append(disk_adapter)

        # Write VMX file
        out_file = out_dir / (self.name + '.vmx')
        with open(out_file, 'w') as f:
            f.write('\n'.join(vmx))

        return VmwareVirtualMachine(self.virtualization_software, vmx=out_file)


class Vmware(VirtualizationSoftware):
    name = "VMwareWorkstation"

    def check_available(self):
        try:
            # Check VMware version
            version = utils.run_process(['vmware', '--version'])
            if not version.startswith(b'VMware Workstation') or int(version[19:21]) < 16:
                raise Exception('VMware version incompatible')

            # Check if CLI programs are available
            utils.run_process(['vmrun', 'list'])
            utils.run_process(['vmware-mount', '-L'])
        except Exception as ex:
            raise VirtualizationSoftwareNotAvailable() from ex

    def start(self, vm: VmwareVirtualMachine):
        utils.run_process(['vmrun', 'start', str(vm.vmx.absolute()), 'gui'])

    def is_running(self, vm: VmwareVirtualMachine):
        running_vms = [Path(p) for p in utils.run_process(['vmrun', 'list']).decode().splitlines()[1:]]
        return vm.vmx in running_vms

    def snapshot(self, vm: VmwareVirtualMachine, name: Optional[str] = None):
        name = name or f'Snapshot {datetime.now().isoformat()}'
        utils.run_process(['vmrun', 'snapshot', str(vm.vmx.absolute()), name])

    def mount_disk(self, disk: VirtualDisk, readonly=True) -> Path:
        mount_dir = Path(tempfile.mkdtemp())
        utils.run_process(['vmware-mount', *(['-r'] if readonly else []), '-f', str(disk.path), str(mount_dir)])
        return mount_dir / 'flat'

    def unmount_disk(self, disk: VirtualDisk):
        utils.run_process(['vmware-mount', '-K', str(disk.path)])

    def builder(self, name: str) -> VMwareVirtualMachineBuilder:
        return VMwareVirtualMachineBuilder(self, name)

