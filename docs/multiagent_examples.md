# 多智能体团队任务示例

本文档提供后端 API 调用与 CLI 命令的示例。

---

## 前置条件

项目已启动服务：

```powershell
$env:PYTHONUTF8='1'
python -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

---

## 示例 1：使用 CLI 运行研发团队任务

```bash
python -m app.cli team run "分析当前项目架构，找出可改进之处" --team software_dev_team --max-rounds 10
```

可使用交互式查看：

```bash
# 列出模板
python -m app.cli team list
```

---

## 示例 2：使用 API 创建任务

```bash
curl -X POST http://127.0.0.1:8081/team-tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "读取项目现有代码并生成 API 文档",
    "team": "software_dev_team",
    "max_rounds": 15,
    "review_required": true
  }'
```

响应：

```json
{
  "task_id": "task_a1b2c3d4",
  "room_id": "room_e5f6g7h8",
  "status": "running"
}
```

---

## 示例 3：查询任务状态

```bash
curl http://127.0.0.1:8081/team-tasks/task_a1b2c3d4
```

---

## 示例 4：查看 Agent 之间的消息流

```bash
curl http://127.0.0.1:8081/team-tasks/task_a1b2c3d4/messages
```

JSON 数组中 `from_agent` / `to_agent` 显式标识每一条消息的发送方与接收方。

---

## 示例 5：查看共享团队状态

```bash
curl http://127.0.0.1:8081/team-tasks/task_a1b2c3d4/state
```

可查看 `phase`（当前阶段）、`plan`（计划）、`issues`（阻塞项）、`decisions`（决策）、`artifacts`（产物）、`review_status`（评审状态）。

---

## 示例 6：查看 Agent 列表

```bash
curl http://127.0.0.1:8081/team-tasks/task_a1b2c3d4/agents
```

---

## 示例 7：取消任务

```bash
curl -X POST http://127.0.0.1:8081/team-tasks/task_a1b2c3d4/cancel
```

---

## 示例 8：使用 Python SDK 包装

```python
import requests
import json

BASE = "http://127.0.0.1:8081"

def create_team_task(goal: str, team: str = "software_dev_team", max_rounds: int = 20):
    resp = requests.post(f"{BASE}/team-tasks", json={
        "goal": goal,
        "team": team,
        "max_rounds": max_rounds,
        "review_required": True,
    })
    return resp.json()

def get_task(task_id: str):
    return requests.get(f"{BASE}/team-tasks/{task_id}").json()

def get_messages(task_id: str):
    return requests.get(f"{BASE}/team-tasks/{task_id}/messages").json()

def get_state(task_id: str):
    return requests.get(f"{BASE}/team-tasks/{task_id}/state").json()

# 使用
task = create_team_task("分析当前项目结构，列出所有模块")
tid = task["task_id"]
print(f"Task created: {tid}")

import time
time.sleep(5)
info = get_task(tid)
print(f"Status: {info['status']}, Phase: {info['phase']}, Round: {info['current_round']}")

messages = get_messages(tid)
for m in messages:
    print(f"  [{m['message_type']}] {m['from_agent']} -> {m['to_agent'] or 'all'}: {m['content'][:80]}")
```

---

## 同目录下配合现有 API 混合使用

单 Agent 任务与多 Agent 任务可同时运行：

```bash
# 单 Agent
curl -X POST http://127.0.0.1:8081/chat -d '{"message": "写一个 test.py"}' -H "Content-Type: application/json"

# 多 Agent
curl -X POST http://127.0.0.1:8081/team-tasks -d '{"goal": "设计项目架构", "team": "software_dev_team"}' -H "Content-Type: application/json"

# 查询所有任务（仅单 Agent）
curl http://127.0.0.1:8081/tasks
```
