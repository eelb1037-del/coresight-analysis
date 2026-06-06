# CoreSight Analysis

Linux CoreSight 子系统深度分析，基于 **Linux v6.6**。

## 内容

- **[coresight_arch.txt](coresight_arch.txt)** — 完整 ASCII 架构图，涵盖：
  - 5 种设备类型与拓扑模型 (SOURCE / LINK / SINK / LINKSINK / HELPER)
  - 寄存器布局 (功能寄存器 vs 管理寄存器，LAR Lock 保护)
  - 安全模型 (TZPC → AUTHSTATUS → NSID，EL0/EL1 访问规则)
  - ETM Exception Level trace 过滤 (TRCVICTLR EXLEVEL bits)
  - Juno SoC 完整数据流 (6×ETM → funnel → ETF → replicator → TPIU/ETR)
  - TRBE 短路径 (ETE → TRBE 直写 DDR，无 Link)
  - 软件栈 (EL0 perf/sysfs → EL1 内核框架 → MMIO → 硬件)

- **[Android_LTD_演进.pptx](Android_LTD_演进.pptx)** — Android Live Threat Detection 演进 & 软件架构 PPT（深色科技风，10 页）
  - LTD + DSM 分层架构图
  - 演进时间线 (2024-2026)
  - 检测流程数据流
  - 2026 安全生态全景

- **[draw_arch.py](draw_arch.py)** — 架构图生成脚本

## CoreSight 子系统速览

```
SOURCE (ETM/STM/TPDM) → LINK (Funnel/Replicator) → SINK (ETF/ETR/TPIU)
                         ↑
                      HELPER (CATU/CTI)
```

### 寄存器模型

| 范围 | 类型 | 关键寄存器 |
|------|------|------|
| 0x000-0xEFF | 功能寄存器 (LAR Lock 保护) | TRCVICTLR, TMC_CTL, FUNNEL_FUNCTL |
| 0xF00+ | 管理寄存器 (架构通用) | LAR, LSR, **AUTHSTATUS**, DEVID, DEVTYPE |

### 安全模型

- **AUTHSTATUS** 由 SoC TZPC 信号决定，Linux probe 时检查 NSID != 0x3 则拒绝
- 所有 MMIO 操作必须在 **EL1**（内核）完成，EL0 通过 perf 间接控制
- ETM TRCVICTLR EXLEVEL bits 决定 trace 哪些 Exception Level
