# Desktop parallel workflows

[简体中文](../zh/design/multi-task-execution.md) · [User guide](../desktop.md#3-create-a-translation-project)

## Scope

**Desktop-only backend implementation.** Multiple projects execute independently without a fixed workflow-count ceiling. Users keep the existing project pages and controls.

The earlier task-center proposal is withdrawn: no new navigation, task list, badge, bulk action, aggregate endpoint or frontend scheduler. Web parallelism will be designed and implemented separately. Web worker limits, queue delivery, exports and CLI execution are not changed.

## Execution boundaries

- Cross-project parse, preparation, translation, chapter translation, review and SRT workflows can overlap. [Project write exclusion](../../packages/backend/wenyi_backend/project_service.py#L46) still prevents conflicting work within one project.
- [Durable admission](../../packages/backend/wenyi_backend/job_service.py#L31) still creates the job/run identity and configuration snapshot before dispatch, compensating a rejected submission. Existing HTTP contracts and resume semantics remain unchanged.
- [LocalRuntime](../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L212) no longer serializes workflows through one consumer or a size-128 admission queue. Each accepted workflow owns an async supervisor.
- A [Desktop dispatcher](../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L198) owns one synchronous execution thread per invocation. Its one-thread executor is private to that invocation, not a shared fixed pool of workflow slots. Context is copied into that thread. Default executor capacity remains available for control requests, snapshots and maintenance.
- The [shared task adapter](../../packages/backend/wenyi_backend/workers/tasks.py#L316) calls an explicitly injected dispatcher. The [backend context](../../packages/backend/wenyi_backend/context.py#L160) defaults to `asyncio.to_thread`, retaining Web behavior. Shared code never imports Desktop or chooses a platform using global state.
- Two export consumers and the bounded export queue remain independent. Telemetry buffers remain bounded. Core orchestration and per-workflow batch/draft concurrency are unchanged.

## Ownership, pause and shutdown

Runtime ownership includes accepted supervisors, active worker tasks and cancellation cleanup. Catalog terminal status alone does not prove the runtime is idle.

1. Register the run before scheduling it. Duplicate delivery of an owned or terminal run does not create another execution.
2. One workflow's exception or pause does not cancel another workflow. Each real run constructs its own client, stop signal and project storage.
3. On timeout or shutdown, cancel the async worker once. The shared adapter signals cooperative stop and shields the synchronous operation until it exits and flushes checkpoints.
4. Stop accepting work before draining shutdown. Supervisors that have not entered the adapter persist interrupted/resumable state without starting model work. Also recover a worker cancelled before its first instruction.
5. Await every active writer and supervisor before stopping telemetry and releasing the workspace lock. Update safety includes supervisors even when the catalog already shows a terminal status.
6. Preserve existing orphan recovery and explicit user resume after a crash. Do not automatically restart all interrupted tasks.

Thread allocation failure is a normal task failure: persist the affected job/project error without cancelling siblings or leaving the project busy. A task that cannot yet stop remains owned; Python threads are not forcibly killed.

## Resource semantics

“No explicit upper limit” means no Desktop workflow-count guard, semaphore, configurable cap or hidden default-pool slot ceiling. It does not promise unlimited memory, threads, throughput or provider capacity.

Configured per-invocation model limits and the existing retry policy remain effective. [Each routed client](../../packages/core/wenyi_core/llm/router.py#L27) owns its own limiter; shared credentials do not turn those counters into an account-wide cross-project quota. Aggregate rate coordination and Web capacity management are outside this change.

More simultaneous projects can increase memory use and simultaneous model spending. Export limits and telemetry buffer bounds are resource policies for those components, not limits on workflow admission.

## Verification contract

All regression data is temporary; fake clients and synchronization events replace external model/parser services.

- Three and eight synchronous workflows enter before any can finish, even with the default executor restricted to one thread. Control work still executes on that default executor.
- Submit 129 workflows before execution to cover removal of the old queue ceiling. Shutdown handles both supervisors that have not started and children cancelled before adapter entry.
- Real offline HTTP workflows save three independent project checkpoints concurrently. Pausing or failing one leaves the others running; resume gets a new run identity without translating saved paragraphs again or duplicating usage.
- Same-project duplicate admission remains rejected. Project/run progress identity, independent clients and application contexts remain intact.
- Duplicate dispatch, thread allocation failure, export capacity, timeout diagnostics, all-writer shutdown, update blocking and crash recovery remain covered.
- Shared worker tests retain the default Web dispatcher and cancellation/export behavior. No frontend route, HTTP schema, migration or dependency change is required.

Run the affected suites and full Python suite because state/recovery are involved, repository Ruff check/format check, architecture/orchestrator/storage contracts, and `git diff --check`. Report unavailable PostgreSQL/Redis integration services separately. These offline checks do not establish production throughput, extreme-load stability or real-model translation quality.
