#!/bin/sh
# Generate the pod's host key on first start (emptyDir /keys, so a re-created
# pod gets a NEW host key — customers see a host-key change on recreate; a
# persistent per-machine host key is the follow-up, and is called out in the
# customer doc). Then exec sshd in the foreground as this user.
set -eu
mkdir -p /keys
if [ ! -f /keys/ssh_host_ed25519_key ]; then
  ssh-keygen -q -t ed25519 -N '' -f /keys/ssh_host_ed25519_key
fi
chmod 600 /keys/ssh_host_ed25519_key
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
