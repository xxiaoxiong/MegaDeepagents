# 多智能体团队任务 API

本文档基于当前项目扩展的多智能体支持设计，新增的 TeamTask 侧边 REST 接口。

所有端点使用前缀 `/team-tasks`。原有 `/tasks/*` 单 Agent 路径不受影响。

---

## 创建团队任务

```
POST /team-tasks
```

### 请求体

```json
{
  "goal": "分析当前项目并生成多智能体改造方案",
  "team": "software_dev_team",
  "max_rounds": 20,
  "review_required": true
}
```

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `goal` | string | 必填 | 任务目标 |
| `team` | string | `software_dev_team` | 团队模板名 |
| `max_rounds` | int | 20 | 最大轮次 |
| `review_required` | bool | true | 是否需要评审 |

### 响应

```json
{
  "task_id": "task_59718c4a",
  "room_id": "room_ee24f203eabc",
  "status": "running"
}
```

`status` 返回 `running` 表示已提交后台执行。

---

## 查询任务状态

```
GET /team-tasks/{task_id}
```

### 响应

```json
{
  "task_id": "task_59718c4a",
  "room_id": "room_ee24f203eabc",
  "goal": "分析当前项目...",
  "team_name": "software_dev_team",
  "status": "completed",
  "phase": "completed",
  "current_round": 5,
  "max_rounds": 20,
  "agents": ["Planner", "Coder", "Tester", "ReviewerAgent", "Finalizer"],
  "created_at": "",
  "updated_at": ""
}
```

---

## 获取消息流

```
GET /team-tasks/{task_id}/messages
```

### 响应

```json
[
  {
    "id": "msg_abc123",
    "from_agent": "system",
    "to_agent": null,
    "visibility": "broadcast",
    "message_type": "user_request",
    "content": "分析当前项目架构",
    "cause_by": "user",
    "reply_to": null,
    "requires_response": false,
    "artifact_refs": [],
    "evidence": [],
    "created_at": "2026-07-08T15:00:00"
  },
  {
    "id": "msg_def456",
    "from_agent": "Planner",
    "to_agent": null,
    "visibility": "broadcast",
    "message_type": "plan",
    "content": "计划内容...",
    "requires_response": false,
    "artifact_refs": [{"path": "/workspace/plan.md", "role": "plan", "produced_by": "Planner"}],
    "evidence": [],
    "created_at": "2026-07-08T15:00:05"
  }
]
```

消息在 from_agent 与 to_agent 之间传递，前端可通过 agent 过滤展示 Agent-to-Agent 消息流。

---

## 获取团队共享状态

```
GET /team-tasks/{task_id}/state
```

### 响应

```json
{
  "goal": "分析当前项目",
  "phase": "executing",
  "plan": "1. 阅读现有代码\n2. 输出分析报告",
  "current_round": 3,
  "max_rounds": 20,
  "open_questions": [],
  "issues": [
    {
      "id": "issue_xxx",
      "title": "缺测试",
      "description": "",
      "severity": "high",
      "status": "open",
      "owner": "Coder",
      "evidence": [],
      "created_at": "...",
      "resolved_at": null
    }
  ],
  "decisions": [
    {
      "id": "decision_xxx",
      "title": "采用方案A",
      "rationale": "...",
      "decided_by": "Planner",
      "alternatives": [],
      "created_at": "..."
    }
  ],
  "artifacts": [
    {
      "path": "/workspace/plan.md",
      "name": "",
      "role": "plan",
      "produced_by": "Planner",
      "size_bytes": 0,
      "created_at": "..."
    }
  ],
  "completed_steps": ["读取代码"],
  "blocked_steps": [],
  "review_status": "pending",
  "review_cycles": 0,
  "final_output": null
}
```

---

## 获取 Agent 列表

```
GET /team-tasks/{task_id}/agents
```

### 响应

```json
[
  {
    "name": "Planner",
    "role": "Planner",
    "goal": "把高层目标拆解为有序步骤，并指派负责人",
    "watched_message_types": ["user_request", "question", "handoff"],
    "allowed_tools": []
  }
]
```

---

## 获取轮次记录

```
GET /team-tasks/{task_id}/rounds
```

### 响应

```json
[
  {
    "round_number": 1,
    "selected_speaker": "Planner",
    "action_summary": "update_state(->)",
    "message_ids": ["msg_ab...", "msg_cd..."],
    "termination_reason": null,
    "created_at": "..."
  }
]
```

---

## 注入消息

```
POST /team-tasks/{task_id}/messages
```

人工向任务注入一条消息（以 system 身份 broadcast）。

### 请求体

```json
{
  "goal": "新增需求：添加用户认证功能",
  "team": "software_dev_team",
  "max_rounds": 10,
  "review_required": true
}
```

### 响应

```json
{
  "status": "injected"
}
```

---

## 取消任务

```
POST /team-tasks/{task_id}/cancel
```

### 响应

```json
{
  "status": "cancelled",
  "task_id": "task_xxx",
  "room_id": "room_xxx"
}
```

---

## 可用的 Team 模板

| 名称 | 描述 | Agent |
|---|---|---|
| `software_dev_team` | 软件开发团队 | Planner, Coder, Tester, ReviewerAgent, Finalizer |
| `research_team` | 研究团队 | ResearchPlanner, Researcher, Finalizer |

---

## 与现有 API 的关系

| 方面 | 单 Agent（/tasks/*） | 多 Agent（/team-tasks/*） |
|---|---|---|
| 执行模型 | 1 个 DeepAgent | 5 Agent 团队通信 |
| 消息模型 | 扁平 role（user/assistant/tool） | AgentMessage（from_agent/to_agent/visibility） |
| 状态 | 仅 tasks 表字段 | SharedTeamState（phase/issues/decisions） |
| 持久化 | task store | task store + team store |
| 兼容 | 不变 | 新增，不破坏原有 |
