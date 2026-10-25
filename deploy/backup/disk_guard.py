"""Refuse formatting anything except the newly attached, empty 300 GiB disk."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

EXPECTED_SIZE = 300 * 1024**3


def validate_inventory(record, signatures):
    if record.get("type") != "disk" or record.get("size") != EXPECTED_SIZE:
        raise ValueError("Expected the dedicated 300 GiB whole disk")
    if record.get("children") or record.get("fstype") or any(record.get("mountpoints") or []):
        raise ValueError("Disk has partitions, a filesystem, or a mount; refusing to erase it")
    if signatures:
        raise ValueError("Disk has existing signatures; refusing to erase it")


def main(path):
    device = Path(path).resolve(strict=True)
    if not stat.S_ISBLK(device.stat().st_mode):
        raise ValueError("Expected a block device")
    inventory = json.loads(subprocess.check_output([
        "lsblk", "--json", "--bytes", "--paths", "--output", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS", str(device),
    ]))["blockdevices"]
    if len(inventory) != 1:
        raise ValueError("Ambiguous disk inventory")
    signatures = json.loads(subprocess.check_output(["wipefs", "--no-act", "--json", str(device)]))["signatures"]
    validate_inventory(inventory[0], signatures)
    print(device)


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run the disk check with sudo")
    main(sys.argv[1])
