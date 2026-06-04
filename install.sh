#!/usr/bin/env bash
# install.sh — Set up unitree-d1-control on Linux (Pop!_OS / Ubuntu 22.04+).
# Run once from the project root:  ./install.sh

set -euo pipefail

D1_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$D1_DIR/venv"
PYTHON="${PYTHON:-python3}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[D1]${NC} $*"; }
warn()  { echo -e "${YELLOW}[D1]${NC} $*"; }
error() { echo -e "${RED}[D1]${NC} $*" >&2; exit 1; }

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   Unitree D1 Arm Control — Linux Installer   ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# ── 1. Python 3.10+ ──────────────────────────────────────────────────────────
info "Checking Python..."
$PYTHON --version &>/dev/null || error "Python 3 not found. Install python3 and retry."
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    error "Python 3.10+ required (found $PY_VER)."
fi
info "Python $PY_VER — OK"

# ── 2. Virtual environment ───────────────────────────────────────────────────
if [ -d "$VENV_DIR" ]; then
    info "Existing venv found — skipping creation."
else
    info "Creating virtual environment in venv/ ..."
    $PYTHON -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

info "Upgrading pip..."
"$VENV_PIP" install --quiet --upgrade pip

# ── 3. Python dependencies ───────────────────────────────────────────────────
info "Installing Python requirements..."
"$VENV_PIP" install --quiet -r "$D1_DIR/requirements.txt"
info "Python packages installed (cyclonedds, dearpygui, pyserial, numpy)."

# ── 4. Runtime directories ───────────────────────────────────────────────────
mkdir -p "$D1_DIR/logs" "$D1_DIR/recordings"
info "Runtime directories ready (logs/, recordings/)."

# ── 5. Sudoers — passwordless ip / dhclient ──────────────────────────────────
# arm_control.py calls  sudo ip addr ...  to set 192.168.123.162/24 on the
# Ethernet interface before DDS connects. Without NOPASSWD the script blocks.
SUDOERS_FILE="/etc/sudoers.d/d1-arm"
USER_NAME="${SUDO_USER:-$(whoami)}"

if sudo test -f "$SUDOERS_FILE" 2>/dev/null; then
    info "Sudoers rule already present at $SUDOERS_FILE."
else
    warn "Sudoers rule needed so 'sudo ip' / 'sudo dhclient' run without a password prompt."
    echo ""
    read -rp "  Set up sudoers rule now? [Y/n] " _ans
    _ans="${_ans:-Y}"
    if [[ "$_ans" =~ ^[Yy] ]]; then
        echo "${USER_NAME} ALL=(ALL) NOPASSWD: /sbin/ip, /sbin/dhclient" \
            | sudo tee "$SUDOERS_FILE" >/dev/null
        sudo chmod 440 "$SUDOERS_FILE"
        info "Sudoers rule written to $SUDOERS_FILE."
    else
        warn "Skipped. Add it manually before running the arm:"
        warn "  echo '${USER_NAME} ALL=(ALL) NOPASSWD: /sbin/ip, /sbin/dhclient' | sudo tee $SUDOERS_FILE && sudo chmod 440 $SUDOERS_FILE"
    fi
fi

# ── 6. Firewall — DDS multicast ──────────────────────────────────────────────
# firewalld blocks incoming UDP multicast even with the interface in the
# trusted zone, because the interface is manually configured (not via NM).
# Symptom: tcpdump sees arm packets but Python sockets receive nothing.
if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld 2>/dev/null; then
    if sudo firewall-cmd --zone=trusted --query-source=192.168.123.0/24 --permanent &>/dev/null; then
        info "Firewall: arm subnet (192.168.123.0/24) already trusted."
    else
        warn "firewalld is active and will block DDS multicast from the arm."
        warn "Fix: add 192.168.123.0/24 as a trusted source + open UDP 7400-7401."
        echo ""
        read -rp "  Apply firewall rules now? [Y/n] " _fw
        _fw="${_fw:-Y}"
        if [[ "$_fw" =~ ^[Yy] ]]; then
            sudo firewall-cmd --zone=trusted --add-source=192.168.123.0/24 --permanent
            sudo firewall-cmd --zone=public  --add-port=7400-7401/udp   --permanent
            sudo firewall-cmd --reload
            info "Firewall rules applied."
        else
            warn "Skipped. Run manually before connecting the arm:"
            warn "  sudo firewall-cmd --zone=trusted --add-source=192.168.123.0/24 --permanent"
            warn "  sudo firewall-cmd --zone=public  --add-port=7400-7401/udp   --permanent"
            warn "  sudo firewall-cmd --reload"
        fi
    fi
else
    info "firewalld not active — no firewall changes needed."
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}  Installation complete!${NC}"
echo ""
echo "  Activate the virtual environment:"
echo -e "    ${CYAN}source venv/bin/activate${NC}"
echo ""
echo "  Run the CLI demo:"
echo -e "    ${CYAN}python3 src/main.py${NC}"
echo ""
echo "  Launch the control GUI (drag-teach record/playback):"
echo -e "    ${CYAN}python3 src/gui.py${NC}"
echo ""
