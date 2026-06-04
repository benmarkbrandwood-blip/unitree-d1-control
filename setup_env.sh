#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# cyclonedds PyPI wheel is self-contained (bundles libddsc 0.10.5).
# No system CycloneDDS build or CYCLONEDDS_HOME needed for Python-only use.
# See plan.md Phase 3a only if you also want to build the C++ d1_sdk examples.
pip install -r requirements.txt

echo ""
echo "Venv ready. Activate with:  source venv/bin/activate"
echo "Run demo with:              python3 src/main.py"
