#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Run all tests in the qootests collection. Exit non-zero if any fail.
dir=$(cd "$(dirname "$0")" && pwd)
status=0
for t in "$dir"/test_*.py; do
    if ! python3 "$t"; then
        status=1
        echo "$(basename "$t"): FAIL" >&2
    fi
done
exit $status
