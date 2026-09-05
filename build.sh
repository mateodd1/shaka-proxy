#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
./.venv/bin/pip install -q -r requirements.txt
./.venv/bin/pip install --no-build-isolation --force-reinstall --no-deps .
echo "installed $(./.venv/bin/python -c 'import proxy; print(proxy.__file__)')"
