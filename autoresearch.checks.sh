#!/bin/bash
set -euo pipefail
# Run the full test + lint suite, suppress verbose output
./dev check 2>&1 | tail -30
