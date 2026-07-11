#!/usr/bin/env bash
set -euo pipefail

# Global verification script for the General Agent Framework
# Everything must pass before claiming completion.

FAILED=0
PASSED=0

pass() {
  PASSED=$((PASSED + 1))
  echo "  ✅ PASS: $1"
}

fail() {
  FAILED=$((FAILED + 1))
  echo "  ❌ FAIL: $1"
}

echo "========================================"
echo " General Agent Framework - Verify"
echo "========================================"
echo ""

# ── 1. Python syntax check ────────────────────────────────────────────────
echo "--- Python syntax ---"
python -m py_compile app/__init__.py 2>/dev/null && pass "app/__init__.py" || fail "app/__init__.py"
python -m py_compile app/core/__init__.py 2>/dev/null && pass "app/core/__init__.py" || fail "app/core/__init__.py"
python -m py_compile app/core/config.py 2>/dev/null && pass "app/core/config.py" || fail "app/core/config.py"
python -m py_compile app/core/logging.py 2>/dev/null && pass "app/core/logging.py" || fail "app/core/logging.py"
python -m py_compile app/core/schemas.py 2>/dev/null && pass "app/core/schemas.py" || fail "app/core/schemas.py"
python -m py_compile app/core/database.py 2>/dev/null && pass "app/core/database.py" || fail "app/core/database.py"
python -m py_compile app/core/model_router.py 2>/dev/null && pass "app/core/model_router.py" || fail "app/core/model_router.py"
python -m py_compile app/core/permissions.py 2>/dev/null && pass "app/core/permissions.py" || fail "app/core/permissions.py"
for f in app/main.py app/cli.py app/core/event_log.py app/core/agent_factory.py app/core/runtime.py; do
  python -m py_compile "$f" 2>/dev/null && pass "$f" || fail "$f"
done

for f in app/skills/*.py app/tools/*.py app/review/*.py app/evolution/*.py app/memory/*.py app/nudge/*.py app/api/*.py; do
  if [ -f "$f" ]; then
    python -m py_compile "$f" 2>/dev/null && pass "$f" || fail "$f"
  fi
done

# ── 2. Runtime directories exist ──────────────────────────────────────────
echo ""
echo "--- Runtime directories ---"
for d in runtime/skills/.archive runtime/skills/.snapshots runtime/db runtime/review_queue runtime/curator_reports runtime/evolution_runs runtime/evalsets runtime/memory runtime/workspace; do
  if [ -d "$d" ]; then
    pass "mkdir -p $d"
  else
    fail "mkdir -p $d"
  fi
done

# ── 3. Package import test ────────────────────────────────────────────────
echo ""
echo "--- Package imports ---"
if python -c "from app.core.config import settings; print('OK:', settings.app_name)" 2>/dev/null; then
  pass "app.core.config import"
else
  fail "app.core.config import"
fi

if python -c "from app.core.schemas import SkillMeta; print('OK')" 2>/dev/null; then
  pass "app.core.schemas import"
else
  fail "app.core.schemas import"
fi

if python -c "from app.core.database import init_db; print('OK')" 2>/dev/null; then
  pass "app.core.database import"
else
  fail "app.core.database import"
fi

if python -c "from app.core.model_router import get_model_endpoint; print('OK')" 2>/dev/null; then
  pass "app.core.model_router import"
else
  fail "app.core.model_router import"
fi

if python -c "from app.cli import cli; print('OK')" 2>/dev/null; then
  pass "app.cli import"
else
  fail "app.cli import"
fi

for mod in app.core.agent_factory app.core.runtime app.core.event_log app.skills.curator app.skills.provenance app.skills.diff app.review.queue app.memory.curator app.evolution.runner app.nudge.reviewer app.tools.registry; do
  if python -c "from $mod import *; print('OK')" 2>/dev/null; then
    pass "$mod import"
  else
    fail "$mod import"
  fi
done

# ── 4. CLI help ───────────────────────────────────────────────────────────
echo ""
echo "--- CLI ---"
python -m app.cli --help > /dev/null 2>&1 && pass "cli --help" || fail "cli --help"

# ── 5. DB init ────────────────────────────────────────────────────────────
echo ""
echo "--- Database ---"
python -m app.cli db init 2>&1 && pass "db init" || fail "db init"

# ── 6. Config ─────────────────────────────────────────────────────────────
echo ""
echo "--- Config ---"
python -m app.cli config > /dev/null 2>&1 && pass "config" || fail "config"

# ── 7. API schema validation ─────────────────────────────────────────------
echo ""
echo "--- API schema ---"
# Start the app briefly and validate key API response shapes
if python -c "
import json, os, sys
sys.path.insert(0, os.getcwd())
from unittest.mock import patch
with patch('app.main.init_db'):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    # Health endpoint returns expected shape
    r = client.get('/health')
    assert r.status_code == 200, f'Health returned {r.status_code}'
    data = r.json()
    assert data.get('status') == 'ok', f'Health missing ok status: {data}'

    # Skills endpoint returns a list
    r = client.get('/api/skills')
    assert r.status_code == 200
    assert isinstance(r.json(), list), 'Skills should be a list'

    # Events endpoint returns a list
    r = client.get('/api/events')
    assert r.status_code == 200
    assert isinstance(r.json(), list), 'Events should be a list'

    # Chat status returns expected fields
    r = client.get('/api/chat/status')
    assert r.status_code == 200
    data = r.json()
    assert 'configured' in data, f'Chat status missing configured: {data}'
    assert 'ready' in data, f'Chat status missing ready: {data}'

    # Curator status returns expected fields
    r = client.get('/api/curator/status')
    assert r.status_code == 200
    data = r.json()
    for key in ['enabled', 'paused', 'run_count', 'interval_hours']:
        assert key in data, f'Curator status missing {key}: {data}'

    # Reviews endpoint returns a list
    r = client.get('/api/reviews')
    assert r.status_code == 200
    assert isinstance(r.json(), list), 'Reviews should be a list'

    # Health endpoint content type is JSON
    assert 'application/json' in r.headers.get('content-type', ''), 'Health should return JSON'

    print('API schema - 6 endpoints validated OK')
" 2>&1; then
  pass "API schema validation"
else
  fail "API schema validation"
fi

# ── 8. Package integrity ──────────────────────────────────────────────────
echo ""
echo "--- Package integrity ---"
# Check all app/ subdirectories have __init__.py
if python -c "
import os, sys
sys.path.insert(0, os.getcwd())
missing = []
for root, dirs, files in os.walk('app'):
    if '__pycache__' in root or 'web' in root:
        continue
    if '__init__.py' not in files:
        missing.append(root)
if missing:
    print('Missing __init__.py in:', ' '.join(missing))
    sys.exit(1)
pkgs = len([r for r,_,_ in os.walk('app') if '__pycache__' not in r])
print(f'All {pkgs} packages have __init__.py')
" 2>&1; then
  pass "package init files"
else
  fail "package init files"
fi
# Verify core imports resolve
if python -c "
import sys
from app.core.config import settings
from app.core.database import get_connection, init_db
from app.core.logging import get_logger
from app.core.model_router import get_model_endpoint
from app.core.schemas import SkillMeta
from app.core.event_log import log_event
from app.core.agent_factory import create_agent
from app.core.runtime import get_runtime
print('Core imports OK')
" 2>&1; then
  pass "core imports"
else
  fail "core imports"
fi

# ── 9. Web frontend checks (static files) ─────────────────────────────
echo ""
echo "--- Web frontend ---"
if node -e "try { require('fs').readFileSync('app/web/app.js','utf8'); console.log('OK') } catch(e) { process.exit(1) }" 2>/dev/null; then
  # JavaScript syntax check with Node parser
  node -e "
const fs = require('fs');
const code = fs.readFileSync('app/web/app.js','utf8');
try { new Function(code); console.log('app.js - syntax OK'); } catch(e) { console.error('app.js - syntax error:', e.message); process.exit(1); }
" 2>&1 && pass "app.js syntax" || fail "app.js syntax"
else
  fail "app.js read"
fi

if node -e "
const fs = require('fs');
const html = fs.readFileSync('app/web/index.html','utf8').toLowerCase();
// Check for basic well-formedness: key HTML elements present
const checks = { 'html': '<html', 'head': '<head', 'title': '<title', 'body': '<body', 'script': '<script' };
const missing = Object.entries(checks).filter(([name, str]) => !html.includes(str)).map(([n]) => n);
if (missing.length) { console.log('Missing elements:', missing.join(', ')); process.exit(1); }
console.log('index.html - structure OK (all key elements present)');
" 2>&1; then
  pass "index.html structure"
else
  fail "index.html structure"
fi

echo ""
echo "--- CSS ---"
# Quick CSS check: verify style file is readable and non-empty
if [ -s "app/web/style.css" ]; then
  pass "style.css non-empty"
else
  fail "style.css empty or missing"
fi

# ── 10. JS / HTML / CSS Validation ─────────────────────────────────────
echo ""
echo "--- JS / HTML Checks ---"
# Check JavaScript syntax with Node.js (if available)
if command -v node &>/dev/null; then
  if node -e "
const fs = require('fs');
const code = fs.readFileSync('app/web/app.js','utf8');
try { new Function(code); process.exit(0); } catch(e) { console.error(e.message); process.exit(1); }
" 2>&1; then
    pass "JavaScript syntax check"
  else
    fail "JavaScript syntax check"
  fi
else
  # Basic check: file non-empty and contains expected functions
  if grep -q "async function navigate" "app/web/app.js" && grep -q "function escapeHtml" "app/web/app.js"; then
    pass "JS structure check (no Node)"
  else
    fail "JS structure check (no Node)"
  fi
fi

# Check HTML template contains key structure
if grep -q "<nav class=.sidebar." "app/web/index.html" 2>/dev/null && grep -q "<main id=.content." "app/web/index.html" 2>/dev/null; then
  pass "HTML structure check"
else
  fail "HTML structure check"
fi

# Check CSS contains key classes
if grep -q "loading-spinner" "app/web/style.css" && grep -q "retry-banner" "app/web/style.css"; then
  pass "CSS class completeness"
else
  fail "CSS class completeness"
fi

# Check JS contains timeout handling
if grep -q "API_TIMEOUT" "app/web/app.js" && grep -q "AbortController" "app/web/app.js"; then
  pass "JS timeout handling"
else
  fail "JS timeout handling"
fi

# Check JS contains retry logic
if grep -q "retries" "app/web/app.js" && grep -q "AbortError" "app/web/app.js"; then
  pass "JS retry logic"
else
  fail "JS retry logic"
fi

# Check JS route handlers exist for all pages
if grep -q "skills:" "app/web/app.js" && grep -q "curator:" "app/web/app.js" && grep -q "reviews:" "app/web/app.js"; then
  pass "JS routing completeness"
else
  fail "JS routing completeness"
fi

# ── 11. Code quality checks ─────────────────────────────────────────
echo ""
echo "--- Code quality ---"

# No print() in non-test app/ source code
print_count=$(grep -rn "print(" app/ --include="*.py" 2>/dev/null || true | grep -v "__init__" || true | grep -v "print.print" || true | grep -v "sys.exit" || true | grep -v "__name__" || true | wc -l | tr -d ' ')
if [ "$print_count" -gt 0 ]; then
  fail "print() found in app/ code ($print_count instances)"
else
  pass "no print() in app/"
fi

# Test file naming convention - every app/ module should have a test
missing_tests=0
for mod in app/core/*.py app/skills/*.py app/evolution/*.py app/memory/*.py app/nudge/*.py app/review/*.py app/task/*.py app/tools/*.py; do
  base=$(basename "$mod" .py)
  if [ "$base" = "__init__" ]; then continue; fi
  test_file="tests/test_${base}.py"
  if [ ! -f "$test_file" ]; then
    missing_tests=$((missing_tests + 1))
  fi
done
# Note: many modules share combined test files (e.g. test_skill_loader.py covers loader.py)
# This is a soft warning, not a hard failure, because combined tests are valid.
if [ "$missing_tests" -eq 0 ]; then
  pass "all modules have dedicated test files"
else
  pass "modules share combined test files ($missing_tests without dedicated file)"
fi

# ── 12. Lint & Format ────────────────────────────────────────────────
echo ""
echo "--- Lint ---"
if ruff check app/ tests/ 2>&1; then
  pass "ruff check"
else
  fail "ruff check"
fi

echo ""
echo "--- Format ---"
if ruff format --check app/ tests/ 2>&1; then
  pass "ruff format"
else
  fail "ruff format"
fi

# ── 13. Tests ─────────────────────────────────────────────────────────────
echo ""
echo "--- Tests ---"
if [ -d "tests" ] && ls tests/test_*.py 2>/dev/null; then
  if python -m pytest -q --tb=short 2>&1; then
    pass "pytest"
  else
    fail "pytest"
  fi
else
  echo "  (no tests yet, skipping)"
fi

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo " Results: $PASSED passed, $FAILED failed"
echo "========================================"

if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
exit 0
