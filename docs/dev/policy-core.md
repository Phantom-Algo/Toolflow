# Policy 内核使用指南

Policy 内核位于 `toolflow_core.common.policy`，提供进程内的不可变配置快照与线程安全更新。文件、DB、远程仓库通过异步 Loader 提供配置数据，领域模型负责校验，Manager 负责发布。Loader 与重载入口为异步接口；模型、Builder 和 Manager 保持同步。

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

不要将 `list`、`dict`、`set`、`Any` 或客户端对象声明为领域字段；不支持的声明在模型完成初始化时抛出 `PolicyImmutabilityError`。原始配置仍可使用普通 JSON 字典和列表，模型负责转换。

继承时保留 BasePolicy 的冻结、默认值校验和重新校验配置。校验器与查询方法保持纯函数，不向 Policy 添加私有状态或缓存。

## 初始化与读取

```python
from toolflow_core.common.policy import PolicyManager

initial = RuntimePolicy.from_config({
    "timeout_ms": 3000,
    "allowed_tools": ["search", "read"],
    "tool_timeouts": {"search": 1000},
})
manager = PolicyManager(initial)

# 一次 request / task / 过滤链取得一次快照。
snapshot = manager.snapshot()
policy = snapshot.policy
timeout = policy.tool_timeouts.get("search", policy.timeout_ms)
```

将同一个 Manager 注入消费者，消费者每次开始操作时读取快照。读取只在取得快照引用时短暂持锁；业务逻辑使用该快照时不持锁。旧快照保持有效，不会随热更新修改。

初始 `revision` 为 0，每次发布成功加 1；它只在当前 Manager 的生命周期内有意义，不是数据库版本，也不能用于跨进程排序。

## 修改配置与处理冲突

```python
from toolflow_core.common.policy import PolicyConflictError

base = manager.snapshot()
candidate = base.policy.with_changes(timeout_ms=5000)

try:
    published = manager.replace(candidate, expected=base)
except PolicyConflictError:
    # 期间有其他写者成功发布。由应用决定重新读取、合并修改或提示用户。
    raise
```

`expected` 必须是该 Manager 当前返回的原始快照对象，不能自行构造，也不能使用另一个 Manager 的同号快照。发布在锁内检查对象身份，再整体替换引用；冲突不会修改内存状态。

也可以使用纯函数形式：

```python
base = manager.snapshot()
published = manager.update(
    lambda policy: policy.with_changes(retry_limit=3),
    expected=base,
)
```

`update()` 在快照已失效时直接报冲突；否则在锁外调用一次 transform，再检查并发布。不会自动重试 transform，因此应用不必担心隐藏的重复调用。回调仍应保持纯函数，避免配置发布失败后留下外部副作用。

`with_changes()` 按顶层字段替换，不递归合并字典，并重新校验全部字段约束。可以显式构建嵌套修改：

```python
base = manager.snapshot()
timeouts = dict(base.policy.tool_timeouts)
timeouts["search"] = 2000
published = manager.replace(
    base.policy.with_changes(tool_timeouts=timeouts),
    expected=base,
)
```

Manager 在初始化和发布前会重新校验、隔离候选模型，不信任 `model_construct()` 或 `model_copy(update=...)` 产生的对象。推荐统一使用 `from_config()`、`with_changes()`。直接调用 Pydantic 构造函数也会冻结容器，但其字段错误保留 Pydantic 的 `ValidationError`。

## 接入文件、DB 或远程配置

实现 `async def load() -> Mapping[str, object]` 即可，无须继承特定基类。DB / 远程配置使用客户端的原生异步 I/O；文件或同步客户端通过适配器中的 `asyncio.to_thread()` 接入，避免仅将阻塞函数声明成 `async`。

```python
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

from toolflow_core.common.policy import reload_policy


class JsonPolicyLoader:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def load(self) -> Mapping[str, object]:
        return await asyncio.to_thread(self._read_file)

    def _read_file(self) -> Mapping[str, object]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("policy JSON must be an object")
        return raw


async def main() -> None:
    loader = JsonPolicyLoader(Path("policy.json"))
    initial = RuntimePolicy.from_config(await loader.load())
    manager = PolicyManager(initial)
    published = await reload_policy(manager, loader, RuntimePolicy.from_config)
    print(published.revision)


# 独立脚本入口使用；已有事件循环的应用直接 await 相应协程。
asyncio.run(main())
```

`await reload_policy()` 开始执行时，在第一次等待 I/O 前取得基线快照，然后 `await loader.load()`、构建、条件发布。期间如果有其他线程或协程成功发布，则抛出 `PolicyConflictError`，不会用较早开始的加载结果覆盖新内存状态。只创建协程对象而不等待或调度它，不会执行加载。

一次新的显式 reload 表示使用配置源替换内存，因此可以覆盖尚未保存的本地修改。不要在未确定权威源与冲突规则时直接叠加自动轮询。源端旧副本的识别、源版本顺序、多源合并和完整读取由应用或适配器负责。

加载阶段异常包装为 `PolicyLoadError` 并保留异常链；配置校验失败使用 `PolicyValidationError`；循环和不支持的数据结构使用 `PolicyImmutabilityError`。这些错误均不会替换现有快照。

### 取消、超时与事件循环边界

在等待 Loader 时收到 `asyncio.CancelledError`，取消直接向上传播，不包装成 `PolicyLoadError`，也不执行 Builder 或发布新快照。Loader 清理资源后应继续传播取消，不要吞掉它。[Python 取消语义](https://docs.python.org/3.13/library/asyncio-task.html#task-cancellation)

调用方可以在自己的异步函数中设置加载超时：

```python
async with asyncio.timeout(5):
    published = await reload_policy(manager, loader, RuntimePolicy.from_config)
```

Loader 返回之后，Builder、重新校验和条件发布在当前线程同步执行，没有新的 `await` 点。它们应短小且不包含 I/O；大配置或耗时校验仍可能阻塞事件循环，取消与超时不能中途打断这段同步代码，也不会撤销已经成功的发布。Manager 保留 `threading.Lock`，继续协调协程与其他线程的更新。

如果 Loader 使用 `asyncio.to_thread()`，取消等待不代表底层读取线程立即停止；该线程稍后返回也不会由这次已取消的重载继续发布。内核不会将构建或发布本身放到后台线程。[Python 线程调用接口](https://docs.python.org/3.13/library/asyncio-task.html#running-in-threads)

### 从同步版本迁移

- 将 Loader 的 `def load()` 改为 `async def load()`，内部使用原生异步 I/O 或在线程中执行原有同步读取函数。
- 初始加载改为 `RuntimePolicy.from_config(await loader.load())`。
- 重载改为 `await reload_policy(...)`；同步脚本在最外层使用一次 `asyncio.run(main())`，不要在已有事件循环里嵌套 `asyncio.run()`。
- `PolicyBuilder`、`snapshot()`、`replace()`、`update()` 无须加 `await`；不支持向新的重载入口直接传旧的同步 Loader。

## 导出与可选持久化

```python
# 保存更新返回的精确快照。
data = published.policy.to_config()
payload = json.dumps(data, ensure_ascii=False)
# 应用自行保存 payload；例如文件整体替换、DB 事务或远端 CAS。
```

`to_config()` 使用领域字段名称，返回独立的普通字典 / 列表；修改它不会影响已发布配置。也支持 Pydantic 的 `model_dump()` / `model_dump_json()`，其中 `by_alias` 等参数遵循 Pydantic 的序列化规则。

内存发布成功只代表当前进程已生效。保存失败时保留内存配置，由应用记录“已生效、未保存”并决定重试。不要自动回滚，也不要并行保存不同版本而允许旧保存覆盖新保存。需要可靠持久化、源端 CAS、自动监听或多进程同步时，另行加入协调层。

## 示例与测试

在仓库根目录运行完整示例：

```bash
uv run --package toolflow-core python packages/toolflow-core/examples/policy_snapshot.py
```

运行测试：

```bash
uv run --package toolflow-core --group dev python -m pytest packages/toolflow-core/tests/common/policy -q
```

如果本机 Anaconda Python 的 `readline` 扩展在 pytest 启动时崩溃，可以在命令中添加 `-p no:capture -p no:debugging`，跳过会加载该扩展的插件。无需修改全局 pytest 设置。

异步测试使用标准库 `asyncio.run()` 与事件控制时序，不额外依赖 pytest-asyncio；覆盖异步加载让出事件循环、并发重载冲突、取消、超时以及同步文件适配器。

公共接口的泛型关系在 `typing_usage.py` 中使用 `assert_type` 验证，可以临时运行 mypy，无须增加运行时依赖：

```bash
uvx mypy --python-executable .venv/bin/python --python-version 3.13 \
  packages/toolflow-core/src/toolflow_core/common/policy \
  packages/toolflow-core/tests/common/policy/typing_usage.py \
  packages/toolflow-core/examples/policy_snapshot.py
```
