#!/usr/bin/env bash
#
# Turn off password authentication.
#
#   sudo ./harden-ssh.sh
#
# A server with root and a password reachable from the internet starts
# receiving brute-force attempts within minutes of booting. Once key access
# works, a password is nothing but a way in for somebody else.
#
# Separate from install.sh on purpose: this touches how you reach the machine,
# not what runs on it, and it must never be a side effect of installing an
# application.

set -euo pipefail

# Refuse to proceed unless a key is actually installed. Disabling passwords
# without one locks the owner out of their own server, and the recovery is a
# rescue console at best.
KEYS=/root/.ssh/authorized_keys
if [[ ! -s "$KEYS" ]]; then
	echo "REFUSING: $KEYS is missing or empty." >&2
	echo "Install your public key first, or you will lock yourself out." >&2
	exit 1
fi
echo "==> found $(grep -c . "$KEYS") key(s) in $KEYS"

install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-doorslip-hardening.conf <<'EOF'
# Keys only. A leaked password is worth nothing if the server will not take one.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF

sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo "==> password authentication is off; keep this session open and test a new one before closing it"
