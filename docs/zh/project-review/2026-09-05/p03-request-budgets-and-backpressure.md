# P03 · 建立统一请求预算、并发限制与取消机制

[返回索引](README.md) · [English](../../../project-review/2026-09-05/p03-request-budgets-and-backpressure.md)

类型：未来规划，尚未实施；依赖与顺序见索引 · 2026-09-05

## 当前基础与证据

项目已有共享重试、线程安全用量统计、阶段指标和 Review 轮次上限。SRT 并发固定为 100，批次外围还有捕获全部异常的 3 次尝试；这可能重复执行已在 provider 内耗尽的重试。不同领域线程池也没有共同的请求配额。此项关注可控的运行资源，不以增加并发作为质量或速度的保证。

## 目标

让用户在运行前能理解成本量级，在运行中能限制并发和 token 消耗，在失败/取消后保留可继续的状态。首先约束可测的调用数与 token，货币预算仅在提供明确模型价格表时计算，并标识估计。

## 实施步骤

区分传输重试与输出格式修复：传输仍只交给 `llm/retrying.py`；业务仅对可恢复的输出协议问题重试。将字幕 batch/overlap/concurrency 变成受约束且纳入状态身份的配置。建立 provider 无关的共享请求许可与预算接口，由领域服务获取配额，避免 Orchestrator 拥有线程池。加入停止提交新任务、完成在途请求、落盘检查点的取消路径。

## 预算行为

P06 的 embedding/重排、P08 的局部风格分析和 P09 的跨章取证分别记录调用类型与阶段；本地 embedding 记录耗时/资源，远程 embedding 按服务实际 usage 或明确估计记账，不假装是聊天输出 token。P07 命中记忆属于复用指标，不产生虚假的模型调用；有条件验证另计实际请求。

提供按阶段的预估、累计和剩余额度，说明预估基于字符/抽样而不是保证。并发任务提交前保留预算，结束后按实际 usage 调整；在途请求可能使软预算超出，必须明确超出上限的定义。429、超时和永久错误分别统计，避免失败风暴掩盖实际进度。

## 验收标准

离线模拟 401、429、超时、畸形输出、并发响应和取消；永久错误不被业务层重复重试，传输重试次数有明确上界。预算耗尽后不再提交新调用、不会标记未完成段 done，恢复不重复收费。配置模型、默认模板、示例、CLI 与双语文档同步。

## 依赖、节奏与取舍

依赖 F03/F04/F09 的字幕状态语义与 P01 的账本契约。建议 5–8 日分三次小交付：重试边界与参数、配额、预算与取消。降低字幕并发可能增加耗时，但减少服务拥塞；不根据未核实的当前 provider 价格承诺节省金额。

## 代码与文档依据

- [trans_novel/srt/translate.py:22](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L22)
- [trans_novel/srt/translate.py:73](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L73)
- [trans_novel/llm/retrying.py:271](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/llm/retrying.py#L271)
- [trans_novel/llm/usage.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/llm/usage.py#L1)
- [trans_novel/pipeline/metrics.py:118](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/metrics.py#L118)
- [trans_novel/config.py:126](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/config.py#L126)
