#!/usr/bin/env bash
#
# Provision a Doorslip server on a fresh Ubuntu box.
#
#   sudo ./install.sh example.org
#   sudo ./install.sh example.org /tmp/doorslip-0.1.0-py3-none-any.whl
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
	echo "usage: $0 <hostname> [local-wheel]   e.g. $0 example.org" >&2
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
#
# The seed instance is `doorslip.org` itself, so that is the string the
# documents are authored against and the one rewritten here. Every occurrence
# of it in SKILL.md, REFERENCE.md and index.html means "the server serving
# this document", never "the project" — the project is linked as a GitHub URL,
# which has no `.org` in it and is deliberately left alone. Anyone installing
# their own instance gets documents that name their host and nobody else's.
render() {
	tr -d '\r' < "$1" | sed "s/doorslip\\.org/$HOST/g"
}

echo "==> installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv curl sqlite3 cron debian-keyring debian-archive-keyring apt-transport-https

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
# The service runs as `doorslip` and needs to write the database, its WAL
# sidecars and the welcome desk's key. Anything root touched in here - a
# backup, a manual query, a file moved during an upgrade - can leave a root
# owned file behind, and SQLite reports that as "attempt to write a readonly
# database", which sounds like a permissions problem with the disk rather
# than with one file.
chown -R doorslip:doorslip "$DATA_DIR"
install -d -m 755 "$APP_DIR"

python3 -m venv "$APP_DIR/venv" 2>/dev/null || true
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
if [[ -n "$LOCAL_WHEEL" ]]; then
	echo "==> installing doorslip from $LOCAL_WHEEL"
	"$APP_DIR/venv/bin/pip" install --quiet --upgrade --force-reinstall "$LOCAL_WHEEL"
else
	echo "==> installing doorslip from PyPI"
	# --no-cache-dir because pip caches the index it read, not only the
	# wheels. Twice now a release was on PyPI, this line reported success,
	# and the service kept serving the previous version - the same silent
	# no-op RELEASING.md opens by warning about, arriving through a different
	# door. A deploy that does nothing must not look like a deploy.
	"$APP_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade doorslip
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
		# Drop older wheels: serving two versions side by side invites
		# somebody to install the stale one from a link they kept.
		rm -f "$WEB_DIR"/doorslip-*.whl
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

# The landing page. Rendered like the rest, so an instance that is not the
# seed serves a page naming its own host instead of somebody else's.
if [[ -f "$HERE/index.html" ]]; then
	render "$HERE/index.html" > "$WEB_DIR/index.html"
	echo "    https://$HOST/"
fi

# The link preview. Copied, never rendered: it is a PNG, and running it
# through sed would corrupt it.
if [[ -f "$HERE/card.png" ]]; then
	cp "$HERE/card.png" "$WEB_DIR/card.png"
	echo "    https://$HOST/card.png"
fi

chmod 644 "$WEB_DIR"/* 2>/dev/null || true

echo "==> scheduling backups"
# Nightly, before anybody is awake to notice the disk churn. There is no
# identity recovery in this protocol, so a lost directory is not something the
# people using it could rebuild for themselves.
if [[ -f "$HERE/backup.sh" ]]; then
	install -m 700 "$HERE/backup.sh" /usr/local/sbin/doorslip-backup
	printf '17 4 * * * root /usr/local/sbin/doorslip-backup >/var/log/doorslip-backup.log 2>&1
' 		> /etc/cron.d/doorslip-backup
	chmod 644 /etc/cron.d/doorslip-backup
	/usr/local/sbin/doorslip-backup || echo "    first backup failed" >&2
fi

echo "==> writing the Caddyfile"
render "$HERE/Caddyfile" | sed "s/DOORSLIP_HOST/$HOST/g" > /etc/caddy/Caddyfile

echo "==> starting"
systemctl daemon-reload
systemctl enable doorslip
# `enable --now` starts a stopped service but leaves a running one alone, so
# an upgrade would install new code and keep serving the old process without
# saying anything. Restart unconditionally: this script exists to make the
# server match what was just installed.
systemctl restart doorslip
systemctl restart caddy

INSTALLED="$("$APP_DIR/venv/bin/pip" show doorslip 2>/dev/null | awk '/^Version:/{print $2}')"

echo
echo "waiting for a certificate and a first response..."
for _ in $(seq 1 30); do
	if curl -fsS "https://$HOST/nonce?pubkey=probe" >/tmp/doorslip-probe 2>/dev/null; then
		# Report the version the running process announces, not the one on
		# disk. Those parted company twice: pip reported success while the
		# service kept serving what it had, and nothing said so. A deploy has
		# to end by naming what is actually answering.
		SERVING="$(sed -n 's/.*"client":"\([^"]*\)".*/\1/p' /tmp/doorslip-probe)"
		echo "up: https://$HOST"
		echo "serving: ${SERVING:-unknown}   installed: ${INSTALLED:-unknown}"
		echo "welcome desk: welcome@$HOST"
		rm -f /tmp/doorslip-probe
		if [[ -n "$SERVING" && -n "$INSTALLED" && "$SERVING" != "$INSTALLED" ]]; then
			echo "the running server is not what is installed; it did not restart cleanly" >&2
			exit 1
		fi
		exit 0
	fi
	sleep 2
done

echo "no response yet. check:  journalctl -u doorslip -u caddy -n 50 --no-pager" >&2
exit 1
