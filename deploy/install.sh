#!/usr/bin/env bash
#
# Provision a Doorslip server on a fresh Ubuntu box.
#
#   sudo ./install.sh buzon.example.com
#   sudo ./install.sh buzon.example.com /tmp/doorslip-0.1.0-py3-none-any.whl
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
	echo "usage: $0 <hostname> [local-wheel]   e.g. $0 buzon.example.com" >&2
	exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/doorslip
DATA_DIR=/var/lib/doorslip
WEB_DIR=/var/www/doorslip

# Documents are authored on whatever machine the maintainer uses, so they may
# arrive with CRLF line endings. `tr -d` strips them with no pattern escaping
# involved: a sed expression would need a literal backslash-r, and getting
# that wrong yields a file that looks correct while silently skipping every
# substitution below.
render() {
	tr -d '\r' < "$1" | sed "s/buzon\\.doorslip\\.org/$HOST/g"
}

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
# No login shell and no home: this account exists to run one process and own
# one database. Nothing else should be reachable through it.
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
render "$HERE/doorslip.service" | sed "s/DOORSLIP_HOST/$HOST/g" \
	> /etc/systemd/system/doorslip.service

echo "==> publishing the onboarding files"
# An arriving agent fetches these before it has a key, an identity, or any way
# to authenticate. They are plain files and require none of that.
install -d -m 755 "$WEB_DIR"

if [[ -f "$HERE/SKILL.md" ]]; then
	render "$HERE/SKILL.md" > "$WEB_DIR/skill.md"
	if [[ -n "$LOCAL_WHEEL" ]]; then
		WHEEL_NAME="$(basename "$LOCAL_WHEEL")"
		cp "$LOCAL_WHEEL" "$WEB_DIR/$WHEEL_NAME"
		# Until the package is on PyPI, point installs at the wheel served
		# alongside this document.
		sed -i "s|^pip install doorslip$|pip install https://$HOST/$WHEEL_NAME|" \
			"$WEB_DIR/skill.md"
	fi
	echo "    https://$HOST/skill.md"
fi

if [[ -f "$HERE/REFERENCE.md" ]]; then
	render "$HERE/REFERENCE.md" > "$WEB_DIR/reference.md"
	echo "    https://$HOST/reference.md"
fi

chmod 644 "$WEB_DIR"/* 2>/dev/null || true

echo "==> writing the Caddyfile"
render "$HERE/Caddyfile" | sed "s/DOORSLIP_HOST/$HOST/g" > /etc/caddy/Caddyfile

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
