请对当前项目执行第一阶段改造：**修正多智能体运行时的核心语义，打通现有模块，消除文档、测试和真实代码之间的不一致。**

这是一个实际代码改造任务，不是只输出分析报告。你必须持续完成“审查 → 修改 → 测试 → 修复 → 回归验证”，直到本阶段验收项全部通过。不要中途停下来询问是否继续，不要只生成设计文档，不要留下无实现的 TODO、空壳接口或伪代码。

# 一、本阶段边界

本阶段不要大规模引入新的架构概念，不要急着实现动态团队、并行 Worker、向量记忆、分布式队列等后续能力。

本阶段只解决：

1. 当前 TeamRunner、TeamGraph、ReviewRepair、Termination、MessageBus、API、测试之间的真实性和一致性问题。
2. 为下一阶段统一运行时做好可复用的代码结构。
3. 确保现有固定团队模式能够可靠地运行、停止、恢复和验证。

# 二、工作原则

1. 先完整阅读以下代码，不要直接修改：
   - `app/multiagent/team_runner.py`
   - `app/multiagent/team_graph.py`
   - `app/multiagent/runtime_adapter.py`
   - `app/multiagent/speaker_selector.py`
   - `app/multiagent/termination.py`
   - `app/multiagent/review_repair.py`
   - `app/multiagent/action_guard.py`
   - `app/multiagent/bus.py`
   - `app/multiagent/room.py`
   - `app/multiagent/state.py`
   - `app/multiagent/store.py`
   - `app/api/routes_team.py`
   - `tests/test_multiagent_*.py`
   - `README.md`
   - `AGENTS.md`
2. 不要相信 README 和代码注释一定正确，以真实调用链为准。
3. 在改动前先运行现有测试并记录基线。真实模型测试必须与普通单元测试分离，不能让默认测试依赖外部模型服务。
4. 保留现有单 Agent 功能，不得破坏：
   - DeepAgents 执行
   - HITL
   - Skills
   - Memory
   - Workspace
   - MCP
   - LangSmith
   - 现有 API 基本兼容性
5. 每一次状态变化、消息发布、Artifact 变化和终止决策都必须具备可测试语义。

# 三、必须修复的问题

## 1. 修复 TeamGraph 与真实代码接口不兼容

当前 `TeamGraphRunner.node_select_speaker()` 调用 `SpeakerSelector.select()` 时使用了错误参数名或错误调用形式。

必须：

- 统一 `SpeakerSelector.select()` 的正式接口。
- 删除专门迎合错误接口的 Mock。
- 测试必须直接使用与生产实现一致的调用契约。
- 禁止通过宽泛的 `**kwargs` 掩盖接口错误。

## 2. 修复 TeamGraph 的轮次状态

当前 Graph State 中的 `round` 没有可靠递增。

必须：

- 每轮只递增一次。
- checkpoint 中保存正确轮次。
- `max_rounds` 和安全上限能够真实生效。
- resume 后从正确轮次继续，而不是重新从 0 开始。
- 增加“运行若干节点后中断，重新加载后继续”的真实恢复测试。

## 3. 消除 TeamRunner 与 TeamGraph 的业务语义分叉

请抽取一个可复用的单轮执行组件，例如：

```python
class TeamRoundExecutor:
    def select_speaker(...)
    def execute_speaker(...)
    def publish_actions(...)
    def apply_state_changes(...)
    def persist_round(...)
    def check_termination(...)
```

具体命名可根据项目风格调整，但必须做到：

- TeamRunner 和 TeamGraph 不再分别复制一套逻辑。
- 同一轮必须统一完成：
  - 选择 Agent
  - 加载 Inbox
  - 调用 Agent
  - Action 转 Message
  - MessageBus 发布
  - Inbox 投递
  - 消息已读处理
  - Shared State 更新
  - State 持久化
  - Round 持久化
  - SSE 事件
  - Trace metadata
  - productive delivery 判断
  - termination 判断

在本阶段可以暂时保留同步 TeamRunner 作为兼容入口，但它必须复用同一个 RoundExecutor，不能保留第二套业务逻辑。

## 4. 修复 ReviewRepairLoop 断链

当前 `process_review_result()` 返回 Critique Messages，但调用方没有可靠发布这些消息。

必须实现：

- Review 失败后生成的 critique 真正发布到 MessageBus。
- critique 投递到明确的修复责任 Agent。
- `requires_response=True` 能触发下一轮调度。
- 修复完成后能够重新进入 review。
- review cycle 必须保存在 SharedTeamState 或持久化层，不能只存在于 Python 对象内。
- Runner reload 后 review cycle 不丢失。
- `max_review_cycles` 使用本次运行的有效配置。
- 修复 `ReviewResult.raw` 未正确赋值等数据模型问题。
- 对未知 severity、缺失 owner、空 issues 做容错。

增加完整测试：

```text
Coder 创建产物
→ Reviewer 拒绝
→ 生成 Critique
→ Coder 收到
→ Coder 修复
→ 再次 Review
→ 通过
→ Finalizer
```

测试可以使用确定性 Fake Model，但必须覆盖真实主链。

## 5. 修复终止语义

禁止以下错误：

```text
达到 max_rounds
→ 标记 COMPLETED
```

重新定义终止状态：

- 验证成功：`COMPLETED`
- 达到最大轮次但未达到成功条件：`INCOMPLETE` 或 `FAILED`
- 超时：`TIMED_OUT`
- 用户取消：`CANCELLED`
- 连续无进展：`FAILED`
- 模型或工具不可恢复错误：`FAILED`
- 等待人工处理：`WAITING_HUMAN`

如现有枚举不支持，需要在保持迁移兼容的前提下增加状态。

完成必须满足明确条件，例如：

- final output 存在；
- 必要 review 已通过；
- 没有 blocker issue；
- 必要 Artifact 存在；
- 终止策略明确判定成功。

不能因为 Finalizer 输出一句完成或达到轮次上限就自动成功。

## 6. 修复 `review_required` 配置不生效

当前运行配置和 TeamSpec 默认配置存在混用。

请引入明确的有效策略计算，例如：

```python
EffectiveRunPolicy.from_team_and_run_config(...)
```

本次 RunConfig 必须能够覆盖 TeamSpec 默认值，并在：

- TerminationChecker
- ReviewRepairLoop
- TeamRunner
- TeamGraph
- API 返回值

中保持一致。

## 7. 修复 Prompt 构造结果未使用

检查 TeamRunner 中预先构造的 Prompt 是否被丢弃，以及 Adapter 是否再次构造了缺少 `team_agents` 的 Prompt。

必须确保每个 Agent 实际收到：

- 自己的角色、目标和权限；
- 当前团队成员的真实名称；
- 当前任务目标；
- 当前 Shared State；
- 经过筛选的 Inbox；
- 当前轮次；
- Action 输出协议。

删除无效变量和重复 Prompt 构造路径。

## 8. 修改未知 Agent 的路由策略

当前未知 `to_agent` 会回退到 broadcast，这在生产环境不安全。

改为：

- 默认拒绝未知目标。
- 写入结构化路由错误事件。
- 将消息放入 dead-letter 记录或返回 Orchestrator 重新决策。
- 只有配置显式开启 `allow_broadcast_fallback` 时，非敏感消息才可回退广播。
- Alias 必须使用确定性映射，不能依赖过度宽松的字符串包含关系。
- 增加目标 Agent 拼写错误、恶意名称和多义名称测试。

## 9. 实现真正可生效的取消

当前取消接口重新加载一个 Runner，不能可靠停止正在运行的线程。

必须：

- 在持久化状态中写入 cancel request。
- 主循环或 Graph 每个节点执行前检查取消状态。
- 长工具调用至少在边界处检查取消。
- 取消后禁止后续节点继续覆盖状态。
- 取消状态使用事务或 CAS 保护。
- 增加“运行中取消，后续轮次不再执行”的测试。

本阶段不要求立刻引入分布式队列，但取消不能只修改另一个内存对象。

## 10. 修正文档和注释漂移

清理以下错误表述：

- “多 Agent 不真正调用 LLM”
- “半模拟模式”
- 已存在模块被描述成已进入主流程，但实际上尚未接通
- TeamGraph 被描述为完整可恢复运行时，但测试尚未验证真实恢复
- 分层记忆或 ConflictResolver 被描述为生产主链能力

文档必须区分：

- 已完成并接入主链
- 实验性模块
- 规划中能力
- 已知限制

# 四、测试重构要求

测试目录至少区分：

```text
tests/unit/
tests/integration/
tests/e2e/
tests/live_model/
```

无法一次迁移全部目录时，也必须通过 pytest marker 明确隔离。

默认执行：

```bash
pytest -m "not live_model"
```

不得调用外部模型。

必须增加以下测试：

1. TeamGraph 使用真实 SpeakerSelector 接口。
2. round 正确递增。
3. checkpoint 恢复后不重复已提交副作用。
4. ReviewRepair 完整闭环。
5. max_rounds 不会误报 completed。
6. review_required=false 真正跳过评审。
7. 未知 Agent 默认进入路由错误而非广播。
8. cancel request 会阻止下一轮。
9. TeamRunner 与 TeamGraph 对同一确定性输入产生等价状态变化。
10. 文档中宣称的核心能力至少有一条对应集成测试。

# 五、本阶段验收标准

必须满足：

- 默认测试不依赖外部 LLM。
- 所有默认测试通过。
- TeamGraph 能完成至少一个确定性多轮任务。
- TeamGraph 能从 checkpoint 恢复。
- ReviewRepair 闭环真实接通。
- 达到最大轮次但未完成时不会标记成功。
- review_required 配置真正生效。
- 未知 Agent 不会默认广播。
- 取消操作真实阻止后续执行。
- TeamRunner 和 TeamGraph 不再复制两套完整业务逻辑。
- README、AGENTS.md 与真实代码一致。
- 不允许通过删除测试、放宽断言或捕获所有异常来制造通过。

# 六、最终交付

完成后输出：

1. 本阶段发现的根因。
2. 修改过的文件。
3. 关键架构调整。
4. 数据库或状态模型变化。
5. 执行过的测试命令。
6. 每条测试的真实结果。
7. 仍然存在但属于下一阶段的问题。
8. 给下一阶段保留的稳定扩展点。

必须实际完成代码修改和验证后再汇报，不要只输出建议。