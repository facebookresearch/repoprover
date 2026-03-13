# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

#!/bin/bash
# Set up the toy example project for testing repoprover.
#
# This script:
#   1. Copies the project files to a working directory
#   2. Initializes git
#   3. Fetches Mathlib and builds the project
#
# Usage:
#   bash examples/toy_project/setup.sh [TARGET_DIR]
#
# TARGET_DIR defaults to /tmp/repoprover-toy-test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-/tmp/repoprover-toy-test}"

if [ -d "$TARGET" ]; then
    echo "Target directory already exists: $TARGET"
    echo "Remove it first or pass a different path."
    exit 1
fi

echo "=== Setting up toy project in $TARGET ==="

# Copy project files (exclude setup.sh itself)
mkdir -p "$TARGET"
cp -r "$SCRIPT_DIR"/lakefile.lean \
      "$SCRIPT_DIR"/lean-toolchain \
      "$SCRIPT_DIR"/TestProject.lean \
      "$SCRIPT_DIR"/TestProject \
      "$SCRIPT_DIR"/manifest.json \
      "$SCRIPT_DIR"/CONTENTS.md \
      "$TARGET"/
mkdir -p "$TARGET"/issues

# Initialize git
cd "$TARGET"
git init -b main
git add -A
git commit -m "Initial setup"

echo ""
echo "=== Fetching Mathlib (this may take a few minutes) ==="
lake update

echo ""
echo "=== Building project ==="
lake build

echo ""
echo "=== Done! ==="
echo ""
echo "Run repoprover with:"
echo "  python -m repoprover run $TARGET --pool-size 2 --verbose"
