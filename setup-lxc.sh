#!/bin/bash
# Bootstrap script for Debian/Ubuntu LXC.
# Run as root: bash setup-lxc.sh
set -euo pipefail

INSTALL_DIR="/opt/applypilot-discovery"
REPO_URL="https://github.com/YOUR_USERNAME/applypilot-discovery.git"

# ── Python 3.11 ──────────────────────────────────────────────────────────────
PYTHON=""
for c in python3.13 python3.12 python3.11; do
    command -v "$c" &>/dev/null && PYTHON="$c" && break
done

if [ -z "$PYTHON" ]; then
    echo "Python 3.11+ not found. Installing from source..."
    apt-get update && apt-get install -y wget build-essential libssl-dev \
        libffi-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev
    wget -q https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
    tar xf Python-3.11.9.tgz
    cd Python-3.11.9
    ./configure --enable-optimizations --quiet
    make -j"$(nproc)"
    make altinstall
    cd ..
    rm -rf Python-3.11.9 Python-3.11.9.tgz
    PYTHON=python3.11
fi

echo "Using $PYTHON (${PYTHON} --version)"
curl -sS https://bootstrap.pypa.io/get-pip.py | "$PYTHON"

# ── Repo ─────────────────────────────────────────────────────────────────────
if [ ! -d "$INSTALL_DIR/.git" ]; then
    git clone "$REPO_URL" "$INSTALL_DIR"
else
    cd "$INSTALL_DIR" && git pull --ff-only
fi

# ── Deps ─────────────────────────────────────────────────────────────────────
"$PYTHON" -m pip install -r "$INSTALL_DIR/requirements.txt"

# ── Systemd service ──────────────────────────────────────────────────────────
# Update User= in the service file to match your username
sed -i "s/^User=.*/User=$(logname 2>/dev/null || echo yassine)/" \
    "$INSTALL_DIR/applypilot-discovery.service"
sed -i "s|/usr/local/bin/python3.11|$(which $PYTHON)|" \
    "$INSTALL_DIR/applypilot-discovery.service"

cp "$INSTALL_DIR/applypilot-discovery.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable applypilot-discovery

echo ""
echo "Done. Next steps:"
echo "  1. cp $INSTALL_DIR/.env.example $INSTALL_DIR/.env"
echo "     Fill in DATABASE_URL and DATABASE_TOKEN"
echo "  2. sudo systemctl start applypilot-discovery"
echo "  3. journalctl -u applypilot-discovery -f"
