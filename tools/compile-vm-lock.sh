#!/usr/bin/env bash
set -euo pipefail

python_version="$(python3.10 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$python_version" != "3.10" ]; then
  echo "Python 3.10 is required to compile the VM lock" >&2
  exit 1
fi

python3.10 -m pip install --disable-pip-version-check "pip<26" "pip-tools==7.5.2" packaging typing-extensions
python3.10 -m piptools compile \
  --strip-extras \
  --resolver=backtracking \
  --no-emit-index-url \
  --no-emit-trusted-host \
  --output-file constraints-deploy.txt \
  requirements-core.txt \
  requirements-integrations.txt
python3.10 -m piptools compile \
  --generate-hashes \
  --strip-extras \
  --resolver=backtracking \
  --constraint constraints-deploy.txt \
  --output-file constraints/vm-python.lock \
  requirements-core.txt \
  requirements-integrations.txt
python3.10 tools/verify-deploy-constraints.py
python3.10 -m pip install --dry-run --require-hashes -r constraints/vm-python.lock
