from pathlib import Path

BOOTCODE_LEN = 3
NTFS_SIGNATURE = b'NTFS    '


def is_signature(flat_mount: Path, signature: bytes, bootcode_len: int = BOOTCODE_LEN):
    with open(flat_mount, 'rb') as f:
        magic = f.read(bootcode_len + len(signature))[bootcode_len:]
    return magic == signature


def is_ntfs(flat_mount: Path):
    return is_signature(flat_mount, NTFS_SIGNATURE)

