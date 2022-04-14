import contextlib
import itertools
import logging
import os
import re
import subprocess
import tempfile
import time
from enum import IntEnum
from pathlib import Path

import click

from diskvm.errors import DiskVmError


class SizeUnit(IntEnum):
    B = 1
    KB = 1024
    MB = 1024 * KB
    GB = 1024 * MB
    TB = 1024 * GB


def size(num: int, unit: SizeUnit):
    """
    Get size in bytes
    """
    return num * unit


def groupby(lst, key, reverse=False):
    return list(map(lambda g: (g[0], list(g[1])), itertools.groupby(sorted(lst, key=key, reverse=reverse), key=key)))


def run_process(args: list[str], stderr=False):
    try:
        logging.debug(f'Running subprocess {args}')
        proc = subprocess.run(args=args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        logging.debug(f'Finished subprocess {args} stdout="{proc.stdout}" stderr="{proc.stderr}"')
        if stderr:
            return proc.stdout, proc.stderr
        else:
            return proc.stdout
    except subprocess.CalledProcessError as ex:
        raise DiskVmError(args, ex.stderr) from ex
    except (subprocess.SubprocessError, OSError) as ex:
        raise DiskVmError(args) from ex


@contextlib.contextmanager
def temp_dir():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        yield tmp_dir
    finally:
        tmp_dir.rmdir()


def retry(num, sleep=1):
    def _inner(func):
        def _wrapped(*args, **kwargs):
            for i in reversed(range(1 + num)):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    if i == 0:
                        raise
                    else:
                        time.sleep(sleep)
        return _wrapped
    return _inner


def size_blockdevice(file_or_device):
    """
    Find size of file or block device in bytes
    """
    with open(file_or_device, 'rb') as f:
        return f.seek(0, os.SEEK_END)


class ChoiceMap(click.Choice):
    def __init__(self, choices: dict):
        self.choice_map = choices
        super().__init__(choices=self.choice_map.keys(), case_sensitive=False)

    def convert(self, value, param, ctx):
        if value in self.choice_map.values():
            return value
        return self.choice_map[super().convert(value=value, param=param, ctx=ctx)]


class BytesParamType(click.types.StringParamType):
    name = 'bytes'

    def convert(self, value, param, ctx) -> bytes:
        try:
            return bytes.fromhex(value)
        except ValueError:
            self.fail(message=f'"{value}" is not a hex string', param=param, ctx=ctx)


class SizeParamType(click.ParamType):
    name = 'size'
    PATTERN = re.compile(r'([0-9]+)([KMGT]?B?)')

    def convert(self, value, param, ctx) -> int:
        if isinstance(value, int):
            return value
        elif m := self.PATTERN.fullmatch(value):
            return int(m.group(1)) * SizeUnit[m.group(2) if m.group(2).endswith('B') else m.group(2) + 'B']
        else:
            self.fail(message=f'"{value}" is not a valid size', param=param, ctx=ctx)
