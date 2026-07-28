"""Smoke test: planner must filter LLM-injected unknown capabilities."""
from app.multiagent.planner import _llm_plan_to_taskgraph
from app.multiagent.agent_profile import get_capability_registry

graph = _llm_plan_to_taskgraph({
    "tasks": [{
        "id": "task-008",
        "title": "Review config",
        "objective": "review the configuration",
        "dependencies": [],
        "required_capabilities": ["reviewing", "file_read", "config", "output_artifact_type"],
        "output_artifact_type": "config",
    }]
}, "test goal")

node = graph.nodes["task-008"]
caps = set(node.required_capabilities)
print("Node caps:", caps)
print("Has reviewing:", "reviewing" in caps)
print("Has config:", "config" in caps)
print("Has file_read:", "file_read" in caps)
assert "reviewing" in caps, "primary role preserved"
assert "file_read" in caps, "tool role preserved"
assert "config" not in caps, "unknown config must be filtered"
assert "output_artifact_type" not in caps, "unknown output_artifact_type must be filtered"

r = get_capability_registry()
profile = r.find_best_worker(caps)
print("Best worker:", profile.id if profile else "None")
assert profile is not None, "must find a reviewer"
assert "reviewing" in set(profile.capabilities)

# Negative: no worker if we ask for only the bogus capability
no_match = r.find_best_worker({"config"})
assert no_match is None
print("OK: all assertions passed.")
