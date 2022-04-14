import copy
import crypt
import logging
import struct

from Cryptodome.Cipher import DES, AES, ARC4
from Cryptodome.Util.Padding import pad
from Registry.Registry import Registry
from impacket.examples.secretsdump import SAMHashes, LocalOperations, USER_ACCOUNT_V, SAM_HASH, SAM_HASH_AES
from impacket.ntlm import compute_nthash

from diskvm.data import VolumeInfo, DiskVmCreatorContext
from diskvm.plugins.base import PluginSpec


class WindowsRegistryOverridePasswordPlugin(PluginSpec):
    """
    Reset user account passwords by overwriting NTLM hashes in SAM registry hives.
    Based on impacket secretsdump.
    """
    NEW_PASSWORD = 'newpwd'

    @staticmethod
    def encrypt_nt_hash(helper: SAMHashes, rid, old_encrypted_hash, nt_hash):
        crypto_common = helper._SAMHashes__cryptoCommon  # noqa
        hashed_bootkey = helper._SAMHashes__hashedBootKey  # noqa

        key1, key2 = crypto_common.deriveKey(rid)
        key = DES.new(key1, DES.MODE_ECB).encrypt(nt_hash[:8]) + DES.new(key2, DES.MODE_ECB).encrypt(nt_hash[8:])

        enc_hash = copy.deepcopy(old_encrypted_hash)
        if isinstance(enc_hash, SAM_HASH_AES):
            enc_hash['Hash'] = AES.new(hashed_bootkey[:16], AES.MODE_CBC, enc_hash['Salt']).encrypt(pad(key, 16))
        elif isinstance(enc_hash, SAM_HASH):
            rc4_key = helper.MD5(hashed_bootkey[:16] + struct.pack("<L", rid) + b"NTPASSWORD\0")
            enc_hash['Hash'] = ARC4.new(rc4_key).encrypt(key)
        else:
            raise Exception('Unknown NT Hash format')
        return enc_hash

    def set_new_password(self, helper: SAMHashes, rid: int, user_account: USER_ACCOUNT_V):
        if user_account['NTHashLength'] == 0:
            # User has no NT hash
            return None

        data = user_account['Data']
        enc_nt_hash = None
        if data[user_account['NTHashOffset']:][2:3] == b'\x01':
            if user_account['NTHashLength'] == 20:
                enc_nt_hash = SAM_HASH(data[user_account['NTHashOffset']:][:user_account['NTHashLength']])
                # nt_hash = helper._SAMHashes__decryptHash(rid, enc_nt_hash, b"NTPASSWORD\0", False)
        else:
            if user_account['NTHashLength'] == 56:
                enc_nt_hash = SAM_HASH_AES(data[user_account['NTHashOffset']:][:user_account['NTHashLength']])
                # nt_hash = helper._SAMHashes__decryptHash(rid, enc_nt_hash, b"NTPASSWORD\0", True)

        if not enc_nt_hash:
            return None

        # Calculate NT hash of new password
        new_nt_hash = compute_nthash(self.NEW_PASSWORD)
        new_enc_hash = self.encrypt_nt_hash(helper, rid, enc_nt_hash, new_nt_hash)

        # Replace NT hash in registry
        user_account = copy.deepcopy(user_account)
        user_account['Data'] = data[:user_account['NTHashOffset']] + new_enc_hash.getData() + data[user_account['NTHashOffset'] + user_account['NTHashLength']:]
        return user_account

    def modify_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        sys32_config = volume_info.filesystem_mount / 'Windows' / 'System32' / 'config'
        sam_file = sys32_config / 'SAM'
        system_file = sys32_config / 'SYSTEM'

        if sam_file.is_file() and system_file.is_file():
            logging.info(f'Begin password bypass for {volume_info}')

            helper = None
            try:
                # Read bootkey from registry
                bootkey = LocalOperations(systemHive=system_file).getBootKey()
                helper = SAMHashes(samFile=sam_file, bootKey=bootkey, perSecretCallback=lambda h: logging.info(f'Found NTLM hash for user: {h}'))
                helper.dump()

                sam = Registry(sam_file)
                sam_data = bytearray(sam._buf)
                for k in sam.open('SAM\\Domains\\Account\\Users').subkeys():
                    if k.name() == 'Names':
                        continue

                    user_account = USER_ACCOUNT_V(k.value('V').value())
                    user_name = user_account['Data'][user_account['NameOffset']:user_account['NameOffset'] + user_account['NameLength']].decode('UTF-16')
                    new_user_account = self.set_new_password(helper=helper, rid=int(k.name(), 16), user_account=user_account)
                    if not new_user_account:
                        continue
                    new_user_data = new_user_account.getData()

                    # Write data back to registry file
                    # quick and dirty workaround, because the library does not support writing back to registry
                    data_offset = k.value('V')._vkrecord.data_offset() + 4
                    sam_data[data_offset:data_offset + len(new_user_data)] = new_user_data

                    logging.info(f'Changed Windows password for user {user_name} to "{self.NEW_PASSWORD}"')

                try:
                    sam_file.write_bytes(sam_data)
                except OSError:
                    # Occurs if NTFS partition could not be mounted correctly
                    logging.warning(f'Password bypass failed: Could not write to SAM file {sam_file}')
                    pass
            finally:
                if helper:
                    helper.finish()


class EtcShadowBlankPasswords(PluginSpec):
    NEW_PASSWORD = 'newpwd'

    def __init__(self, hash_method=crypt.METHOD_SHA256):
        self.hash_method = hash_method

    def hash_password(self, password: str) -> str:
        # Use a SHA512 hash by default
        return crypt.crypt(password, self.hash_method)

    def modify_filesystem(self, volume_info: VolumeInfo, ctx: DiskVmCreatorContext):
        etc_shadow = volume_info.filesystem_mount / 'etc/shadow'
        if etc_shadow.exists():
            logging.info(f'Overriding passwords in /etc/shadow of {volume_info}')

            # Override all set passwords with a plaintext password
            entries = [l.split(':') for l in etc_shadow.read_text().splitlines()]
            for e in entries:
                if len(e) >= 2 and e[1] not in ['!', '*']:
                    e[1] = self.hash_password(self.NEW_PASSWORD)
                    logging.info(f'Password for user "{e[0]}" now is "{self.NEW_PASSWORD}"')
            etc_shadow.write_text('\n'.join(map(lambda l: ':'.join(l), entries)))

