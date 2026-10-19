# 额外发现的潜在阻塞点

## 概述

在修复主要的卡死问题（`process.py` 中的 3 个阻塞点）后，又发现了 `containers.py` 中 **2 个额外的 `exec_run()` 调用**，它们也没有超时保护。

---

## 阻塞点 4: `remove_path()` - rm -rf 命令

**位置**: `runtime/src/sharp/dispatcher/runtime/containers.py:287`

```python
def remove_path(self, container_name: str, path: str, *, require_prefix: str) -> None:
    # ...
    container.exec_run(["rm", "-rf", "--", normalized], user="0")
    # ⚠️ 没有超时保护
```

**调用路径**:
```
cleanup_graph_snapshots() 
  → remove_path()
    → exec_run(["rm", "-rf", ...])
```

**在哪里调用**:
- `tasks/explore.py:276` - explore 任务的 finally 块中
- `tasks/reason.py:340` - reason 任务的 finally 块中

**风险评估**: **中等**
- ✅ 在 `finally` 块中调用，是清理逻辑
- ✅ 已经有 `try-except DockerException` 捕获
- ⚠️ 但如果 `exec_run()` 本身阻塞（不抛异常），`finally` 块永远不会完成
- ⚠️ 任务无法正常结束，可能影响调度循环

---

## 阻塞点 5: `path_exists_in_container()` - test -f 命令

**位置**: `runtime/src/sharp/dispatcher/runtime/containers.py:308`

```python
def path_exists_in_container(self, container_name: str, path: str) -> bool:
    # ...
    result = container.exec_run(["test", "-f", path])
    # ⚠️ 没有超时保护
    return result.exit_code == 0
```

**调用路径**:
```
inject_apk_if_needed()
  → path_exists_in_container()
    → exec_run(["test", "-f", ...])
```

**在哪里调用**:
- `tasks/common.py:71` - APK 注入前检查文件是否存在（幂等性检查）

**风险评估**: **低**
- ✅ 只在 APK 注入场景调用（不是每个任务都需要）
- ✅ 已经有 `try-except DockerException` 捕获
- ⚠️ 但如果 `exec_run()` 阻塞，APK 注入会卡住
- ⚠️ 如果项目需要 APK，会导致任务无法启动

---

## 是否需要修复？

### 阻塞点 4 (`remove_path`) - **建议修复**

**原因**:
1. 在 `finally` 块中调用，如果阻塞会影响任务清理
2. 每个 explore/reason 任务结束时都会调用
3. 虽然是 "best-effort"，但阻塞会导致任务永远无法完成
4. 可能累积资源泄漏（快照目录无法清理）

**修复优先级**: 🟡 **中等**（不如主循环阻塞紧急，但值得修复）

### 阻塞点 5 (`path_exists_in_container`) - **可选修复**

**原因**:
1. 只在 APK 注入场景使用（相对少见）
2. 在任务启动阶段，如果阻塞会快速暴露（不会隐蔽很久）
3. 已经有异常处理

**修复优先级**: 🟢 **低**（nice to have，但不紧急）

---

## 建议的修复方案

### 修复阻塞点 4 (`remove_path`)

```python
import concurrent.futures

def remove_path(self, container_name: str, path: str, *, require_prefix: str) -> None:
    """Best-effort recursive removal of a path inside a container."""
    normalized = str(PurePosixPath(path))
    prefix = str(PurePosixPath(require_prefix))
    if prefix in ("", "/") or normalized in ("", "/"):
        return
    if normalized != prefix and not normalized.startswith(prefix + "/"):
        LOG.warning("refusing to remove container path outside %s path=%s", prefix, path)
        return
    container = self._get_container(container_name)
    if container is None:
        return
    
    def _do_remove():
        try:
            container.exec_run(["rm", "-rf", "--", normalized], user="0")
        except DockerException as exc:
            LOG.debug("failed to remove container path=%s container=%s error=%s", 
                     normalized, container_name, exc)
    
    # Best-effort with timeout
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_remove)
        try:
            future.result(timeout=2.0)  # 2 秒超时
        except concurrent.futures.TimeoutError:
            LOG.warning("timeout removing container path=%s after 2.0s", normalized)
```

### 修复阻塞点 5 (`path_exists_in_container`)

```python
import concurrent.futures

def path_exists_in_container(self, container_name: str, path: str) -> bool:
    """True if `path` is an existing file inside the container."""
    container = self._get_container(container_name)
    if container is None:
        return False
    
    def _do_check():
        try:
            result = container.exec_run(["test", "-f", path])
            return result.exit_code == 0
        except DockerException as exc:
            LOG.debug("path_exists check failed container=%s path=%s error=%s", 
                     container_name, path, exc)
            return False
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_do_check)
        try:
            return future.result(timeout=1.0)  # 1 秒超时
        except concurrent.futures.TimeoutError:
            LOG.warning("timeout checking path existence container=%s path=%s after 1.0s", 
                       container_name, path)
            return False  # 超时当作不存在
```

---

## 总结

### 已修复（process.py）
✅ 阻塞点 1: `_read_session_id()` - cat session file  
✅ 阻塞点 2: `_kill_session()` - pkill  
✅ 阻塞点 3: `_kill_session()` - rm session file  

### 新发现（containers.py）
🟡 阻塞点 4: `remove_path()` - rm -rf（建议修复）  
🟢 阻塞点 5: `path_exists_in_container()` - test -f（可选修复）  

---

## 建议

1. **先测试已有的修复** - 看看 process.py 的 3 个修复是否解决了主要的卡死问题
2. **如果仍然偶尔卡死** - 考虑修复阻塞点 4（`remove_path`）
3. **如果 APK 注入场景卡死** - 修复阻塞点 5（`path_exists_in_container`）

**当前评估**: 主要的卡死问题应该已经被 process.py 的修复解决了。containers.py 的这两个点是**额外的防御性修复**，不是最紧急的。
