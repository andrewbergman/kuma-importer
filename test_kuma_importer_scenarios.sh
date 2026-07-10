#!/usr/bin/env bash
set -euo pipefail

# kuma-importer scenario test runner
#
# Safe by default:
# - runs help / validate / verify / dry-run / export / export-verify / filtering / logging tests
# - destructive tests are dry-run only unless RUN_DANGEROUS=1 is exported
#
# Usage examples:
#   ./test_kuma_importer_scenarios.sh \
#       --script kuma_importer.py \
#       --csv monitors_post_recovery.csv \
#       --txt example_domains.txt
#
# Optional environment variables:
#   PYTHON_BIN=python3
#   RUN_DANGEROUS=1          # enables real destructive tests (requires explicit --confirm values below)
#   TEST_CLIENT=ClientA       # client used for --client filter test
#   TEST_LIMIT=5             # limit used for --limit test
#   TEST_DELETE_SELECTED='example.com'   # selector for delete-selected tests
#   TEST_LOG='kuma_test.log' # log file path for --log-file test
#
# Notes:
# - This script assumes your Uptime Kuma credentials are already available via:
#     * kuma_importer.conf
#     * or environment variables (KUMA_URL / KUMA_USERNAME / KUMA_PASSWORD)
# - Safe scenarios do not modify state, except export and export-verify which create CSV/log files locally.

PYTHON_BIN="${PYTHON_BIN:-python}"
SCRIPT="kuma_importer.py"
CSV_FILE=""
TXT_FILE=""
TEST_CLIENT="${TEST_CLIENT:-ClientA}"
TEST_LIMIT="${TEST_LIMIT:-5}"
TEST_DELETE_SELECTED="${TEST_DELETE_SELECTED:-example.com}"
TEST_LOG="${TEST_LOG:-kuma_test.log}"
RUN_DANGEROUS="${RUN_DANGEROUS:-0}"
BACKUP_DIR="${BACKUP_DIR:-backups}"

usage() {
  cat <<'USAGE'
Usage:
  ./test_kuma_importer_scenarios.sh --script kuma_importer.py --csv monitors.csv [--txt domains.txt]

Required:
  --script   Path to kuma_importer.py
  --csv      Path to a known-good CSV file

Optional:
  --txt      Path to a TXT file for TXT import tests
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --script) SCRIPT="$2"; shift 2 ;;
    --csv) CSV_FILE="$2"; shift 2 ;;
    --txt) TXT_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

if [[ -z "$CSV_FILE" ]]; then
  echo "ERROR: --csv is required" >&2
  usage
  exit 2
fi

if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: script not found: $SCRIPT" >&2
  exit 2
fi

if [[ ! -f "$CSV_FILE" ]]; then
  echo "ERROR: CSV not found: $CSV_FILE" >&2
  exit 2
fi

if [[ -n "$TXT_FILE" && ! -f "$TXT_FILE" ]]; then
  echo "ERROR: TXT not found: $TXT_FILE" >&2
  exit 2
fi

TS="$(date +%Y%m%d-%H%M%S)"
EXPORT_FILE="exported_from_test_${TS}.csv"
EXPORT_VERIFY_FILE="export_verify_${TS}.csv"
TEST_RUN_LOG="scenario_test_${TS}.log"
STATUS=0

run_step() {
  local name="$1"
  shift
  echo
  echo "=================================================================="
  echo "TEST: $name"
  echo "COMMAND: $*"
  echo "=================================================================="
  if "$@"; then
    echo "RESULT: PASS - $name" | tee -a "$TEST_RUN_LOG"
  else
    echo "RESULT: FAIL - $name" | tee -a "$TEST_RUN_LOG"
    STATUS=1
  fi
}

echo "Starting kuma-importer scenario tests"
echo "Script: $SCRIPT"
echo "CSV:    $CSV_FILE"
echo "TXT:    ${TXT_FILE:-<not provided>}"
echo "Log:    $TEST_RUN_LOG"
echo "Dangerous tests enabled: $RUN_DANGEROUS"
echo

# 1. Basic help / CLI visibility
run_step "help output" \
  "$PYTHON_BIN" "$SCRIPT" --help

# 2. Validate CSV
run_step "validate CSV" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --validate

# 3. Verify / audit mode
run_step "verify CSV" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --verify

# 4. Standard dry-run
run_step "dry-run CSV" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --dry-run

# 5. Verbose dry-run
run_step "verbose dry-run CSV" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --dry-run --verbose

# 6. Client filter
run_step "client filter dry-run" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --client "$TEST_CLIENT" --dry-run

# 7. Limit test
run_step "limit dry-run" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --limit "$TEST_LIMIT" --dry-run

# 8. Quiet verify
run_step "quiet verify" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --verify --quiet

# 9. Log file output
rm -f "$TEST_LOG"
run_step "log file output" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --dry-run --log-file "$TEST_LOG"
if [[ -f "$TEST_LOG" ]]; then
  echo "Log file created: $TEST_LOG" | tee -a "$TEST_RUN_LOG"
else
  echo "Expected log file was not created: $TEST_LOG" | tee -a "$TEST_RUN_LOG"
  STATUS=1
fi

# 10. Export current monitors
run_step "export monitors" \
  "$PYTHON_BIN" "$SCRIPT" --export "$EXPORT_FILE"

# 11. Export + verify combined command
run_step "export and verify" \
  "$PYTHON_BIN" "$SCRIPT" --export-verify "$EXPORT_VERIFY_FILE"

# 12. Validate exported CSV
if [[ -f "$EXPORT_VERIFY_FILE" ]]; then
  run_step "validate exported CSV" \
    "$PYTHON_BIN" "$SCRIPT" --csv "$EXPORT_VERIFY_FILE" --validate
fi

# 13. TXT flows if a TXT file was provided
if [[ -n "$TXT_FILE" ]]; then
  run_step "validate TXT via dry-run" \
    "$PYTHON_BIN" "$SCRIPT" --txt "$TXT_FILE" --dry-run
fi

# 14. Delete-selected dry-run (safe)
run_step "delete-selected dry-run" \
  "$PYTHON_BIN" "$SCRIPT" --delete-selected "$TEST_DELETE_SELECTED" --dry-run

# 15. Delete-all dry-run (safe)
run_step "delete-all dry-run" \
  "$PYTHON_BIN" "$SCRIPT" --delete-all --dry-run

# 16. Delete-missing dry-run (safe)
run_step "delete-missing dry-run" \
  "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE" --delete-missing --dry-run

# 17. Optional dangerous tests (real destructive operations)
if [[ "$RUN_DANGEROUS" == "1" ]]; then
  echo
  echo "WARNING: RUN_DANGEROUS=1 is set. Real destructive tests will run."
  echo "A backup/export command should be available in your environment before proceeding."

  # Real delete-selected command using explicit confirmation.
  run_step "REAL delete-selected" \
    "$PYTHON_BIN" "$SCRIPT" --delete-selected "$TEST_DELETE_SELECTED" --confirm "DELETE"

  # Real delete-all command using explicit confirmation.
  run_step "REAL delete-all" \
    "$PYTHON_BIN" "$SCRIPT" --delete-all --confirm "DELETE ALL"

  # Restore from CSV after delete-all.
  run_step "REAL restore from CSV" \
    "$PYTHON_BIN" "$SCRIPT" --csv "$CSV_FILE"
fi

echo
echo "Scenario tests complete. Consolidated log: $TEST_RUN_LOG"
if [[ "$STATUS" -eq 0 ]]; then
  echo "OVERALL RESULT: PASS"
else
  echo "OVERALL RESULT: FAIL"
fi

exit "$STATUS"
