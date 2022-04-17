import itertools
import logging
import shutil
from pathlib import Path

import click

from diskvm.utils import ChoiceMap, BytesParamType, SizeParamType
from diskvm.vm.base import FirmwareType
from diskvm.plugins.luks import LuksAddPasswordPlugin, LuksOnTheFlyDecryptPlugin
from diskvm.plugins.os_detect import DetectOperatingSystemPlugin, DetectEfiPlugin
from diskvm.plugins.password_bypass import WindowsRegistryOverridePasswordPlugin, EtcShadowBlankPasswords
from diskvm.plugins.veracrypt import VeraCryptOnTheFlyDecryptPlugin, \
    VeraCryptOverridePasswordPlugin
from diskvm.plugins.bitlocker import BitLockerOverridePasswordPlugin, BitLockerOnTheFlyDecryptPlugin
from diskvm.runner import DiskVmCreator, DiskVmCreatorOptions
from diskvm.vm.vmware import Vmware


VIRTUALIZATION_SOFTWARE = {
    'vmware': Vmware(),
}
PASSWORD_BYPASS_OPTIONS = {
    'auto': [EtcShadowBlankPasswords(), WindowsRegistryOverridePasswordPlugin()],
    'none': [],
    'linux': [EtcShadowBlankPasswords()],
    'windows': [WindowsRegistryOverridePasswordPlugin()],
}
FDE_BYPASS_OPTIONS = {
    'none': [],
    'auto': [BitLockerOverridePasswordPlugin(), LuksAddPasswordPlugin(), VeraCryptOverridePasswordPlugin()],
    'bitlocker_otf_mount': [BitLockerOnTheFlyDecryptPlugin()],
    'bitlocker_add_clearkey': [BitLockerOverridePasswordPlugin()],
    'luks_add_pw': [LuksAddPasswordPlugin()],
    'luks_otf_mount': [LuksOnTheFlyDecryptPlugin()],
    'veracrypt_otf_mount': [VeraCryptOnTheFlyDecryptPlugin()],
    'veracrypt_overwrite_pw': [VeraCryptOverridePasswordPlugin()],
}


def to_path(ctx, param, value: str) -> Path:
    return Path(value)


@click.command()
@click.argument('disk_image', required=True, type=click.Path(exists=True, readable=True, file_okay=True, dir_okay=False), callback=to_path)
@click.option('--out-dir', required=True, type=click.Path(exists=False, writable=True, dir_okay=True, file_okay=False), callback=to_path)
@click.option('--name', type=click.STRING, default='')
@click.option('--start-vm/--no-start-vm', is_flag=True, default=True, help='Start the created VM after writing its configuration')
@click.option('--virtualization-software', type=ChoiceMap(choices=VIRTUALIZATION_SOFTWARE), default='vmware')
@click.option('--vm-memory', type=SizeParamType(), default='4GB')
@click.option('--vm-cpus', type=click.IntRange(min=1), default=2)
@click.option('--guest-os', type=click.STRING, default='auto')
@click.option('--firmware', type=click.Choice(choices=['auto', FirmwareType.BIOS.value, FirmwareType.EFI.value]), default='auto')
@click.option('--pw-bypass', type=ChoiceMap(choices=PASSWORD_BYPASS_OPTIONS), default='auto')
@click.option('--fde-bypass', type=ChoiceMap(choices=FDE_BYPASS_OPTIONS), default='none')
@click.option('--master-key', type=BytesParamType(), multiple=True, required=False)
@click.option('--master-keys-file', type=click.File(mode='r'), required=False, help='File containing possible master keys. One key per line as hex string.')
@click.option('--xts-combine-keys', type=click.BOOL, default=True, help='Combine each key with every other to build possible XTS mode keys when keys extracted from a memory dump are provided.')
@click.option('-v', '--verbose', count=True, default=0)
def main(disk_image: Path, out_dir: Path, name, start_vm, virtualization_software, vm_memory, vm_cpus, guest_os, firmware,
         pw_bypass, fde_bypass, master_key, master_keys_file, xts_combine_keys, verbose):
    # Set up logging
    logging.basicConfig(level=logging.DEBUG if verbose >= 2 else logging.INFO if verbose >= 1 else logging.WARNING)

    out_dir.mkdir(exist_ok=True)
    # # Clear output directory
    # for f in out_dir.iterdir():
    #     if f.is_dir():
    #         shutil.rmtree(f)
    #     else:
    #         f.unlink()

    # Create a list of possible master keys
    master_keys = set(master_key or [])
    if master_keys_file:
        master_keys |= set(map(bytes.fromhex, filter(None, master_keys_file.read().splitlines())))
    if xts_combine_keys:
        # Combine keys of same length to possible XTS keys
        master_keys |= set(map(lambda t: t[0] + t[1], filter(lambda t: len(t[0]) == len(t[1]), itertools.permutations(master_keys, 2))))

    # Configure plugins and options from CLI parameters
    creator = DiskVmCreator(virtualization_software)
    creator.plugins.add(*pw_bypass)
    creator.plugins.add(*fde_bypass)
    options = DiskVmCreatorOptions(
        out_dir=out_dir,
        disk_image=disk_image,
        name=name or disk_image.name.split('.')[0],
        memory=vm_memory,
        num_cpus=vm_cpus,
        start_vm=start_vm,
        additional_options={
            'master_keys': master_keys,
        }
    )
    if guest_os == 'auto':
        creator.plugins.add(DetectOperatingSystemPlugin())
    else:
        options.guest_os = guest_os
    if firmware == 'auto':
        creator.plugins.add(DetectEfiPlugin())
    else:
        options.firmware = FirmwareType(firmware)

    # Create VM and run it
    creator.run(options)


if __name__ == '__main__':
    main()




