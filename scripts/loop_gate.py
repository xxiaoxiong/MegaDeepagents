#!/usr/bin/env python3
"""Stop gate for the long-running implementation loop.

Invoked by the Stop hook. Outputs a JSON decision:
  {"decision": "block", "reason": "..."}  — continue working
  {"decision": "allow", "reason": "..."}  — loop may stop
"""

import json
import sys
from pathlib import Path

root = Path.cwd()
state_path = root / ".claude" / "loop-state.json"

try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except Exception as e:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": f"Continue. Cannot read .claude/loop-state.json: {e}. Fix the state file and continue.",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

iteration_count = int(state.get("iteration_count", 0))
target_iterations = int(state.get("target_iterations", 500))
tasks_completed = bool(state.get("tasks_completed", False))
verify_passed = bool(state.get("verify_passed", False))
review_passed = bool(state.get("review_passed", False))
blockers = state.get("known_blockers", [])

if blockers:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": f"Continue. known_blockers is not empty: {blockers}. Resolve blockers if possible.",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

if iteration_count < target_iterations:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": f"Continue. Only {iteration_count}/{target_iterations} iterations complete. Read docs/TASKS.md, perform next improvement cycle, run verification, update loop-state and loop-log.",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

if not tasks_completed:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": "Continue. docs/TASKS.md is not fully complete according to .claude/loop-state.json.",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

if not verify_passed:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": "Continue. Verification has not passed. Run bash scripts/verify.sh, fix failures, set verify_passed=true only after it exits 0.",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

if not review_passed:
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": "Continue. Independent review has not passed. Use loop-verifier, fix issues, then set review_passed=true only after PASS.",
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)

print(
    json.dumps(
        {"decision": "allow", "reason": "All stop gate conditions satisfied."},
        ensure_ascii=False,
    )
)
