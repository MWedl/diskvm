import dataclasses
import enum
import struct
from typing import NewType

Uint8 = NewType('Uint8', int)
Uint16 = NewType('Uint16', int)
Uint32 = NewType('Uint32', int)
Uint64 = NewType('Uint64', int)


class Endian(enum.Enum):
    LITTLE = 'little'
    BIG = 'big'


def bytes_field(size, metadata=None, *args, **kwargs):
    metadata = (metadata or {}) | {'size': size}
    return dataclasses.field(*args, metadata=metadata, **kwargs)


@dataclasses.dataclass()
class Structure:
    endian = Endian.LITTLE

    type_format_map = {
        Uint8: 'B',
        Uint16: 'H',
        Uint32: 'I',
        Uint64: 'Q',
    }

    @classmethod
    def _get_struct_format(cls):
        out = '<' if cls.endian == Endian.LITTLE else '>'
        for f in dataclasses.fields(cls):
            if fmt := cls.type_format_map.get(f.type):
                out += fmt
            elif f.type is bytes and (size := f.metadata.get('size')):
                out += f'{size}s'
            else:
                raise Exception(f'Unsupported Type in Structure: {f.type}')
        return out

    def pack(self):
        return struct.pack(self._get_struct_format(), *dataclasses.astuple(self))

    @classmethod
    def unpack(cls, data):
        values = struct.unpack(cls._get_struct_format(), data)
        return cls(**dict(zip(map(lambda f: f.name, dataclasses.fields(cls)), values)))  # noqa
