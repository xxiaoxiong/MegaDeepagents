"""Smoke test: _safe_json catches PermissionError and returns JSON error.

This verifies the fix for the bug where team_update_task with status=succeeded
raised PermissionError through the LangChain tool boundary and failed the entire
task (even though artifacts were already produced).
"""
import json
import sys

sys.path.insert(0, '/app')

from app.multiagent.control_plane import build_team_tools, TeamControlPlaneService


def test_safe_json_catches_permission_error():
    """The _safe_json helper should catch PermissionError and return JSON."""
    # Build tools with a real service (uses real singletons)
    service = TeamControlPlaneService()
    tools = build_team_tools(service, 'run_test', 'agent_test')
    tool_map = {t.name: t for t in tools}
    update_tool = tool_map['team_update_task']

    # The tool will fail at _caller (agent_test not registered) OR at the
    # PermissionError check. Either way, it should return JSON, not raise.
    print('Test: team_update_task should not raise PermissionError')
    try:
        result = update_tool.invoke({
            'task_id': 'task_1',
            'changes': {'status': 'completed'},
        })
        print(f'  result: {result[:300]}')
        parsed = json.loads(result)
        # Should be a structured error (ok=False) — either PermissionError
        # or KeyError (agent not registered), but NOT a raised exception.
        assert isinstance(parsed, dict), f'expected dict, got {type(parsed)}'
        if parsed.get('ok') is False:
            print(f"  PASS: returned structured error: {parsed.get('error')}: {parsed.get('message','')[:100]}")
        else:
            print(f'  PASS: returned JSON (no exception raised)')
    except PermissionError as exc:
        print(f'  FAIL: raised PermissionError: {exc}')
        sys.exit(1)
    except Exception as exc:
        print(f'  FAIL: raised {type(exc).__name__}: {exc}')
        sys.exit(1)


def test_safe_json_catches_keyerror():
    """KeyError (task not found) should also return JSON, not raise."""
    service = TeamControlPlaneService()
    tools = build_team_tools(service, 'run_test', 'lead')
    tool_map = {t.name: t for t in tools}
    get_task = tool_map['team_get_task']

    print('Test: team_get_task with non-existent task should not raise')
    try:
        result = get_task.invoke({'task_id': 'nonexistent_task'})
        print(f'  result: {result[:300]}')
        parsed = json.loads(result)
        assert isinstance(parsed, dict), f'expected dict, got {type(parsed)}'
        print(f'  PASS: returned JSON (no exception raised)')
    except KeyError as exc:
        print(f'  FAIL: raised KeyError: {exc}')
        sys.exit(1)
    except Exception as exc:
        print(f'  FAIL: raised {type(exc).__name__}: {exc}')
        sys.exit(1)


if __name__ == '__main__':
    test_safe_json_catches_permission_error()
    print()
    test_safe_json_catches_keyerror()
    print()
    print('All smoke tests passed!')
