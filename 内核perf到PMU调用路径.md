# 内核中通过 perf 访问 PMU 的调用路径

> 内核驱动调用 perf 内核 API 后，在 `kernel/events/core.c` 里如何一路走到 arm64 PMU 驱动。
> 代码基于 Linux v6.6，行号对应该版本。配套图：`kernel_perf_pmu_callpath.svg`。
> 配套文档：`内核中调用PMU.md`（API 用法）、`perf子系统分析总结.md`（整体架构）。

---

## 贯穿全局的机制：event_function_call（IPI 到目标 CPU）

理解所有路径的前提：**PMU 是 per-CPU 硬件，只能在它所在的核上读写**。
因此 core 里几乎每个操作都通过 `event_function_call()`（`core.c:263`，底层 `smp_call_function` / IPI）
**跳到目标 CPU 去执行**。这是 perf 内核路径的主旋律。

唯一例外是 `perf_event_read_local()`——它要求调用者**本来就在目标核上且关了中断**，所以免 IPI。

---

## 路径 ① 创建 + 安装：perf_event_create_kernel_counter

```
perf_event_create_kernel_counter()              core.c:12755
  ├─ perf_event_alloc()                          分配 perf_event 对象
  │    └─ pmu->event_init(event)        ──────►  armpmu_event_init()      arm_pmu.c:500
  │         └─ 校验 + map_event 查事件表 ──────►  armv8_pmuv3_map_event()
  ├─ find_get_context()                          找/建 task 或 per-CPU ctx
  ├─ find_get_pmu_context()                      v6.6 按 pmu 分组那层
  └─ perf_install_in_context()                   core.c:2791
       └─ cpu_function_call / task_function_call ─► IPI 到目标 CPU
            └─ __perf_install_in_context()       core.c:2723   (已在目标核上)
                 └─ event_sched_in()             core.c:2480
                      └─ event->pmu->add()  ───► armpmu_add()              arm_pmu.c
                           ├─ get_event_idx() ─► armv8pmu_get_event_idx()  分配计数器槽
                           └─ pmu->start()    ─► armv8pmu_enable_event()   写 pmevtyper/pmevcntr
```

要点：
- 创建在调用者进程上下文，**安装必须 IPI 到目标核**——要写那个核的 PMU 寄存器。
- `pmu->add` = 分配计数器（`event_sched_in` 里 `core.c:2516`），`pmu->start` = 真正开始数。

---

## 路径 ② 启动：perf_event_enable

```
perf_event_enable()                              core.c:2990
  └─ _perf_event_enable()                         core.c:2953
       └─ event_function_call(__perf_event_enable) ─► IPI 到目标 CPU
            └─ __perf_event_enable()             core.c:2902
                 └─ ctx_sched_in → event_sched_in
                      └─ pmu->add / pmu->start ─► armpmu_add → armv8pmu_enable_event
```

---

## 路径 ③ 读计数（两条，差别很大）

### ③a perf_event_read_local() —— 快路径（热路径用）

```
perf_event_read_local()                          core.c:4528
  ├─ local_irq_save()                            关中断，挡掉调度/复用/IPI
  ├─ 校验：必须在对的 CPU、对的 task、pinned 在本核
  ├─ if (event->oncpu == 本CPU)
  │      event->pmu->read(event) ──────────────► armv8pmu_read_counter()   直接 MRS 读
  └─ *value = local64_read(&event->count)
```

- **没有 IPI**：前提是调用者**已在 event 所在 CPU、且关了中断**（`core.c:4574`）。
- 这就是它快的原因，也是它限制多的原因。
- `running < enabled` 表示被复用过，原始值需按 `enabled/running` 外推（`.pinned=1` 时一般相等）。

### ③b perf_event_read_value() —— 通用路径（可跨核）

```
perf_event_read_value()                          core.c:5428
  └─ perf_event_read()                           core.c:4593
       └─ event_function_call / smp_call ───────► IPI 到目标 CPU
            └─ __perf_event_read()               core.c:4446   (在目标核上)
                 └─ pmu->read(event) ───────────► armv8pmu_read_counter()
```

- 任意上下文可调，代价是**可能 IPI**（慢）。

---

## 路径 ④ 溢出采样（回调，自底向上）

```
计数器溢出 → PPI 中断（硬件）
  └─ armpmu_dispatch_irq()                        arm_pmu.c:419
       └─ armv8pmu_handle_irq()                   arm_pmuv3.c:762
            ├─ 读 pmovsclr 找溢出计数器
            ├─ armpmu_event_update()              累加到 event->count
            ├─ armpmu_event_set_period()          重装采样周期
            └─ perf_event_overflow() ───────────► core.c:9575
                 └─ __perf_event_overflow()        core.c:9500
                      ├─ 限流判断（throttle）
                      └─ event->overflow_handler(event, data, regs)
                            = create 时传入的回调
                              （默认 perf_event_output → ring buffer）
```

你在 `create_kernel_counter` 传的 `overflow_handler`，最终在 `__perf_event_overflow`（`core.c:9500`）被调用。

---

## 总览

```
   驱动调用 API                  core.c (架构无关)              arm PMU 驱动
   ─────────────                ──────────────────             ─────────────
   create_kernel_counter ─┐
                          ├─ event_alloc → pmu->event_init ─► armpmu_event_init
                          └─ install_in_context ══IPI══► event_sched_in
                                                          → pmu->add  ─────► armpmu_add (分配 idx)
   perf_event_enable ════IPI════► __perf_event_enable     → pmu->start ───► armv8pmu_enable_event
                                                                              │ 写 pmevtyper/cntr
   read_local ─(同核,关中断,无IPI)──► pmu->read ─────────► armv8pmu_read_counter (MRS)
   read_value ════IPI════► __perf_event_read → pmu->read ─► armv8pmu_read_counter

   硬件溢出 ◄── armv8pmu_handle_irq ◄── PPI 中断
        └─► perf_event_overflow → __perf_event_overflow → 你的 overflow_handler
```

---

## 三个要记住的设计点

1. **per-CPU 强制 IPI**：create / install / enable / read_value 都靠 `event_function_call`
   把操作送到目标核执行——因为 PMU 寄存器只能本核访问。

2. **read_local 是唯一免 IPI 的读法**：代价是调用者必须自己保证已在目标核 + 关中断
   （`local_irq_save`），所以快但严格。热路径必用它。

3. **pmu->add / start / read 是 core 与驱动的唯一接口**：驱动永远不直接调 `armv8pmu_*`，
   全部经 core 转发——这正是 perf 能统一仲裁所有计数器使用者的根本。

---

## 关键函数行号速查（v6.6 core.c）

| 函数 | 行号 |
|---|---|
| `event_function_call` | 263 |
| `event_sched_in` | 2480 |
| `__perf_install_in_context` | 2723 |
| `perf_install_in_context` | 2791 |
| `__perf_event_enable` | 2902 |
| `perf_event_enable` | 2990 |
| `__perf_event_read` | 4446 |
| `perf_event_read_local` | 4528 |
| `perf_event_read` | 4593 |
| `perf_event_read_value` | 5428 |
| `__perf_event_overflow` | 9500 |
| `perf_event_overflow` | 9575 |
| `perf_event_create_kernel_counter` | 12755 |

arm 侧：`armpmu_event_init` arm_pmu.c:500 · `armpmu_dispatch_irq` arm_pmu.c:419 · `armv8pmu_handle_irq` arm_pmuv3.c:762
