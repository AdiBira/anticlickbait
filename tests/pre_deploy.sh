#!/bin/bash
# Pre-deploy test suite. Run before every deployment.
# Usage: bash tests/pre_deploy.sh [base_url]
#
# Exit code 0 = all tests passed, safe to deploy
# Exit code 1 = at least one test failed, do NOT deploy

set -e

BASE_URL="${1:-https://anticlickbait.vercel.app}"
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$DIR")"

PASSED=0
FAILED=0
SKIPPED=0

run_test() {
  local name="$1"
  shift
  echo ""
  echo "================================================================"
  echo "  $name"
  echo "================================================================"
  if "$@"; then
    echo "  -> PASSED"
    PASSED=$((PASSED + 1))
  else
    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
      SKIPPED=$((SKIPPED + 1))
    else
      echo "  -> FAILED"
      FAILED=$((FAILED + 1))
    fi
  fi
}

echo "Pre-deploy test suite"
echo "Target: $BASE_URL"
echo ""

# 1. TypeScript type check
run_test "TypeScript type check" bash -c "cd '$ROOT/web' && npx tsc --noEmit --pretty"

# 2. Next.js build
run_test "Next.js production build" bash -c "cd '$ROOT/web' && npm run build"

# 3. Frontend stale state test
run_test "Frontend: stale state on navigation" node "$DIR/test_frontend_stale_state.mjs" "$BASE_URL"

# 4. Video navigation reset test
if [ -n "$TEST_VIDEO_A" ] && [ -n "$TEST_VIDEO_B" ]; then
  run_test "Frontend: video navigation resets state" node "$DIR/test_video_navigation_reset.mjs" "$BASE_URL"
else
  echo ""
  echo "SKIP: Video navigation reset test (set TEST_VIDEO_A and TEST_VIDEO_B env vars)"
  SKIPPED=$((SKIPPED + 1))
fi

# 5. Backend title test
if [ -n "$TEST_AUTH_TOKEN" ]; then
  run_test "Backend: title not from frontend" python3 "$DIR/test_backend_title_ignored.py"
else
  echo ""
  echo "SKIP: Backend title test (set TEST_AUTH_TOKEN env var)"
  SKIPPED=$((SKIPPED + 1))
fi

echo ""
echo "================================================================"
echo "  RESULTS: $PASSED passed, $FAILED failed, $SKIPPED skipped"
echo "================================================================"

if [ $FAILED -gt 0 ]; then
  echo "DO NOT DEPLOY - tests failed"
  exit 1
fi

echo "Safe to deploy"
exit 0
