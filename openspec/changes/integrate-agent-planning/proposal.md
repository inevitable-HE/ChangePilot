## Why

现有规划 Agent、RAG 知识库和可靠执行运行时已经分别实现，但运营 API 仍使用固定工作流定义，导致用户从控制台看不到真实的 Agent 规划过程，也无法证明不同目标会形成不同计划。

## What Changes

- 将结构化变更请求接入真实的 LangGraph 规划、SQLite RAG、确定性计划校验和工作流映射链路。
- 默认使用可复现的离线规划模型，并允许显式切换 DeepSeek 网关。
- 提供只读规划轨迹接口，在控制台展示规划节点、RAG 证据、校验结果、知识快照和模型用量。
- 新增只读就绪度检查目标，验证 Agent 能依据目标选择不同工具和审批边界。

## Capabilities

### New Capabilities

- `integrated-agent-planning`: 定义运营请求到 Agent 规划、证据展示和可靠执行的端到端行为。

### Modified Capabilities

- `operations-console`: 展示 Agent 规划轨迹并允许选择只读就绪度场景。

## Impact

- 运营服务成为规划模块与执行模块的组合边界。
- 新增规划轨迹 API 和控制台视图。
- 默认演示仍完全离线，不增加 Docker 或在线模型依赖。
