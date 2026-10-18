# Policy 内核使用指南

Policy 内核位于 `toolflow_core.common.policy`。Policy 表达不可变配置，Manager 提供异步读取与“先持久化、后发布”的更新，Repo 负责按逻辑身份复用 Manager。DB / 文件客户端由应用通过 `PolicyStore` 接入。

实现与验证状态见 [2026-10-18 实施方案](plan/261018-policy-manager-repo-plan.md)。本文代码使用 Python 3.13；示例中的 `await` 需放在异步函数内，`store` 和配置 key 由应用提供。

## 从原同步接口迁移

| 原用法 | 当前用法 |
| --- | --- |
| `PolicyManager(policy)` | 优先 `await repo.get(key)`；直接构造必须传入 `StoredPolicy`、`key` 和 `store` |
| 同步 `snapshot / replace / update` | 全部使用 `await`；Resolver 的 `resolve` 和 Gateway 的 `prepare` 同样异步 |
| 内存更新后由调用方选择保存 | Manager 统一先保存再发布，不提供默认空写入器，也不需要在成功更新后重复保存 |

优先通过共享 Repo 获取 Manager。直接构造的独立 Manager 不受 Repo 的实例注册与阻断保留机制管理，不适合用于同一逻辑身份的全局共享入口。

## 定义领域配置

```python
from collections.abc import Mapping
from pydantic import Field
from toolflow_core.common.policy import BasePolicy


class RuntimePolicy(BasePolicy):
    timeout_ms: int = Field(default=3000, gt=0)
    retry_limit: int = Field(default=2, ge=0, le=10)
    allowed_tools: frozenset[str] = frozenset()
    tool_timeouts: Mapping[str, int] = Field(default_factory=dict)
```

支持标量 `str`、`bool`、`int`、有限 `float`、`None`，以及 `Literal`、联合类型、嵌套 `BasePolicy`、`tuple`、`frozenset` 和 `Mapping[str, T]`。集合成员同样必须是支持的类型。

领域字段不使用 `list`、`dict`、`set`、`Any` 或客户端对象；不支持的声明在模型完成初始化时抛出 `PolicyImmutabilityError`。原始 JSON / DB 数据可以使用普通字典和列表，模型负责转换并递归冻结。保留 BasePolicy 的冻结、默认值校验和重新校验配置，不添加可变私有状态，校验器与查询方法保持纯函数。

`PolicyType.from_config(raw)` 校验并隔离原始数据；`policy.to_config()` 返回独立的普通容器；`policy.with_changes(**changes)` 执行顶层字段替换及完整校验，不递归合并 Mapping。不要使用不会校验更新值的 `model_copy(update=...)` 绕过这些入口。Manager 仍会在加载及发布边界重新校验。

## 实现数据源

应用实现 `PolicyStore[TPolicy]` 的两个异步方法：

```python
from toolflow_core.common.policy import PolicyStore, StoredPolicy


class RuntimePolicyStore(PolicyStore[RuntimePolicy]):
    async def load(self, key: str) -> StoredPolicy[RuntimePolicy] | None:
        # Read one complete policy/version pair from the authoritative source.
        ...

    async def save(
        self,
        key: str,
        candidate: RuntimePolicy,
        *,
        expected_source_version: str,
    ) -> str:
        # Atomically compare the source version and commit the entire candidate.
        # Return a fresh version only after persistence confirms the commit.
        ...
```

`load()` 返回已校验的 `StoredPolicy(policy, source_version)`，配置不存在时返回 `None`。版本为非空字符串，每次成功保存都变化，删除再创建也不能复用旧版本。`save()` 不得隐式修改候选配置的业务字段，不能把版本不匹配转换成无条件覆盖。

| 写入结果 | 适配器行为 | Manager 行为 |
| --- | --- | --- |
| 确认提交 | 返回新源版本 | 发布新内存快照 |
| 版本不匹配或记录已删除 | 抛 `PolicySourceConflictError` | 保持旧快照，阻断新访问，等待显式 reload |
| 确认未提交且没有在途写入 | 抛 `PolicyWriteRejectedError` | 保持旧快照，允许后续读取和重试 |
| 提交结果不确定 | 抛 `PolicyWriteUncertainError` | 阻断该 key，等待显式恢复 |
| 其他写入异常或内部任务取消 | 传播原异常 | 按结果不确定处理 |

DB 适配器应使用源端事务 / CAS，文件适配器需实现版本检查、写者互斥和完整替换。单纯原子替换文件不能防止旧写者覆盖新内容。同步 I/O 应在线程中执行；取消 `to_thread()` 的等待不表示底层写入已停止。

## 获取共享 Manager

```python
from toolflow_core.common.policy import CachetoolsPolicyManagerRepo

# Construct once per application policy namespace; inject this repo into consumers.
repo = CachetoolsPolicyManagerRepo(store, ttl=60.0, maxsize=1024)

# Inside an async operation:
manager = await repo.get(agent_key)
snapshot = await manager.snapshot()
timeout = snapshot.policy.timeout_ms
```

Repo 缓存 Manager 对象引用，而不是复制 Policy。每个 Repo 对应一种 Policy 及源命名空间，key 必须稳定、无拼接歧义，并包含必要的用户隔离维度：

| Scope | 逻辑身份 |
| --- | --- |
| Application | user_id |
| Agent | user_id + agent_id，不包含 session_id |
| Session | user_id + session_id |

同一 Agent 在多个 Session 中获取同一个 Manager。应用需要复用同一个 Repo；不同 Repo / 进程不共享 Python 对象身份。

key 的组合应使用明确编码，例如 `agent_key = json.dumps([user_id, agent_id], ensure_ascii=False, separators=(",", ":"))`（需 `import json`），并与源存储使用的 key 一致。初次回源查无配置时，`get()` 抛出 `PolicyManagerNotFoundError`，不缓存缺失结果；源读取和缓存异常直接传播。

TTL 仅控制 Repo 的强引用保留时间，不表示 Policy 新鲜度。读命中和 Manager 更新不续期；容量淘汰可以早于 TTL。缓存未命中后先检查弱引用注册表：有存活实例就重新缓存并返回它，没有才读源并创建。对象仍存活时始终保持身份；所有引用释放后允许回收，下次按持久化配置重建。过期对象的物理清理由后续缓存访问触发，不启动后台线程。

默认实现要求 `ttl` 为有限正数（秒）、`maxsize` 为正整数；默认值分别是 60 和 1024。Repo 通过固定 64 把异步分段锁协调 Manager；不同 key 可能因分段碰撞而等待。

需要自定义缓存时，继承 `BasePolicyManagerRepo`，调用 `super().__init__(store, ttl=...)` 并实现 `_read_cache()`、`_read_source()`、`_write_cache()` 三个异步钩子；读源钩子须与写入 Store 使用相同权威源。基类提供 get、身份管理和恢复流程。缓存必须保存原对象并支持 TTL，不能把 Manager 序列化到 Redis 后重建。

自定义缓存读取时，需清理已过期项占用的强引用，再让基类查询弱引用注册表。仅返回 None 却仍保留过期对象，会使其一直被视为存活，无法按预期回收。三个钩子在共享锁内执行，不得重入相同 Repo / Manager。

## 修改配置

```python
from toolflow_core.common.policy import PolicyConflictError, PolicySourceConflictError

base = await manager.snapshot()
candidate = base.policy.with_changes(timeout_ms=5000)
try:
    published = await manager.replace(candidate, expected=base)
except PolicyConflictError:
    # Re-read the current snapshot and decide how to merge; no hidden retry.
    raise
except PolicySourceConflictError:
    # Another source writer won. Reload explicitly before deciding to retry.
    await repo.reload(agent_key)
    raise

# Alternatively, run a synchronous pure transform exactly once.
base = await manager.snapshot()
published = await manager.update(
    lambda policy: policy.with_changes(retry_limit=3), expected=base
)
```

`expected` 必须是该 Manager 当前返回的快照原对象，其他 Manager、伪造或过期快照均被拒绝。候选 Policy 在锁外校验；锁内依次检查快照身份、等待源端条件保存、整体替换内存快照。保存期间 `snapshot()`、同 key 的 Repo 获取及重载均等待，事件循环仍能处理其他分段的工作。transform 是同步纯函数，不做阻塞 I/O，不自动重试。

`revision` 从 0 开始，是本地发布计数；`source_version` 用于源端 CAS。重建 Manager 后 `revision` 可以重新从 0 开始，不能跨实例排序。旧快照始终保持原值；已经取得的快照和正在运行的业务不会被撤回。

开始提交之前可以取消；开始提交之后延迟传播调用者取消，等完整的保存及发布结束后才解锁。多次取消也不提前释放锁。调用方收到取消仍可能已经提交，不应盲目重试；应用关闭时应等待在途更新完成。

## 显式刷新与恢复

```python
# Observe external source changes while keeping the live manager object.
manager = await repo.reload(agent_key)

# Only after an uncertain write: the resolver must settle the original operation
# and return the authoritative committed record (or None for confirmed absence).
manager = await repo.recover(agent_key, resolve_previous_write)
```

源版本不变的 reload 保留原快照对象；源版本变化时校验并发布新快照。不允许同一源版本对应不同数据。加载失败不覆盖当前快照；确认源记录被删除时，存活 Manager 会被阻断，避免旧句柄继续取得配置。

提交结果不确定时，`get()`、`snapshot()`、更新和普通 `reload()` 都不能绕过阻断；恢复标记及被阻断 Manager 的额外强引用不随 TTL 或调用者引用消失。resolver 的签名为 `Callable[[str], Awaitable[StoredPolicy[TPolicy] | None]]`，必须确认先前写入已经结束，再读取权威状态；仅再次读源或再次超时不满足恢复契约。在有效源状态安装之前，resolver / 校验失败或取消会保留已有阻断。

刷新与恢复的顺序是“读取并校验源状态 → 安装内存快照并解除阻断 → 回写缓存”。最后一步失败或被取消时，异常仍向上传播，但已经安装的状态不会回滚，Manager 也不会仅因缓存失败而重新阻断。缓存恢复可用后，调用方可以重新 `get()` 缓存这个原对象，不需要重复源写入。

Repo 与 Manager 归属于一个事件循环，跨事件循环直接使用会报错。外部线程使用 `asyncio.run_coroutine_threadsafe(coro, owner_loop)`；不能在 owner_loop 所在线程调用其 Future.result()。进程内锁不提供跨进程读视图同步：源端 CAS 防止覆盖，其他进程的缓存需要显式刷新或额外同步机制。

Gateway 通过注入的 `resolve_keys(ToolCall) -> PermissionManagerKeys` 确定三个作用域的 key。当前 `prepare()` 顺序获取 Session、Agent、Application 三个 Manager，然后调用异步 Resolver；短路发生在快照读取与权限规则解析阶段，不跳过前面的 Manager 获取。因此，即使 Session 配置将拒绝调用，下游 Manager 获取失败仍会使 prepare 失败。缺少可选身份时由应用映射到明确的继承配置，不能把配置不存在或存储异常当作允许。三个作用域之间不提供原子视图。

## 示例与验证

SQLite 示例会实际提交配置，并通过独立源读取验证保存结果：

```bash
uv run --package toolflow-core python packages/toolflow-core/examples/policy_snapshot.py
uv run --package toolflow-core --group dev python -m pytest packages/toolflow-core/tests -q
```

完整适配器示例见 [policy_snapshot.py](../../packages/toolflow-core/examples/policy_snapshot.py)。SQLite 事务与条件 UPDATE 实现源端 CAS；它是可运行示例，不属于 Core 内置的生产存储适配器。

如果本机 Anaconda Python 的 `readline` 扩展在 pytest 启动时崩溃，可以添加 `-p no:capture -p no:debugging`，跳过加载该扩展的插件。测试使用事件、Barrier、假时钟和真实 SQLite 文件验证并发顺序，不依赖定时 sleep。

公共泛型关系通过 `typing_usage.py` 检查：

```bash
uvx --with types-cachetools mypy --python-executable .venv/bin/python \
  --python-version 3.13 packages/toolflow-core/src/toolflow_core/common/policy \
  packages/toolflow-core/src/toolflow_core/gateway \
  packages/toolflow-core/tests/common/policy/typing_usage.py \
  packages/toolflow-core/examples/policy_snapshot.py
```

2026-10-18 验证记录：109 项测试通过（包含 asyncio 调试模式），mypy 检查的 24 个源文件无错误；SQLite 示例运行通过。详细并发测试见 [Repo / Manager 集成测试](../../packages/toolflow-core/tests/common/policy/test_repo_integration.py) 和 [SQLite 集成测试](../../packages/toolflow-core/tests/common/policy/test_sqlite_integration.py)。
