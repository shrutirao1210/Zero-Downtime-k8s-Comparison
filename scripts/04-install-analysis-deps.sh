#!/usr/bin/env bash
# Install Python analysis dependencies
set -euo pipefail
pip3 install numpy scipy matplotlib pandas
echo "Analysis dependencies installed."
python3 -c "import numpy, scipy, matplotlib, pandas; print('All imports OK')"
