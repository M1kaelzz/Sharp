# Sharp-v2 架构与逻辑审查报告

## 执行概要

通过系统性审查 Sharp-v2 代码库，发现了**多个潜在问题**，包括并发安全隐患、资源泄漏风险、错误处理不完整等。除了已修复的 `process.py` 卡死问题外，还发现以下关键问题需要关注。

---

## 🔴 高危问题

### 1. 容器锁竞争条件（Race Condition）

**位置**: `runtime/src/sharp/dispatcher/runtime/containers.py:94-100`

```python
def _ensure_running_lock(self, name: str) -> threading.Lock:
    with self._ensure_running_locks_guard:
        lock = self._ensure_running_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            self._ensure_running_locks[name] = lock
        return lock
```

**问题**: 
- 字典 `_ensure_running_locks` 在多线程环境下可能有竞争条件
- 虽然有 `_ensure_running_locks_guard` 保护创建，但获取后的使用不在锁保护内
- 理论上是安全的，但代码意图不够清晰

**影响**: 低（设计正确，但可读性差）

**建议**: 添加注释说明锁的生命周期

---

### 2. ThreadPoolExecutor 关闭时可能丢失任务

**位置**: `runtime/src/sharp/dispatcher/scheduler/loop.py:76-86`

```python
def close(self) -> None:
    if self.futures:
        LOG.info(
            "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
            len(self.futures),
            sorted({task.project_id for task in self.futures.values()}),
        )
    self.executor.shutdown(wait=True)  # ✅ 会等待
    self.cleanup_executor.shutdown(wait=True)  # ✅ 会等待
    self.container_manager.close()
    self.client.close()
```

**问题**:
- `executor.shutdown(wait=True)` **会等待**所有任务完成，这是**正确的**
- 但如果某个任务卡死（如之前的 `_read_session_id` 问题），整个关闭流程会阻塞

**影响**: 中等（已修复 process.py 后风险降低）

**建议**: 
- 添加关闭超时机制
- 在超时后强制取消所有任务

```python
def close(self, timeout: int = 30) -> None:
    if self.futures:
        LOG.info("dispatcher shutting down waiting_for_tasks=%s", len(self.futures))
        # 先尝试取消所有任务
        for task in self.futures.values():
            task.cancellation.cancel("dispatcher_shutdown")
    
    # 使用超时等待
    self.executor.shutdown(wait=True, cancel_futures=False)
    # 如果卡住，记录警告而不是永久阻塞
```

---

### 3. 容器清理可能泄漏资源

**位置**: `runtime/src/sharp/dispatcher/runtime/containers.py:131-157`

```python
def cleanup_completed(self, project_id: str) -> bool:
    name = self.container_name(project_id)
    state = self.inspect_state(name)
    if state is None:
        return True
    container = self._require_container(name)
    if self._config.completed_action == "remove":
        LOG.info("removing completed project container project=%s container=%s", project_id, name)
        try:
            container.remove(force=True)
        except NotFound:
            return True
        except DockerException as exc:
            LOG.warning("failed to remove container=%s error=%s", name, exc)
            return False  # ⚠️ 返回 False，但容器可能处于半清理状态
        return self.inspect_state(name) is None
```

**问题**:
- 如果 `container.remove()` 部分失败（容器停止但未删除），下次清理可能会跳过
- `_inactive_cleanup_done` 字典会记录失败的清理，但容器仍占用资源
- 没有重试机制

**影响**: 中等（长期运行会积累僵尸容器）

**建议**: 
- 添加清理重试计数
- 定期扫描并强制清理卡住的容器
- 记录清理失败的容器到持久化日志

```python
# 在 DispatcherLoop 中添加
self.cleanup_retry_counts: dict[str, int] = {}
MAX_CLEANUP_RETRIES = 3

def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
    for summary in summaries:
        if summary.status != "completed":
            continue
        container_name = self.container_manager.container_name(summary.id)
        
        # 检查重试次数
        retry_count = self.cleanup_retry_counts.get(container_name, 0)
        if retry_count >= MAX_CLEANUP_RETRIES:
            LOG.error("container cleanup failed after %d retries, giving up container=%s", 
                     retry_count, container_name)
            continue
        
        # ... 原有逻辑
        
        # 失败时增加计数
        if not success:
            self.cleanup_retry_counts[container_name] = retry_count + 1
```

---

## 🟡 中危问题

### 4. 心跳机制可能导致任务假活

**位置**: `runtime/src/sharp/dispatcher/scheduler/loop.py:833-917`

```python
def _reap_futures(self) -> None:
    done = [future for future in self.futures if future.done()]
    for future in done:
        task = self.futures.pop(future)
        try:
            outcome = future.result()
            # ... 处理结果
        except Exception:
            LOG.exception("task crashed project=%s task=%s worker=%s", 
                         task.project_id, task.task_type, task.worker_name)
```

**问题**:
- 如果任务线程卡死但没有超时，`future.done()` 永远返回 False
- 调度器会认为任务仍在运行，不会分配新任务
- 依赖任务自己的超时机制，但如果超时机制本身有 bug（如之前的 `_read_session_id`），整个系统停滞

**影响**: 中等

**建议**: 
- 添加任务存活时间监控
- 超过合理时间（如 2 小时）的任务强制标记为超时

```python
# 在 RunningTask 中添加
@dataclass
class RunningTask:
    # ... 现有字段
    started_at: float = field(default_factory=time.time)

# 在 _reap_futures 前添加检查
def _check_stuck_tasks(self) -> None:
    now = time.time()
    MAX_TASK_LIFETIME = 7200  # 2小时
    
    for future, task in list(self.futures.items()):
        if now - task.started_at > MAX_TASK_LIFETIME:
            LOG.error("task stuck for %ds, force cancelling project=%s task=%s",
                     int(now - task.started_at), task.project_id, task.task_type)
            task.cancellation.cancel("stuck_timeout")
            # 给 5 秒让它自己清理
            time.sleep(5)
            if not future.done():
                LOG.error("task still not done after cancel, removing from tracking")
                self.futures.pop(future, None)
```

---

### 5. 调度器状态不一致风险

**位置**: `runtime/src/sharp/dispatcher/scheduler/loop.py:988-1009`

```python
def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
    dispatchable_ids = {
        summary.id
        for summary in summaries
        if summary.status == "active" or self._summary_may_have_open_report_work(summary)
    }
    self.runtime_project_ids.intersection_update(dispatchable_ids)
    # ...
```

**问题**:
- `runtime_project_ids` 和 `futures` 可能不同步
- 如果 `_reap_futures` 在 `_refresh_runtime_projects` 之间执行顺序错误，可能导致：
  - 项目被认为还在运行，但实际所有任务已完成
  - 或相反：项目被移除，但任务还在 `futures` 中

**影响**: 中等（可能导致调度延迟）

**建议**: 
- 在 `_reap_futures` 后立即清理 `runtime_project_ids`
- 添加一致性检查

```python
def _ensure_consistency(self) -> None:
    """确保 runtime_project_ids 与 futures 一致"""
    actual_running = {task.project_id for task in self.futures.values()}
    if self.runtime_project_ids != actual_running:
        LOG.warning("runtime_project_ids inconsistency detected, fixing: "
                   "expected=%s actual=%s", self.runtime_project_ids, actual_running)
        self.runtime_project_ids = actual_running
```

---

### 6. APK 注入缺少事务性

**位置**: `runtime/src/sharp/dispatcher/tasks/common.py:32-96`

```python
def ensure_apk_injected(
    container_manager: ContainerManager,
    container_name: str,
    project: "ProjectDetail",
) -> str | None:
    # ...
    if container_manager.path_exists_in_container(container_name, target_path):
        return target_path  # ⚠️ 假设文件完整
    
    try:
        copied = container_manager.write_binary_file(
            container_name, target_path, source_path, max_bytes=MAX_INJECT_APK_BYTES
        )
    except FileNotFoundError as exc:
        raise ApkInjectionError(...)
```

**问题**:
- `path_exists_in_container` 只检查文件存在，不验证完整性
- 如果上次注入失败（部分写入），这次会跳过重试
- 没有校验和验证

**影响**: 中等（可能导致 APK 损坏但任务继续）

**建议**: 
- 添加文件大小或哈希校验
- 或使用临时路径 + 原子重命名

```python
def ensure_apk_injected(...) -> str | None:
    # ...
    target_path = container_apk_path(filename)
    temp_path = f"{target_path}.tmp"
    
    # 检查是否已注入且完整
    if container_manager.path_exists_in_container(container_name, target_path):
        # TODO: 添加大小验证
        return target_path
    
    # 清理可能的半成品
    container_manager.remove_path(container_name, temp_path, require_prefix="/tmp")
    
    try:
        copied = container_manager.write_binary_file(
            container_name, temp_path, source_path, max_bytes=MAX_INJECT_APK_BYTES
        )
        # 原子重命名
        container = container_manager._require_container(container_name)
        container.exec_run(["mv", temp_path, target_path])
    except Exception:
        # 清理失败的临时文件
        container_manager.remove_path(container_name, temp_path, require_prefix="/tmp")
        raise
```

---

## 🟢 低危问题 / 改进建议

### 7. 日志状态字典无限增长

**位置**: `runtime/src/sharp/dispatcher/scheduler/loop.py:64`

```python
self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
```

**问题**:
- `_log_state` 字典用于去重日志，但从不清理
- 长期运行会无限增长（虽然增长很慢）

**影响**: 低（内存泄漏，但量很小）

**建议**: 
- 定期清理超过 1 小时未更新的条目
- 或限制字典大小（如最多 10000 条）

```python
def _cleanup_old_log_state(self) -> None:
    """定期调用，清理旧的日志状态"""
    if len(self._log_state) < 10000:
        return
    # 简单策略：清空重建（日志会重新记录，无大碍）
    self._log_state.clear()
    LOG.debug("log_state dict cleared due to size limit")
```

---

### 8. Worker 健康状态无持久化

**位置**: `runtime/src/sharp/dispatcher/scheduler/loop.py:62-63`

```python
self.worker_unhealthy_until: dict[str, float] = {}
self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
```

**问题**:
- Worker 健康状态在调度器重启后丢失
- 不健康的 worker 会立即被再次调度

**影响**: 低（只影响启动后短暂时间）

**建议**: 
- 将健康状态持久化到文件（可选）
- 或在启动时运行快速健康检查

---

### 9. 容器名称可能冲突

**位置**: `runtime/src/sharp/dispatcher/runtime/containers.py:37-39`

```python
def container_name(self, project_id: str) -> str:
    sanitized = project_id.replace("/", "-")
    return f"{self._PREFIX}{sanitized}"
```

**问题**:
- 只替换 `/`，其他特殊字符可能导致 Docker 错误
- `project_id` 如果包含 `..`、空格等可能有安全隐患

**影响**: 低

**建议**: 
- 使用更严格的清理函数

```python
import re

def container_name(self, project_id: str) -> str:
    # 只保留字母、数字、-、_
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', project_id)
    # 避免多个连续的 -
    sanitized = re.sub(r'-+', '-', sanitized)
    # 移除首尾的 -
    sanitized = sanitized.strip('-')
    return f"{self._PREFIX}{sanitized}"
```

---

### 10. Stall 检测可能误报

**位置**: `runtime/src/sharp/dispatcher/scheduler/loop.py:890-901`

```python
if old_checkpoint is not None and task.fact_count <= old_checkpoint.fact_count:
    stall = self.reason_stall_counts.get(task.project_id, 0) + 1
    self.reason_stall_counts[task.project_id] = stall
    if stall >= REASON_STALL_THRESHOLD:
        self._on_reason_stalled(task.project_id, stall, task.hint_count)
```

**问题**:
- 只检查 `fact_count`，不考虑 `hint_count` 和 `open_intent_count` 的变化
- 如果用户添加 hint 但没有产生新 fact，仍会被标记为 stalled

**影响**: 低（用户体验问题）

**建议**: 
- 将 hint 的增加也视为进展

```python
if old_checkpoint is not None:
    # 检查是否有任何进展
    has_progress = (
        task.fact_count > old_checkpoint.fact_count or
        task.hint_count > old_checkpoint.hint_count
    )
    if not has_progress:
        stall = self.reason_stall_counts.get(task.project_id, 0) + 1
        self.reason_stall_counts[task.project_id] = stall
        if stall >= REASON_STALL_THRESHOLD:
            self._on_reason_stalled(task.project_id, stall, task.hint_count)
    else:
        # 有进展，重置计数
        if task.project_id in self.reason_stall_counts:
            self.reason_stall_counts.pop(task.project_id, None)
            self.reason_stalled_hint_counts.pop(task.project_id, None)
```

---

## 📊 架构评估

### 优点 ✅

1. **清晰的责任分离**
   - ContainerManager：容器生命周期
   - DispatcherLoop：任务调度
   - ManagedProcess：进程管理
   
2. **良好的并发控制**
   - ThreadPoolExecutor 管理任务
   - 锁保护关键资源
   
3. **健壮的错误处理**
   - 大部分异常有捕获和记录
   - 失败重试机制（worker unhealthy/rejected）

4. **日志完善**
   - 关键操作都有日志
   - 去重机制避免日志洪水

### 缺点 ❌

1. **缺少全局超时保护**
   - 依赖各层超时，但某层失效会波及全局
   
2. **状态一致性保证不足**
   - 多个状态字典可能不同步
   
3. **资源清理不彻底**
   - 容器清理可能失败后无重试
   - 日志状态字典无限增长

4. **缺少监控指标**
   - 无 Prometheus/Metrics 暴露
   - 难以外部监控系统健康度

---

## 🎯 优先级修复建议

### P0 - 立即修复
1. ✅ **已修复**: `process.py` 的 `_read_session_id` 卡死问题

### P1 - 本周修复
2. **添加任务存活时间监控**（问题 4）
3. **改进容器清理重试机制**（问题 3）

### P2 - 下个迭代
4. **添加调度器关闭超时**（问题 2）
5. **APK 注入完整性校验**（问题 6）
6. **调度器状态一致性检查**（问题 5）

### P3 - 长期优化
7. **日志状态字典清理**（问题 7）
8. **容器名称清理函数加强**（问题 9）
9. **Stall 检测逻辑优化**（问题 10）

---

## 📝 测试建议

### 单元测试覆盖
1. `ContainerManager` 的锁机制
2. `TaskCancellation` 的并发安全性
3. APK 注入的幂等性和错误恢复

### 集成测试场景
1. **容器清理失败恢复**
   ```bash
   # 模拟 Docker 异常
   docker pause sharp-dispatch-test-project
   # 验证清理重试
   ```

2. **任务卡死检测**
   ```python
   # Mock 一个永不返回的任务
   # 验证 2 小时后被强制取消
   ```

3. **调度器优雅关闭**
   ```bash
   # 启动调度器，运行多个任务
   # Ctrl+C 后验证所有任务正确关闭
   ```

### 压力测试
1. **长时间运行测试**
   - 连续运行 7 天
   - 监控内存/容器数量
   - 验证无泄漏

2. **高并发测试**
   - 同时 50 个项目
   - 验证锁竞争和死锁

---

## 🔍 监控建议

添加以下指标：

```python
# 在 DispatcherLoop 中添加
from prometheus_client import Counter, Gauge, Histogram

task_duration = Histogram('sharp_task_duration_seconds', 'Task execution time', ['task_type'])
task_failures = Counter('sharp_task_failures_total', 'Task failures', ['task_type', 'reason'])
running_tasks = Gauge('sharp_running_tasks', 'Currently running tasks')
container_count = Gauge('sharp_container_count', 'Active containers')
cleanup_failures = Counter('sharp_cleanup_failures_total', 'Container cleanup failures')
```

---

## 总结

Sharp-v2 整体架构**合理且健壮**，但存在一些**边界情况**和**资源管理**问题。最严重的卡死问题已修复，其他问题影响较小但需要逐步改进。

**关键风险**：
- 长时间运行可能积累资源泄漏
- 异常情况下的恢复机制不够完善
- 缺少外部监控能力

**优先行动**：
1. ✅ `process.py` 已修复
2. 添加任务存活监控
3. 改进容器清理机制
4. 添加 Prometheus 指标
