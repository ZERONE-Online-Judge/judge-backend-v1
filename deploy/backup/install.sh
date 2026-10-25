#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -u) == 0 && $(hostname) == zoj-app ]] || {
  echo 'Run with sudo on the central zoj-app server.' >&2; exit 1;
}
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
disk=${1:?Pass the dedicated scsi1 disk by its by-id path}
[[ "$disk" == /dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_drive-scsi1 ]] || exit 1
root=/home/zoj/zerone-online-judge
mount=/srv/zoj-backup
config=/etc/zoj-backup
state=/var/lib/zoj-backup
program=/usr/local/lib/zoj-backup
[[ -d "$root/backend-v1/deploy/env" && -d "$root/frontend/dist" ]] || exit 1
for file in common.py service_backup.py disk_guard.py; do
  [[ -f "$source_dir/$file" ]] || exit 1
done
# Refuse layouts that cannot capture all authoritative service data atomically.
origin=$(findmnt -n -o SOURCE -T "$root")
[[ $(findmnt -n -o FSTYPE -T "$root") == ext4 ]] || exit 1
origin_json=$(lvs --reportformat json --units b --nosuffix -o lv_path,vg_name,lv_name,lv_uuid "$origin")
python3 - "$origin_json" <<'PY'
import json, subprocess, sys
rows = json.loads(sys.argv[1])['report'][0]['lv']
assert len(rows) == 1
vg = rows[0]['vg_name'].strip()
free = json.loads(subprocess.check_output(['vgs','--reportformat','json','--units','b','--nosuffix','-o','vg_free',vg]))['report'][0]['vg'][0]['vg_free']
assert float(free) >= 20 * 1024**3, 'Need 20 GiB unused LVM space for temporary snapshots'
PY

if [[ ! -f "$config/config.json" ]]; then
  device=$(python3 "$source_dir/disk_guard.py" "$disk")
  [[ ! -L "$mount" ]] || exit 1
  if [[ -d "$mount" ]] && find "$mount" -mindepth 1 -print -quit | read -r _; then
    echo 'Backup mount directory is not empty.' >&2; exit 1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends restic parted e2fsprogs lvm2
  [[ $(python3 "$source_dir/disk_guard.py" "$disk") == "$device" ]] || exit 1
  parted --script "$device" mklabel gpt mkpart primary ext4 1MiB 100%
  udevadm settle
  partition="${device}1"
  [[ -b "$partition" ]] || exit 1
  mkfs.ext4 -m 1 -L zoj-backup "$partition"
  filesystem_uuid=$(blkid -s UUID -o value "$partition")
  [[ "$filesystem_uuid" =~ ^[a-f0-9-]{36}$ ]] || exit 1
  install -d -m 0700 "$mount" "$config"
  cp -p /etc/fstab "$config/fstab.before-backup"
  printf 'UUID=%s %s ext4 defaults,nofail,nodev,nosuid,noexec 0 2\n' "$filesystem_uuid" "$mount" >> /etc/fstab
  systemctl daemon-reload
  mount "$mount"
  chmod 0700 "$mount"
  python3 - "$config" "$mount" "$filesystem_uuid" "$origin_json" <<'PY'
import json, os, secrets, sys
from pathlib import Path
config, mount, uuid = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
origin = json.loads(sys.argv[4])['report'][0]['lv'][0]
password = config / 'repository-password'
with password.open('x') as stream:
    stream.write(secrets.token_urlsafe(48) + '\n')
os.chmod(password, 0o600)
(config / 'config.json').write_text(json.dumps({
    'mount': mount, 'filesystem_uuid': uuid, 'password_file': str(password),
    'app_root': '/home/zoj/zerone-online-judge', 'state_dir': '/var/lib/zoj-backup',
    'origin_lv': origin['lv_path'].strip(), 'origin_uuid': origin['lv_uuid'].strip(),
    'volume_group': origin['vg_name'].strip(), 'origin_name': origin['lv_name'].strip(),
    'extra_images': ['zoj-verification-playground:1'],
    'system_paths': ['/etc/docker', '/etc/nginx', '/etc/systemd/system', '/etc/cron.d',
                     '/etc/fstab', '/etc/netplan', '/etc/ssh', '/etc/ufw', '/etc/default',
                     '/etc/sysctl.conf', '/etc/sysctl.d', '/etc/hostname', '/etc/hosts',
                     '/usr/local/lib/zoj-backup', '/etc/zoj-backup/config.json'],
}, indent=2) + '\n')
os.chmod(config / 'config.json', 0o600)
PY
else
  # Reruns never reformat a disk or replace its encryption password.
  mountpoint -q "$mount" || mount "$mount"
  python3 - "$config/config.json" "$mount" <<'PY'
import json, subprocess, sys
config = json.load(open(sys.argv[1]))
uuid = subprocess.check_output(['findmnt','-n','--mountpoint',sys.argv[2],'-o','UUID'],text=True).strip()
assert uuid == config['filesystem_uuid'], 'Existing backup filesystem UUID differs'
PY
fi
if [[ ! -f "$mount/restic/config" ]]; then
  RESTIC_REPOSITORY="$mount/restic" RESTIC_PASSWORD_FILE="$config/repository-password" restic init
fi
install -d -m 0755 "$state" "$program"
install -d -m 0700 "$state/catalog" "$state/snapshot" "$mount/work" "$mount/work/images" "$mount/work/cache" "$mount/work/drills"
install -m 0755 "$source_dir/service_backup.py" "$program/service_backup.py"
install -m 0644 "$source_dir/common.py" "$source_dir/disk_guard.py" "$program/"
ln -sfn "$program/service_backup.py" /usr/local/sbin/zoj-backup
for file in "$source_dir"/*.service "$source_dir"/*.timer; do
  install -m 0644 "$file" /etc/systemd/system/
done
# Deployment scripts share this lock. Preserve ownership for the deployment runner.
if [[ ! -e "$root/service-backup-deploy.lock" ]]; then
  install -o zoj -g zoj -m 0600 /dev/null "$root/service-backup-deploy.lock"
fi
python3 - "$config" <<'PY'
import os, pwd, sys
from pathlib import Path
config = Path(sys.argv[1]); user = pwd.getpwnam('zoj')
path = Path(user.pw_dir) / 'zoj-backup-recovery.key'
if not path.exists():
    path.write_text((config / 'repository-password').read_text())
    os.chmod(path, 0o600); os.chown(path, user.pw_uid, user.pw_gid)
PY
systemctl daemon-reload
systemctl enable --now zoj-backup.timer zoj-backup-maintain.timer zoj-backup-drill.timer
systemctl start zoj-backup.service
systemctl start zoj-backup-drill.service
/usr/local/sbin/zoj-backup status
echo 'ZOJ backup installed and an isolated restore drill passed. Keep the recovery key off this server.'
