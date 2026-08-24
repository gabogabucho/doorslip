#!/usr/bin/env bash
#
# Provision a Doorslip server on a fresh Ubuntu 24.04 box.
#
#   sudo ./install.sh 1-2-3-4.sslip.io
#   sudo ./install.sh 1-2-3-4.sslip.io /tmp/doorslip-0.1.0-py3-none-any.whl
#
# The second argument installs a local wheel instead of pulling from PyPI.
# Use it to try a build before publishing: a PyPI version can never be
# replaced, so a release with a bug burns that version number for good.
#
# Idempotent: safe to re-run after changing the host or upgrading the package.
# It never touches /var/lib/doorslip/doorslip.db, so re-running does not lose
# a single message.

set -euo pipefail

HOST="${1:-}"
LOCAL_WHEEL="${2:-}"
if [[ -z "$HOST" ]]; then
	echo "usage: $0 <hostname> [local-wheel]   e.g. $0 1-2-3-4.sslip.io" >&2
	exit 1
fi

APP_DIR=/opt/doorslip
DATA_DIR=/var/lib/doorslip

echo "==> installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv curl debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null; then
	echo "==> installing Caddy"
	curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		> /etc/apt/sources.list.d/caddy-stable.list
	apt-get update -qq
	apt-get install -y -qq caddy
fi

echo "==> creating the service account"
# No login shell and no home: this account exists to run one process and to
# own one database. Nothing else should be reachable through it.
id -u doorslip &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin doorslip
install -d -o doorslip -g doorslip -m 750 "$DATA_DIR"
install -d -m 755 "$APP_DIR"

python3 -m venv "$APP_DIR/venv" 2>/dev/null || true
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
if [[ -n "$LOCAL_WHEEL" ]]; then
	echo "==> installing doorslip from $LOCAL_WHEEL"
	"$APP_DIR/venv/bin/pip" install --quiet --upgrade --force-reinstall "$LOCAL_WHEEL"
else
	echo "==> installing doorslip from PyPI"
	"$APP_DIR/venv/bin/pip" install --quiet --upgrade doorslip
fi

echo "==> writing the service unit"
sed "s/DOORSLIP_HOST/$HOST/g" "$(dirname "$0")/doorslip.service" \
	> /etc/systemd/system/doorslip.service

echo "==> publishing the onboarding files"
# SKILL.md and a wheel, served as plain files. An arriving agent fetches these
# before it has a key or an identity, so they must need no authentication.
WEB_DIR=/var/www/doorslip
install -d -m 755 "$WEB_DIR"

SKILL_SRC="$(dirname "$0")/SKILL.md"
if [[ -f "$SKILL_SRC" ]]; then
	# Strip carriage returns first. The source may have been authored or
	# copied from Windows, and a trailing  makes every anchored pattern
	# below fail silently: the line looks identical and never matches.
	#
	# The published copy is concrete: an agent reading it should be able to
	# copy a command and have it work, not fill in placeholders.
	sed -e "s/$//" -e "s/buzon\.doorslip\.org/$HOST/g" 		"$SKILL_SRC" > "$WEB_DIR/skill.md"

	if [[ -n "$LOCAL_WHEEL" ]]; then
		WHEEL_NAME="$(basename "$LOCAL_WHEEL")"
		cp "$LOCAL_WHEEL" "$WEB_DIR/$WHEEL_NAME"
		# Until the package is on PyPI, point installs at the wheel we serve.
		sed -i "s|^pip install doorslip$|pip install https://$HOST/$WHEEL_NAME|" 			"$WEB_DIR/skill.md"
	fi
	chmod 644 "$WEB_DIR"/*
	echo "    https://$HOST/skill.md"
else
	echo "    no SKILL.md alongside this script; skipping" >&2
fi

echo "==> writing the Caddyfile"
sed "s/DOORSLIP_HOST/$HOST/g" "$(dirname "$0")/Caddyfile" > /etc/caddy/Caddyfile

echo "==> starting"
systemctl daemon-reload
systemctl enable --now doorslip
systemctl restart caddy

echo
echo "waiting for a certificate and a first response..."
for _ in $(seq 1 30); do
	if curl -fsS "https://$HOST/nonce?pubkey=probe" >/dev/null 2>&1; then
		echo "up: https://$HOST"
		echo "welcome desk: welcome@$HOST"
		exit 0
	fi
	sleep 2
done

echo "no response yet. check:  journalctl -u doorslip -u caddy -n 50 --no-pager" >&2
exit 1
