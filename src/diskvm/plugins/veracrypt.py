import dataclasses
import enum
import hashlib
import logging
import random
import shutil
import string
import tempfile
from binascii import crc32
from enum import Enum
from pathlib import Path
from typing import Union, Optional
from cryptography.hazmat.primitives.ciphers import algorithms, modes, Cipher

from diskvm.data import DiskInfo, DiskVmCreatorContext, VolumeInfo
from diskvm.errors import DiskVmError
from diskvm.plugins import utils
from diskvm.plugins.base import PluginSpec
from diskvm.plugins.os_detect import DetectEfiPlugin
from diskvm.structure import Structure, bytes_field, Uint16, Uint32, Uint64, Endian
from diskvm.utils import run_process
from diskvm.vm.base import VirtualDiskBuilder, VirtualDiskPart


class VeraCryptCipher(Enum):
    AES256_XTS = 'aes-xts-plain64'


class VeraCryptFlags(enum.Flag):
    SYSTEM_ENCRYPTION = 1
    # non-system in-place-encrypted/decrypted volume
    NON_SYSTEM_IN_PLACE_ENCRYPTED = 2


@dataclasses.dataclass()
class VeraCryptHeader(Structure):
    endian = Endian.BIG

    signature: bytes = bytes_field(size=4, default=b'VERA')
    header_format_version: Uint16 = 5
    min_program_version: Uint16 = 0x10b
    # CRC-32 checksum of the (decrypted) bytes 256-511
    checksum_master_keys: Uint32 = 0
    reserved1: bytes = bytes_field(size=16, default=b'\x00' * 16)
    size_hidden_volume: Uint64 = 0
    size_volume: Uint64 = 0
    #  Byte offset of the start of the master key scope
    offset: Uint64 = 0
    # Size of the encrypted area within the master key scope
    size_encrypted: Uint64 = 0
    flags: Uint32 = 0
    sector_size: Uint32 = 512
    reserved2: bytes = bytes_field(size=120, default=b'\x00' * 120)
    checksum_header_fields: Uint32 = 0
    master_keys: bytes = bytes_field(size=256, default=b'\x00' * 256)

    def update_checksums(self):
        # Calculate checksums
        self.checksum_master_keys = Uint32(crc32(self.master_keys))
        header_data = self.pack()
        # Checksum of header fields (includes checksum of master keys)
        self.checksum_header_fields = Uint32(crc32(header_data[:188]))

    def encrypt(self, password: str, cipher: VeraCryptCipher = VeraCryptCipher.AES256_XTS, salt: bytes = None):
        cipher_instance, key_length = {
            VeraCryptCipher.AES256_XTS: (algorithms.AES, 64),
        }[cipher]

        # Derive header key from password
        # Generate a new password (does not matter which)
        # Use SHA512 (arbitrary algorithm) and no PIM
        salt = salt or random.randbytes(64)
        header_key = hashlib.pbkdf2_hmac('SHA512', password=password.encode('ASCII'), salt=salt, iterations=500000,
                                         dklen=key_length)

        # Encrypt header
        header_data = self.pack()
        enc = Cipher(cipher_instance(key=header_key), modes.XTS(b'\x00' * 16)).encryptor()
        encrypted = enc.update(header_data) + enc.finalize()
        return salt + encrypted


@dataclasses.dataclass
class VeraCryptMasterKey:
    key: bytes
    cipher: VeraCryptCipher = VeraCryptCipher.AES256_XTS


def unmount_veracrypt(mount_point: Union[Path, VolumeInfo]) -> bool:
    if isinstance(mount_point, VolumeInfo) and mount_point.additional_info.get('veracrypt', {}).get('mounted'):
        mount_point = mount_point.flat_mount

    if not isinstance(mount_point, Path) or not mount_point.exists():
        return False

    try:
        run_process(['cryptsetup', 'close', mount_point.name])
        return True
    except DiskVmError:
        return False


def try_mount_veracrypt(volume_info: VolumeInfo, master_key: VeraCryptMasterKey) -> Optional[VolumeInfo]:
    mount_device = None
    try:
        volume_name = 'veracrypt-' + ''.join(random.choices(string.ascii_letters, k=10))
        with tempfile.NamedTemporaryFile('wb') as mk_file:
            mk_file.write(master_key.key)
            mk_file.flush()
            run_process(['cryptsetup', 'open', '--type=plain', *(['--readonly'] if volume_info.disk_info.readonly else []),
                         '--cipher', master_key.cipher.value,
                         '--key-file', mk_file.name, '--key-size', str(len(master_key.key) * 8),
                         '--skip', str(volume_info.offset // volume_info.disk_info.sector_size),
                         volume_info.flat_mount, volume_name])
            mount_device = Path('/dev/mapper') / volume_name

        # Check if filesystem signature is NTFS
        # VeraCrypt system encryption is currently only supported on Windows and
        # NTFS is the default filesystem.
        if utils.is_ntfs(mount_device):
            return VolumeInfo(
                disk_info=volume_info.disk_info,
                flat_mount=mount_device,
                parent=volume_info,
                additional_info={'veracrypt': {'mounted': True}}
            )
        else:
            unmount_veracrypt(mount_device)
            return None
    except DiskVmError:
        unmount_veracrypt(mount_device)
        return None


def find_master_key(volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[VeraCryptMasterKey]:
    if key := volume_info.additional_info.get('veracrypt', {}).get('master_key'):
        return key

    for key in ctx.options.additional_options.get('master_keys', []):
        for cipher in list(VeraCryptCipher):
            master_key = VeraCryptMasterKey(key=key, cipher=cipher)
            if mount_point := try_mount_veracrypt(volume_info=volume_info, master_key=master_key):
                unmount_veracrypt(mount_point)
                return master_key
    return None


class VeraCryptMountPlugin(PluginSpec):
    def mounted_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        if DetectEfiPlugin.is_efi_system_partition(volume_info) and \
                (volume_info.filesystem_mount / 'EFI' / 'Veracrypt').is_dir():
            logging.info(f'Detected VeraCrypt system encryption on {volume_info.disk_info}')
            volume_info.disk_info.additional_info.setdefault('veracrypt', {})['system_encryption_enabled'] = True

    def mount(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> Optional[Union[Path, VolumeInfo, list[VolumeInfo]]]:
        if not volume_info.disk_info.additional_info.get('veracrypt', {}).get('system_encryption_enabled'):
            return None

        logging.info(f'Try mounting possible VeraCrypt volume {volume_info}')

        if mk := find_master_key(volume_info=volume_info, ctx=ctx):
            volume_info.additional_info.setdefault('veracrypt', {}).update({
                'enabled': True,
                'master_key': mk,
            })
            return try_mount_veracrypt(volume_info=volume_info, master_key=mk)

        return None

    def unmount_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext) -> bool:
        return unmount_veracrypt(volume_info)


class VeraCryptOverridePasswordPlugin(VeraCryptMountPlugin):
    NEW_PASSWORD = 'newpwd'

    @classmethod
    def create_encrypted_veracrypt_header(cls, system_partition: VolumeInfo, master_key: VeraCryptMasterKey, password: str) -> bytes:
        # Create header
        header = VeraCryptHeader(
            sector_size=Uint32(512),
            size_volume=Uint64(system_partition.size),
            size_encrypted=Uint64(system_partition.size),
            offset=Uint64(system_partition.offset),
            flags=Uint32(VeraCryptFlags.SYSTEM_ENCRYPTION.value),
            master_keys=master_key.key.ljust(256, b'\x00')
        )
        header.update_checksums()
        return header.encrypt(password=password, cipher=master_key.cipher)

    def modify_volume(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        if not volume_info.disk_info.additional_info.get('veracrypt', {}).get('system_encryption_enabled') or \
                not volume_info.additional_info.get('veracrypt', {}).get('mounted') or \
                not volume_info.parent.additional_info.get('veracrypt', {}).get('enabled') or \
                not volume_info.parent.additional_info.get('veracrypt', {}).get('master_key'):
            return

        logging.info(f'Overriding VeraCrypt system encryption header {volume_info.disk_info}')
        with open(volume_info.disk_info.flat_mounted_disk, 'rb+') as f:
            f.seek(62 * volume_info.disk_info.sector_size)
            f.write(self.create_encrypted_veracrypt_header(
                system_partition=volume_info.parent,
                master_key=volume_info.parent.additional_info['veracrypt']['master_key'],
                password=self.NEW_PASSWORD
            ))
        logging.info(f'Changed VeraCrypt password to "{self.NEW_PASSWORD}" {volume_info.disk_info}')


class VeraCryptOnTheFlyDecryptPlugin(VeraCryptMountPlugin):
    def before_create_disk(self, disk_builder: VirtualDiskBuilder, disk_info: DiskInfo, ctx: DiskVmCreatorContext):
        for volume_info in disk_info.volumes:
            # Check if volume is VeraCrypt-encrypted and could be mounted
            if not volume_info.additional_info.get('veracrypt', {}).get('mounted', False) or \
                    not volume_info.parent.additional_info.get('veracrypt', {}).get('enabled', False):
                continue

            # Replace encrypted VeraCrypt volume with plaintext on-the-fly decrypted and mounted volume in virtual disk
            disk_builder.add_part(VirtualDiskPart(
                source_file=volume_info.flat_mount,
                source_offset=0,
                target_offset=volume_info.parent.offset,
                length=volume_info.size,
            ))
            # Set disk and volumes to mounted while the VM runs
            disk_info.keep_mounted = True

    def modify_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        if not volume_info.disk_info.additional_info.get('veracrypt', {}).get('system_encryption_enabled') or \
                not DetectEfiPlugin.is_efi_system_partition(volume_info):
            return

        logging.info(f'Replacing VeraCrypt bootloader with backed-up original bootloader')
        boot_dir = volume_info.filesystem_mount / 'EFI' / 'Boot'

        bootloader_backup = boot_dir / 'original_bootx64.vc_backup'
        if bootloader_backup.exists():
            shutil.move(bootloader_backup, boot_dir / 'bootx64.efi')

        bootloader_backup = boot_dir / 'original_bootia32.vc_backup'
        if bootloader_backup.exists():
            shutil.move(bootloader_backup, boot_dir / 'bootia32.efi')


