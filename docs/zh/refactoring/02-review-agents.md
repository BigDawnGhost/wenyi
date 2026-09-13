# 02 · 分离 Review 对话协议、持久化与仲裁

[总览](README.md) · [English](../../refactoring/02-review-agents.md)

状态：B 批次已实施。基线：`7471256`；实施保留了后续可恢复 provider 中断修复。

实施结果：

- `review/models.py` 管理结果、稳定身份和段落引用；`review/conflicts.py` 管理归一化、冲突分组及决策应用。包级导出仅包含纯类型。
- `ReviewTrace` 与证据查询协议隔离具体存储和证据实现；`pipeline/review_checkpoint.py` 保留原文件名、活动轮次、原子写入及事件规则。
- `agents/review_actions.py` 管理共享协议，`ConversationState` 独占回放消息、可引用证据和请求去重状态。审校块与 `agents/review_arbiter.py` 各自管理提示词和最终校验。

内存测试逐一中断、恢复全部 9 个保存边界，提取前后对照了捕获的快照、事件和模型消息。递归架构测试固定了新依赖方向。operation ID、提示词、磁盘格式、保存顺序和 fallback 策略不变；本次不调整翻译质量或 Review 终止策略。

全书会话与检查点协调仍属于方案 01 的后续范围。

## 证据

[`agents/review_loop.py`](../../../trans_novel/agents/review_loop.py) 审查时共 1,006 行，其中 `_ActionLoop.run()` 为 300 行，`ReviewAgentLoop.review_chunk()` 为 200 行，`ReviewConflictArbiter.arbitrate()` 为 170 行；模块还包含问题身份、归一化、冲突分组和仲裁结果应用。

审查基线中，第 17 行导入的具体 `ReviewRunStore` 是存储依赖，不只是纯 Review 模型；`_ActionLoop` 直接加载和写入 trace。当时的架构测试会拦截 pipeline 导入，却不会拦截该具体 Review 存储依赖。实施后的接口与递归架构测试已固定这一更窄的依赖边界。

## 建议职责

- `review/models.py`：纯 `ReviewOutcome`、`ReviewLoopOutcome`、问题与一致性类型、稳定候选 ID。将纯类型从 `review/run_store.py` 移出，调用方同步更新。
- `review/conflicts.py`：归一化、冲突分组、应用已验证的仲裁结果，不依赖模型或存储。
- `review/contracts.py`：小型、有类型的 trace 端口与证据查询契约。trace 仅支持读取对话快照、在持久化边界保存、记录诊断事件，不能访问正式章节或书级用量。
- `agents/review_actions.py`：请求证据与最终响应协议、对话重放；使用内存 `ConversationState` 和 trace 端口，保留请求校验及回退行为。
- `agents/review_loop.py`：审校块提示词准备及最终问题校验。
- `agents/review_arbiter.py`：冲突提示词准备及最终仲裁校验。
- `pipeline/review_checkpoint.py`：用 `ReviewRunStore` 实现 trace 端口，绑定当前 Review 与轮次；agents 不导入该适配器。

不建设“所有 agent 通用框架”。`_ActionLoop` 已经有两个共享同一协议的实际调用方，只抽取这个协议，不强迫 Translator、Polisher、ReviewFixer 套同一模型。

## 对话恢复与证据规则

保留完成和 fallback trace 的直接复用、推理身份失效检查、重放消息字节、已解析响应复用，以及只补发未完成请求。请求前、取得原始输出后、解析后、执行证据后的保存时点不变；不能因为加了抽象端口，就把写入集中延迟到结束。

保留证据轮次上限、请求去重、可引用 ref 校验、最终 complete 标记、限制新问题只能落在当前块内，以及仲裁必须重新查询继承证据后才能引用的规则。未来向量检索可以实现查询契约，但检索文本不能绕过原文 ref 和验证，直接变成人物亲属关系的确定证据。

## 切片与验收

1. 先提取纯身份、冲突类型和函数，仓内调用与测试同步移除旧私有导入路径。
2. 增加 trace 适配器，再移动 `_ActionLoop`，不改 trace 结构和 operation ID。
3. 移出仲裁器，让 `review_loop.py` 专注审校块。

复用 `tests/test_review_agent.py` 的完成、fallback、在途请求、已解析最终响应、无额外模型调用的证据重执行、减少证据轮次、未知 ref、未解决仲裁等测试。增加内存 trace 端口 fixture，逐个比较持久化快照与模型消息。

扩展架构测试，禁止 agents 导入具体 `review.run_store`，并递归检查新 agent 包；纯 Review 导出保持显式声明。运行 Review、路由续跑、用量和架构测试；与 01 装配时运行全量测试。
