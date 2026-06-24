# Linux perf 子系统分析总结（arm64 视角，基于 v6.6）

> 本文档整理自一次围绕 Linux perf 子系统的深入分析，重点在 arm64。
> 配套架构图：`arm64_perf_arch.svg` / `arm64_perf_arch.svg.png`

---

## 一、perf 子系统三层结构

perf 在内核里分两半、共三层：

| 层 | 位置 | 职责 |
|---|---|---|
| ① 通用 perf core（架构无关） | `kernel/events/` | 事件抽象、调度、ring buffer 传输 |
| ② ARM PMU 框架（ARM 共用） | `drivers/perf/arm_pmu.c` | 桥接 `struct pmu` ↔ `struct arm_pmu`，DT/ACPI 探测 |
| ③ PMUv3 驱动（arm64 实体） | `drivers/perf/arm_pmuv3.c` | 填充回调，实现 ARMv8/v9 标准 PMU |
| ④ 寄存器胶水层（arm64 专属） | `arch/arm64/include/asm/arm_pmuv3.h` | `read/write_sysreg` 封装 PMU 系统寄存器 |
| ⑤ 硬件 | — | PMUv3：N 个通用计数器 + 1 周期计数器 |

**设计哲学**：用 `struct pmu` 把硬件差异隔离在最底层，核心层只管「事件抽象 + 调度 + 数据传输」，一套代码支撑 x86/arm/risc-v + 软件事件 + tracepoint。

> 注意：arm64 把 PMU 框架和驱动集中在 `drivers/perf/`，不在 `arch/` 下（arm32 仍是旧布局 `arch/arm/kernel/perf_event_v7.c`）。

---

## 二、PMU 计数器数量

- 从 `PMCR_EL0.N` 字段读（5 bit，掩码 `0x1f`）→ **架构上限 31 个通用计数器**。
- 另加 1 个**专用周期计数器** `pmccntr_el0`（索引 0，只数 cycles）。
- `arm_pmuv3.c:1110` 处 `num_events = N; num_events += 1`（+1 即周期计数器）。
- **真实芯片**：绝大多数 Cortex-A/Neoverse `N = 6` → 6 通用 + 1 周期 = 7 个同时。Apple M1 走独立驱动，10 个计数器。

### 同时观察超过计数器数量的事件
| 需求 | 方法 |
|---|---|
| 省事，接受估算 | 全列上 → 内核**分时复用**，按使能比例外推（输出现 `(83.33%)` 占比）|
| 要精确绝对值 | 多轮跑，每轮 ≤6 个（负载需可复现）|
| 算比值（分子分母同窗口）| 事件分组 `{a,b}`，组内 ≤6 |
| 治本 | cycles 不占通用槽（白送）+ 砍派生事件 + 用 metric 组 |

---

## 三、周期计数器（cycle counter）

- `pmccntr_el0` 数的是 **CPU 时钟周期**，`perf` 的 `cycles` 事件读它。
- **不是时钟中断**：它只计数、自己不产生中断；时钟中断是 generic timer 的事。
- **专用、不占 6 个通用槽**——加 `cycles` 是免费的。
- 受 **DVFS 调频**影响，cycles ≠ 时间；通常配 instructions 算 **IPC**。
- `cycles`（CPU 真转时才涨，idle 停）≠ `task-clock`（基于时间）。

---

## 四、调用栈采集（callchain）

**作用**：每次采样抓当前函数调用栈，让 `perf record -g` / 火焰图能看到完整调用路径，把「哪里慢」变成「为什么慢、谁触发」。

两层：
- 通用层 `kernel/events/callchain.c`：预分配 per-CPU 缓冲、递归保护、引用计数。
- arm64 层 `arch/arm64/kernel/perf_callchain.c`：
  - `perf_callchain_kernel`：用 `arch_stack_walk` 爬内核栈。
  - `perf_callchain_user`：沿**帧指针 x29** 逐帧爬用户栈，需 `access_ok` + 防缺页 + **剥 PAC 签名**（`ptrauth_strip_user_insn_pac`）+ 防伪造。
  - CONFIG_COMPAT 支持 AArch32。

**价值场景**：定位真凶（共享函数归因）、火焰图、按调用来源拆分开销、挂任意事件（cache-miss/page-fault/tracepoint）、跨用户/内核边界。

**坑**：fp 模式依赖帧指针，`-fomit-frame-pointer` 会断链 → 用 `--call-graph dwarf`（更重、需 debuginfo）。

---

## 五、skid（采样偏移）⭐

**定义**：PMU 事件真正发生的指令，与采样最终记录的 PC 之间的偏差。

**成因**：计数器溢出 → 拉中断 → CPU 取中断 → 读 PC，中间有延迟；加上乱序执行 + 深流水线，这期间又退休了一堆指令，导致记录的 PC 飘到真凶**下游**，甚至**跨过函数返回**，把开销算到别的函数头上。

**特点**：方向单一（往后偏）、大小不固定、统计上可平均（大格局对，单采样细节不可信）。

**根治 = 精确采样**：
- arm64 → **SPE**（`drivers/perf/arm_spe_pmu.c`，`perf record -e arm_spe//` / `perf mem`）
- x86 → PEBS（Intel）/ IBS（AMD）
- 事件后缀 `:p`/`:pp` 请求精度，arm64 上靠 SPE 满足。

---

## 六、代码归因 vs 数据归因

| 想知道 | 用什么 | arm64 要求 |
|---|---|---|
| 哪段**代码/调用路径**触发多 | `perf record -e <ev> -g` + `perf report` | 普通计数器即可 |
| 哪一**行代码/指令** | `perf annotate` | 同上 |
| 哪个**数据地址/变量** | `perf mem` | **需 SPE** |
| 哪个 **cache line 被多核争抢**（伪共享）| `perf c2c` | **需 SPE** |

普通计数器只知道「miss 了一次」，不知道 miss 的是哪个地址——数据地址只有 SPE 在采样时才记录。

### BRBE
- = Branch Record Buffer Extension（分支记录，≈ Intel LBR）。
- **v6.6 不支持**：只有 `arch/arm64/tools/sysreg` 里的寄存器定义，无 perf 驱动（约 6.11+ 才合入）。
- 定位：分支误预测分析、无帧指针重建调用栈、AutoFDO；能从控制流间接缓解「函数已返回」的归因偏差，**但不能精确归因任意事件** —— 那是 SPE 的活。

---

## 七、CoreSight（硬件追踪）

| 机制 | 谁产生 | 内容 |
|---|---|---|
| PMU | 硬件 | 事件计数（统计）|
| ETM/ETE | 硬件 | CPU 指令流（自动全程追踪）|
| STM | **软件主动** | 软件打的标记（硬件加时间戳汇入 trace）|

### STM（System Trace Macrocell）
- **硬件版 printf 通道**：软件往特殊内存地址写，硬件自动加时间戳、和 ETM 合到同一 sink、同一全局时钟。
- 核心机制 **Stimulus Port / Channel**：大量独立通道，不同线程/CPU 各用各的 → 无锁、并发安全（`coresight-stm.c` 的 `channel_space`）。
- 两层：`coresight-stm.c`（硬件 IP 驱动）+ `drivers/hwtracing/stm/`（通用 STM class，暴露 `/dev/stmN`，自带 console/ftrace/heartbeat 软件源）。
- 价值：超低扰动软件埋点、把 ftrace/console 导进硬件 trace、软件事件 ↔ 硬件执行时序对齐。

### Link（funnel / replicator）
- 职责：纯**路由开闸**，把 inport → outport 接通，不碰内容。接口只有 `enable/disable(inport, outport)`。
- **funnel（合流）**：使能输入端口位 + hold time + **priority（仲裁优先级）**。
- **replicator（分流）**：**ID filter** 寄存器选输出端口 / 按 trace ID 过滤。
- 必须处理 **端口级引用计数**（多 path 共享时只首开/末关）。
- 拓扑连接由 DT/ACPI 描述（SoC 厂商配），core 自动选路；使用者通常无需手配。

### TRBE（Trace Buffer Extension）
- **per-CPU sink**，用**系统寄存器**访问（`TRBLIMITR_EL1`/`TRBPTR_EL1`/`TRBBASER_EL1`），不在 AMBA 总线上。
- ETE → TRBE **直接配对**，trace 就地写本核内存。
- **没有 link**：每核自带 sink，不跨核、不共享总线，无需 funnel/replicator/引用计数。
- 卖点：去掉片上 trace 总线拓扑，可线性扩展、配置简单；代价是缓冲受每核内存预算、ARMv9 才有。

---

## 八、性能开销

**核心结论**：纯计数几乎免费；采样开销 ≈ 采样频率 × 每次成本。

| 用法 | 典型开销 |
|---|---|
| `perf stat`（计数）| < 1~2%（生产可常驻）|
| `perf record -F 1000` | 1~3% |
| `-F 1000 -g`（fp 调用栈）| 3~8% |
| `--call-graph dwarf` | 10~30%+（慎用）|
| `-F 8000+` | 10~20%+ |
| **10ms 周期（100 Hz）** | **可忽略**：无栈 ~0.01%、fp ~0.05%、dwarf ~0.1~0.3% |

影响因素：采样频率（一阶）、调用栈模式（dwarf≫fp≫无）、事件数、system-wide vs per-task、ring buffer 大小。**务必用对照法实测。**

---

## 九、长期监控建议

- **用计数模式（`perf stat -I`）而非采样**——采样数据会爆炸（GB~TB）。
- **间隔权衡**（类奈奎斯特，间隔 ≤ 特征时长一半）：
  - 分钟级趋势 → 1s 甚至 10s；
  - 怕漏百毫秒尖峰 → 常驻 100ms；
  - 偶发要看清形状 → **「1s 基线 + 阈值触发 10ms 短抓」分层方案**（最佳）。
- 事件数 ≤ 6，避免分时复用使数据变估算值。
- 推荐：`perf stat -a -I 1000 -e cycles,instructions,cache-misses,cache-references,branch-misses,branches`
- 7×24 共享：加 `--bpf-counters`（内核内聚合，互不抢计数器）；分容器：`--for-each-cgroup`。
- 数据要 logrotate / 定期清。

**1s 会不会抓不住特征**：计数模式 1s 丢的是「何时」不是「多少」（总量精确）；亚秒尖峰被摊平 → 怕漏就降到 100ms 或用分层方案。

---

## 十、内核驱动如何使用 PMU

**不走 `perf_event_open` 系统调用，用 perf 导出的内核 API（`EXPORT_SYMBOL_GPL`）。**

| 函数 | 作用 |
|---|---|
| `perf_event_create_kernel_counter(attr, cpu, task, overflow_handler, context)` | 创建 event |
| `perf_event_read_local(ev, &val, &enabled, &running)` | 快速读（须在对的 CPU、关抢占）|
| `perf_event_read_value()` | 任意上下文读（慢，可能 IPI）|
| `perf_event_enable/disable()` | 启停 |
| `perf_event_release_kernel()` | 释放（必须配对）|

- **纯计数**：`overflow_handler = NULL`，自己定期 `read_local`；建议 `.pinned = 1` 独占防复用。
- **采样**：设 `attr.sample_period` + 提供 overflow handler，溢出自动回调。
- 限制：内核 event 不支持 grouping/AUX；per-CPU 要为每核各建一个；GPL-only；**不能绕过 perf core 直接调 PMU 驱动**。
- 参考实现：`kernel/watchdog_perf.c`（hardlockup 检测）、`kernel/events/hw_breakpoint.c`。

---

## 十一、perf core 关键结构与代码地图（kernel/events/core.c，13810 行）

### 核心数据结构（`include/linux/perf_event.h`）
```
perf_event (:671)            一个被监控的事件实例
  └ perf_event_context (:907)  event 容器（挂 task 或 CPU）
      └ perf_event_pmu_context (:869)  v6.6 新增：按 pmu 分组
          └ pmu (:302)        硬件抽象，驱动实现
```

### struct pmu 关键回调
`event_init`(:350) / `add`·`del`(:385) / `start`·`stop`(:406) / `read`(:415) / `start_txn`·`commit_txn`(:427 事务式上 group)

### 五大功能支柱
1. **生命周期**：`perf_event_open`(:12351) → `perf_event_alloc` → `perf_install_in_context`（IPI 到目标核）
2. **调度**：`__perf_event_task_sched_out/in`(:3625/:3979 上下文切换整组切) + `merge_sched_in`(:3789 group 整组上/不上) + `perf_rotate_context`（分时复用轮转）
3. **采样输出**：`perf_event_overflow`(:9575) → `__perf_event_overflow`(:9500 限流) → `perf_prepare_sample`(:7596) → `perf_output_sample`(:7262) → ring buffer
4. **继承**：fork 时克隆父 event 到子进程（per-task 监控覆盖进程树）
5. **ring buffer**：`ring_buffer.c`，无锁单生产者/单消费者环形缓冲

---

## 附：常用命令速查

```bash
# 计数
perf stat -e cycles,instructions ./app
perf stat -a -I 1000 -e cycles,instructions,cache-misses ...   # 长期监控

# 采样找热点
perf record -F 1000 -g ./app && perf report
perf annotate <func>

# 精确归因 / 数据地址（arm64 SPE）
perf record -e arm_spe// ./app
perf mem record ./app && perf mem report
perf c2c record ./app && perf c2c report   # 伪共享

# 事件分组（保证比值同窗口）
perf stat -e '{cache-misses,cache-references}' ./app

# 查本机 PMU
perf list
dmesg | grep -i pmu
```
