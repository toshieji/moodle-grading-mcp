#!/usr/bin/env bash
# Daily grading poll (cron-friendly). Drives an LLM CLI to grade pending Moodle
# submissions as UNRELEASED drafts via the "moodle-grading" MCP server.
# Safety is enforced server-side (readyforreview / course allowlist / disclosure footer);
# this script never releases — a human reviews and releases in Moodle.
#
# Env:
#   GRADE_COURSE   (required) target Moodle course id
#   GRADE_PROMPT   (optional) grading prompt file (default: prompts/example_grade_prompt.md)
#   LLM_CLI        (optional) LLM CLI command (default: claude)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLM="${LLM_CLI:-claude}"
PROMPT_FILE="${GRADE_PROMPT:-$HERE/prompts/example_grade_prompt.md}"
GRADE_COURSE="${GRADE_COURSE:?set GRADE_COURSE to the target Moodle course id}"
LOG="$HERE/grade.log"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') grading poll start (course=$GRADE_COURSE) ====="
  "$LLM" -p "$(sed "s/{{COURSE}}/$GRADE_COURSE/g" "$PROMPT_FILE")"
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') grading poll end ====="
  echo ""
} >> "$LOG" 2>&1
