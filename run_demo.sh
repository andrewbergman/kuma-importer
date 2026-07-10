#!/usr/bin/env bash

set -euo pipefail

CAST_FILE="demo.cast"

asciinema rec "$CAST_FILE" --overwrite -c "bash -lc '

# Resize terminal for clean recording (rows x cols)
printf \"\033[8;28;100t\" || true
stty cols 100 rows 28 || true

export PS1=\"\$ \"
clear
source kuma-env/bin/activate
export PYTHONWARNINGS="ignore"

pause() { sleep 1.5; }
run() {
  printf \"\n\$ %s\n\" \"\$*\"
  eval \"\$*\"
}

echo \"kuma-importer demo\"
echo \"==================\"
pause

echo
echo \"Step 0: Show the interactive menu\"
pause
run \"printf \\\"12\\\\n\\\" | python kuma_importer.py\"
pause

echo
echo \"Step 1: Import monitors from CSV (real execution)\"
pause
run \"python kuma_importer.py --csv example_monitors.csv --no-backup-before-apply\"
pause

echo
echo \"Step 2: Re-run import (idempotency check)\"
pause
run \"python kuma_importer.py --csv example_monitors.csv --dry-run\"
pause

echo
echo \"Step 3: Verify system state\"
pause
run \"python kuma_importer.py --csv example_monitors.csv --verify\"
pause

echo
echo \"Step 4: Demonstrate filtering (single client)\"
pause
run \"python kuma_importer.py --csv example_monitors.csv --client ExampleClient --dry-run\"
pause

echo
echo \"Step 5: Limit processing (subset test)\"
pause
run \"python kuma_importer.py --csv example_monitors.csv --limit 3 --dry-run\"
pause

echo
echo \"Step 6: Demonstrate delete safety (dry-run)\"
pause
run \"python kuma_importer.py --delete-all --dry-run\"
pause

echo
echo \"Step 7: Delete all monitors (real cleanup)\"
pause
run \"python kuma_importer.py --delete-all --confirm \\\"DELETE ALL\\\" --no-backup-before-apply\"
pause

echo
echo \"========================================\"
echo \"Thank you for having a look\"
echo \"I hope you find this helpful\"
echo \"========================================\"
pause
'"

echo ""
echo "✅ Demo recording complete: $CAST_FILE"
