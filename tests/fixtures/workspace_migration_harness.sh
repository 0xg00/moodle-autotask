#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq e2fsprogs util-linux python3 coreutils >/dev/null
groupadd moodle-agent
useradd -g moodle-agent -d /data/agent moodle-agent
install -d -o root -g root -m 0755 /data
install -d -o moodle-agent -g moodle-agent -m 0700 /data/agent
chmod 0644 /etc/fstab

reset_case() {
  umount /data/agent/workspaces 2>/dev/null || true
  umount /data/staging 2>/dev/null || true
  rm -rf /data/root /data/staging /data/agent/workspaces
  rm -f /tmp/fault-hit
  grep -vF '/data/agent/workspaces' /etc/fstab >/tmp/fstab.clean || true
  cp /tmp/fstab.clean /etc/fstab
  chmod 0644 /etc/fstab
  install -d -o moodle-agent -g moodle-agent -m 0700 /data/agent/workspaces
  install -d -o moodle-agent -g moodle-agent -m 0750 \
    /data/agent/workspaces/legacy-job
  printf legacy-data >/data/agent/workspaces/legacy-job/result-schema.json
  chown moodle-agent:moodle-agent \
    /data/agent/workspaces/legacy-job/result-schema.json
  chmod 0600 /data/agent/workspaces/legacy-job/result-schema.json
}

verify_case() {
  bash /harness/setup.sh
  grep -Fxq legacy-data /data/agent/workspaces/legacy-job/result-schema.json
  grep -Fxq phase=active /data/root/agent-workspaces.state
  test ! -e /data/root/legacy-workspaces.pending
  test ! -e /data/staging
  test ! -e /data/agent/workspaces/lost+found
}

run_concurrent_case() {
  reset_case
  sed '/^  cp -a -- /i\  touch /tmp/concurrent-entered; while [ ! -e /tmp/concurrent-release ]; do sleep 0.05; done' \
    /harness/setup.sh >/tmp/concurrent.sh
  chmod 0755 /tmp/concurrent.sh
  rm -f /tmp/concurrent-entered /tmp/concurrent-release /tmp/second-finished
  bash /tmp/concurrent.sh &
  first=$!
  for _ in $(seq 1 200); do
    [ -e /tmp/concurrent-entered ] && break
    sleep 0.05
  done
  test -e /tmp/concurrent-entered
  (bash /harness/setup.sh; touch /tmp/second-finished) &
  second=$!
  sleep 0.5
  test ! -e /tmp/second-finished
  touch /tmp/concurrent-release
  wait "$first"
  wait "$second"
  test -e /tmp/second-finished
  verify_case
  echo recovered-concurrent
}

run_case() {
  name="$1"
  expression="$2"
  reset_case
  sed "$expression" /harness/setup.sh >/tmp/fault.sh
  chmod 0755 /tmp/fault.sh
  set +e
  /tmp/fault.sh
  status=$?
  set -e
  test "$status" -eq 97
  test -f /tmp/fault-hit
  verify_case
  echo "recovered-$name"
}

run_case after-copy \
  '/^  write_state copied/i\  touch /tmp/fault-hit; exit 97'
run_case during-copy \
  '/^  cp -a -- /c\  install -d -o root -g root -m 0700 "$staging/partial"; touch "$staging/partial/file"; touch /tmp/fault-hit; exit 97'
run_case after-copied \
  '/^  write_state copied/a\  touch /tmp/fault-hit; exit 97'
run_case after-rename \
  '/^  mv -T "$workspace" "$backup"$/a\  touch /tmp/fault-hit; exit 97'
run_case after-mount \
  '/^mount "$workspace"$/a\touch /tmp/fault-hit; exit 97'
run_case after-active \
  '/^write_state active/a\touch /tmp/fault-hit; exit 97'
run_case during-cleanup \
  '/^    find "$backup" /c\    find "$backup" -xdev -type f -delete; touch /tmp/fault-hit; exit 97'

run_concurrent_case

umount /data/agent/workspaces
bash /harness/setup.sh
grep -Fxq legacy-data /data/agent/workspaces/legacy-job/result-schema.json
grep -Fxq phase=active /data/root/agent-workspaces.state

umount /data/agent/workspaces
rm -rf /data/root /data/agent/workspaces
grep -vF '/data/agent/workspaces' /etc/fstab >/tmp/fstab.clean || true
cp /tmp/fstab.clean /etc/fstab
install -d -o root -g root -m 0700 /data/root
install -d -o moodle-agent -g moodle-agent -m 0700 /data/agent/workspaces
dd if=/dev/zero of=/data/root/agent-workspaces.img \
  bs=64M count=1 conv=fsync status=none
chown root:root /data/root/agent-workspaces.img
chmod 0600 /data/root/agent-workspaces.img
mkfs.ext4 -F -E nodiscard -N 100000 -m 6 \
  /data/root/agent-workspaces.img >/dev/null
blocks="$(tune2fs -l /data/root/agent-workspaces.img | \
  awk -F: '/^Block count:/ {gsub(/ /,"",$2); print $2}')"
tune2fs -r $(((blocks * 6 + 99) / 100)) \
  /data/root/agent-workspaces.img >/dev/null
printf '%s %s ext4 loop,nodev,nosuid 0 2\n' \
  /data/root/agent-workspaces.img /data/agent/workspaces >>/etc/fstab
mount /data/agent/workspaces
test -d /data/agent/workspaces/lost+found
install -d -o moodle-agent -g moodle-agent -m 0750 \
  /data/agent/workspaces/prior-job
printf prior-data >/data/agent/workspaces/prior-job/result-schema.json
chown moodle-agent:moodle-agent \
  /data/agent/workspaces/prior-job/result-schema.json
chmod 0600 /data/agent/workspaces/prior-job/result-schema.json
bash /harness/setup.sh
grep -Fxq prior-data /data/agent/workspaces/prior-job/result-schema.json
test ! -e /data/agent/workspaces/lost+found
grep -Fxq phase=active /data/root/agent-workspaces.state

echo workspace-migration-fault-matrix-ok
