#!/usr/bin/env bash
set -euo pipefail
directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$directory"
[[ -f SHA256SUMS ]] && sha256sum --check --quiet SHA256SUMS
exec bash "$directory/install.sh" /dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1
