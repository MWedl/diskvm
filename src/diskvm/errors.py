

class DiskVmError(Exception):
    pass


class InvalidDiskError(DiskVmError):
    pass


class InvalidDiskPartError(DiskVmError):
    pass


class UnsupportedDiskTypeError(DiskVmError):
    pass


class VirtualizationSoftwareNotAvailable(DiskVmError):
    pass

