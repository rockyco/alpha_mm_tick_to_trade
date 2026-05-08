#!/usr/bin/env bash
# Downloads the NASDAQ TotalView-ITCH 1.x sample (S010303-v2, 2003-01-03)
# from NASDAQ's free public archive at emi.nasdaq.com.
#
# This file is required by tb/hft_alpha_perf/test_hft_alpha_perf.py
# (drives 824 in-ROI MSFT depth events through the integrated pipeline).
#
# Output: /tmp/nasdaq_real/S010303.itch (212 MB, plain ASCII)

set -euo pipefail

DEST_DIR="${1:-/tmp/nasdaq_real}"
mkdir -p "$DEST_DIR"

ZIP_URL="https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/S010303-v2.zip"
ZIP_PATH="$DEST_DIR/S010303.zip"
ITCH_PATH="$DEST_DIR/S010303.itch"

if [[ -s "$ITCH_PATH" ]]; then
    echo "Already present: $ITCH_PATH ($(stat -c %s "$ITCH_PATH") bytes)"
    exit 0
fi

echo "Downloading NASDAQ ITCH-1 sample (58 MB compressed -> 212 MB unpacked)..."
curl -sL -o "$ZIP_PATH" "$ZIP_URL"
echo "Decompressing to $ITCH_PATH..."
unzip -p "$ZIP_PATH" > "$ITCH_PATH"
rm -f "$ZIP_PATH"

echo "Done. File MD5:"
md5sum "$ITCH_PATH"

# Sanity check: should be 212,330,490 bytes / md5 c1b56c02f4594b6e626569a61edb26de
expected_md5="c1b56c02f4594b6e626569a61edb26de"
actual_md5=$(md5sum "$ITCH_PATH" | awk '{print $1}')
if [[ "$actual_md5" != "$expected_md5" ]]; then
    echo "WARNING: md5 mismatch: expected $expected_md5, got $actual_md5"
    exit 1
fi
echo "MD5 verified."
