# diskvm
Create virtual machines from disk images.


## Features
* Create VMs from disk images
* Supports Windows and Linux disk images
* Prevent modifications of disk images with VM snapshots
* Prevent communication with external systems by disabling network connections
* Automatically detect OS and configure VM accordingly
* Overwrite Windows and Linux login passwords in VM (log in without knowing the original password)
* Bypass full disk encryption (BitLocker, VeraCrypt, LUKS) in VM with master keys (without knowing the original password/recovery-key)


## Setup
This project requires some external tools. They need to be installed on the host system:
* VMware Workstation 16.x
* [dislocker-pwreset](https://github.com/MWedl/dislocker) - only required for bypassing BitLocker (fork of [dislocker](https://github.com/Aorimn/dislocker) with additional utilities)
* Linux filesystem utilities: `mount`, `umount`, `losetup`, `cryptsetup`, LVM

Install python dependencies:
```shell
pip install -r requirements.txt
```

## Usage
Create VM of an unencrypted disk images and boot it.
This project needs to run as root to allow mounting the disk image and filesystems in it.
```shell
sudo python3 main.py win10-unencrypted.dd --out-dir=./win10-vm/
```


Create VM of a BitLocker-encrypted disk image and boot it.
First extract AES keys from a memory dump of the system.
This project tries all extracted keys and finds the correct master key for accessing volumes.
```shell
aeskeyfind win10-bitlocker.mem > possible-keys.txt
sudo python3 main.py win10-bitlocker.dd --out-dir=./win10-bde-vm/ --fde-bypass=auto --master-key-file=possible-keys.txt

sudo python3 main.py win10-bitlocker.dd --out-dir=./win10-bde-vm/ --fde-bypass=auto --master-key=0f6d666998f8b4523eacad91245c2f26922bebd5e679de54cfc1bae4b881f9b4
```
