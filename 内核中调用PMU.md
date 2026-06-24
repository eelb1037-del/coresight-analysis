# 在内核中调用 PMU（in-kernel perf event API）

> 适用：内核模块 / 驱动想读取硬件 PMU 计数或做溢出采样。
> 代码引用基于 Linux v6.6，arm64 视角，但 API 跨架构通用。
> 真实参考实现：`kernel/watchdog_perf.c`（hardlockup 检测）、`kernel/events/hw_breakpoint.c`。

---

## 1. 核心原则

- **不要走 `perf_event_open(2)` 系统调用** —— 那是给用户态的。
- **不要绕过 perf core 直接调 PMU 驱动**（`struct pmu` 的 `add/start/read` 不对外开放）。
  所有计数器使用者（perf 工具、各内核驱动）必须走同一套 perf core API，由它统一做
  **调度、分时复用、上下文管理、计数器仲裁**。
- 使用 perf 导出的内核 API，拿到 `struct perf_event *` 句柄来操作。
- 这些符号都是 `EXPORT_SYMBOL_GPL`，**仅 GPL 模块**可链接。

---

## 2. API 一览

| 函数 | 作用 | 头文件位置 |
|---|---|---|
| `perf_event_create_kernel_counter()` | 创建一个内核 event | `include/linux/perf_event.h:1103` |
| `perf_event_enable()` / `perf_event_disable()` | 启停计数 | `:1702` |
| `perf_event_read_local()` | **快速读**当前值（须在对的 CPU、关抢占）| `:1110` |
| `perf_event_read_value()` | 任意上下文读（慢，可能 IPI 到目标核）| `:1112` |
| `perf_event_release_kernel()` | 释放（必须与 create 配对）| `:1101` |

```c
#include <linux/perf_event.h>

struct perf_event *
perf_event_create_kernel_counter(struct perf_event_attr *attr,
                                 int cpu,                 // 绑定的 CPU；per-CPU 用指定核
                                 struct task_struct *task,// 监控的进程；NULL = per-CPU 全局
                                 perf_overflow_handler_t overflow_handler, // 采样回调；纯计数填 NULL
                                 void *context);          // 回调里可取的私有数据
```

> 限制（见 `core.c:12766`）：内核 event **不支持 grouping、不支持 AUX**，就是单个计数器。

---

## 3. 配置 `struct perf_event_attr`

```c
struct perf_event_attr attr = {
    .type    = PERF_TYPE_HARDWARE,            // 事件类别
    .config  = PERF_COUNT_HW_CPU_CYCLES,      // 具体事件
    .size    = sizeof(struct perf_event_attr),
    .pinned  = 1,    // 独占计数器，不被分时复用挤下去（监控类强烈建议）
    .disabled= 1,    // 创建时先不启动，后面手动 enable
};
```

常用 `type` / `config`：
- `PERF_TYPE_HARDWARE` + `PERF_COUNT_HW_{CPU_CYCLES, INSTRUCTIONS, CACHE_MISSES, BRANCH_MISSES, ...}`
- `PERF_TYPE_RAW` + `.config = <该 CPU 的原始事件号>`（数标准事件没覆盖的硬件事件，事件号查芯片 TRM）
- `PERF_TYPE_HW_CACHE`（cache 事件的细分编码）

关键 flag：
- `.pinned = 1` —— 独占，避免计数器被复用导致计数不连续。代价是占掉一个通用计数器，别人少一个。
- `.disabled = 1` —— 创建后处于停用，需 `perf_event_enable()` 才开始。
- `.exclude_kernel / .exclude_user / .exclude_hv` —— 只数用户态 / 内核态。

---

## 4. 用法 A：纯计数（最常见）

自己在需要的时机读计数器，不用回调。

```c
static struct perf_event *cyc_ev;

static int my_pmu_start(void)
{
    struct perf_event_attr attr = {
        .type    = PERF_TYPE_HARDWARE,
        .config  = PERF_COUNT_HW_CPU_CYCLES,
        .size    = sizeof(attr),
        .pinned  = 1,
        .disabled= 1,
    };

    /* per-CPU 计数：cpu = 当前核，task = NULL，无回调 */
    cyc_ev = perf_event_create_kernel_counter(&attr, smp_processor_id(),
                                              NULL, NULL, NULL);
    if (IS_ERR(cyc_ev))
        return PTR_ERR(cyc_ev);

    perf_event_enable(cyc_ev);
    return 0;
}

static u64 my_pmu_read(void)
{
    u64 val, enabled, running;

    /* 热路径读值：必须在该 event 绑定的 CPU 上、且关抢占 */
    if (perf_event_read_local(cyc_ev, &val, &enabled, &running))
        return 0;

    /*
     * running < enabled 表示该 event 曾被复用（未全程在数）。
     * 用 .pinned=1 时通常 running == enabled；否则需按比例换算：
     *   estimated = val * enabled / running;
     */
    return val;
}

static void my_pmu_stop(void)
{
    perf_event_disable(cyc_ev);
    perf_event_release_kernel(cyc_ev);   // 必须释放
}
```

### `read_local` vs `read_value` 怎么选

| | `perf_event_read_local()` | `perf_event_read_value()` |
|---|---|---|
| 速度 | 快，直接读本地计数器 | 慢，可能 IPI 到目标核 |
| 上下文要求 | 必须在**对的 CPU、关抢占/中断** | 任意上下文可调 |
| 用途 | 热路径频繁读 | 偶尔读、跨核读 |

**结论**：热路径（每次调度 / 每个请求）一律用 `perf_event_read_local`。

---

## 5. 用法 B：溢出采样（周期性自动回调）

设 `attr.sample_period`，计数器每溢出一次，perf core 自动调你的 handler。
这是 watchdog、性能埋点、自适应调频等的用法。

```c
static void my_overflow(struct perf_event *event,
                        struct perf_sample_data *data,
                        struct pt_regs *regs)
{
    /* 每个采样周期进来一次。regs->pc 是被打断处的 PC（注意有 skid） */
    /* 防限流：让本 event 永不被 throttle（watchdog 的做法） */
    event->hw.interrupts = 0;

    /* ... 你的处理 ... */
}

static int my_sample_start(int cpu)
{
    struct perf_event_attr attr = {
        .type         = PERF_TYPE_HARDWARE,
        .config       = PERF_COUNT_HW_CPU_CYCLES,
        .size         = sizeof(attr),
        .pinned       = 1,
        .disabled     = 1,
        .sample_period= 100000,   // 每 10 万 cycles 回调一次
    };
    struct perf_event *ev;

    ev = perf_event_create_kernel_counter(&attr, cpu, NULL,
                                          my_overflow, NULL /*context*/);
    if (IS_ERR(ev))
        return PTR_ERR(ev);

    perf_event_enable(ev);
    /* 保存 ev 以便后续 disable/release */
    return 0;
}
```

> **skid 提醒**：回调里 `regs->pc` 不是「真正让计数器+1 的那条指令」，而是中断那一刻的 PC，
> 因中断延迟 + 乱序流水线会向后偏移，甚至跨过函数返回。要精确归因用 arm64 SPE，不要靠普通计数器回调。

---

## 6. 真实范例：`kernel/watchdog_perf.c`

内核自带的最标准范例（hardlockup 检测），逐段对应上面：

```c
/* 1) attr：cycles 事件，pinned 独占，disabled 先停 —— watchdog_perf.c:85 */
static struct perf_event_attr wd_hw_attr = {
    .type     = PERF_TYPE_HARDWARE,
    .config   = PERF_COUNT_HW_CPU_CYCLES,
    .size     = sizeof(struct perf_event_attr),
    .pinned   = 1,
    .disabled = 1,
};

/* 2) 溢出回调 —— watchdog_perf.c:94 */
static void watchdog_overflow_callback(struct perf_event *event,
                                       struct perf_sample_data *data,
                                       struct pt_regs *regs)
{
    event->hw.interrupts = 0;                 // 永不限流
    if (!watchdog_check_timestamp())
        return;
    watchdog_hardlockup_check(smp_processor_id(), regs);
}

/* 3) 创建（每 CPU 一个）—— watchdog_perf.c:107 */
wd_attr->sample_period = hw_nmi_get_sample_period(watchdog_thresh);
evt = perf_event_create_kernel_counter(wd_attr, cpu, NULL,
                                       watchdog_overflow_callback, NULL);
this_cpu_write(watchdog_ev, evt);

/* 4) 启停 / 释放 */
perf_event_enable(this_cpu_read(watchdog_ev));     // :150
perf_event_disable(event);                          // :211
perf_event_release_kernel(this_cpu_read(watchdog_ev)); // :257
```

要点：watchdog 在 **per-CPU kthread** 里创建，保证 CPU-locality（`core.c` 的 install 要 IPI 到目标核）。

---

## 7. 必须注意的坑

1. **会真实占用一个硬件计数器**：你一旦创建 event 就和 perf 用户、其它驱动**抢那 ~6 个通用计数器**。
   `.pinned=1` 可独占防复用，但也让别人少一个可用。

2. **per-CPU event 要为每个核各建一个**：`cpu` 参数只绑一个核。想覆盖全系统：
   ```c
   for_each_possible_cpu(cpu) { /* 各建一个；建议在 per-CPU kthread 或配合 CPU hotplug */ }
   ```
   并接入 CPU hotplug 回调（`cpuhp_setup_state`），核上线建、下线释放。

3. **生命周期严格配对**：`create_kernel_counter` ↔ `release_kernel`。模块卸载前务必全部释放，否则泄漏计数器。

4. **读值的上下文**：`perf_event_read_local` 不能在错误的 CPU 或开抢占时调；跨核读用 `perf_event_read_value`。

5. **复用换算**：未用 `.pinned` 时，`running < enabled` 说明被复用，原始值需按 `enabled/running` 外推。

6. **GPL-only**：`EXPORT_SYMBOL_GPL`，非 GPL 模块无法链接。

7. **采样回调在中断上下文**：handler 里不能睡眠、不能做重活；要延后处理用 `irq_work` / workqueue。

8. **错误处理**：`create` 返回值用 `IS_ERR()` 判断，失败可能是不支持该事件、计数器耗尽、CPU 不在线等。

---

## 8. 速查清单

```
创建   perf_event_create_kernel_counter(&attr, cpu, task, handler, ctx)
启动   perf_event_enable(ev)
读值   perf_event_read_local(ev, &val, &enabled, &running)   // 热路径
       perf_event_read_value(ev, &enabled, &running)          // 跨核/任意上下文
停止   perf_event_disable(ev)
释放   perf_event_release_kernel(ev)                          // 必须配对
```

| 场景 | overflow_handler | 读法 |
|---|---|---|
| 纯计数（自己读）| NULL | `read_local` |
| 周期采样（自动回调）| 你的 handler | 在回调里处理 |

参考实现：`kernel/watchdog_perf.c`、`kernel/events/hw_breakpoint.c`。
更多背景见同仓库 `perf子系统分析总结.md`。
