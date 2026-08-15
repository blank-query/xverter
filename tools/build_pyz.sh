#!/bin/bash
# Build a single-file executable: dist/xverter.pyz
# Pure-stdlib zipapp - runs anywhere with python3 >= 3.9, both bundled
# redump DATs included. No pip, no venv, no dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build/pyz dist
mkdir -p build/pyz dist
cp -r xverter build/pyz/xverter
find build/pyz -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cat > build/pyz/__main__.py << 'EOF'
import sys
from xverter.cli import main
sys.exit(main())
EOF

python3 -m zipapp build/pyz \
    --python "/usr/bin/env python3" \
    --output dist/xverter.pyz \
    --compress
chmod +x dist/xverter.pyz
ls -la dist/
echo "smoke:"
./dist/xverter.pyz --version
