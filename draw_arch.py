#!/usr/bin/env python3
"""Generate CoreSight architecture diagram as ASCII art and write to file."""

diagram = r"""
╔══════════════════════════════════════════════════════════════════════════════════════════╗
║                          Linux CoreSight 子系统架构总览 v6.6                              ║
╚══════════════════════════════════════════════════════════════════════════════════════════╝


  1. 设备类型与拓扑模型
  ───────────────────────

                              ┌─────────────────────┐
                              │      SOURCE          │  ★ 产生 trace 数据
                              │  ETM / STM / TPDM    │
                              │  (per-CPU or bus)    │
                              └──────────┬───────────┘
                                         │  out_conns[0]
                                         │
                           ┌─────────────┼─────────────┐
                           │             │             │
                    ┌──────▼──────┐ ┌───▼──────┐ ┌───▼──────────┐
                    │    LINK     │ │  LINK    │ │   LINK       │  ★ 路由数据
                    │   Funnel    │ │Funnel    │ │  Replicator  │
                    │ (多进1出)   │ │(多进1出) │ │  (1进多出)    │
                    └──────┬──────┘ └───┬──────┘ └───┬─────┬────┘
                           │            │            │     │
                    ┌──────▼──────┐     │     ┌──────▼──┐ ┌▼──────────┐
                    │  LINKSINK   │     │     │  SINK   │ │   SINK    │
                    │    ETF      │     │     │  TPIU   │ │   ETR     │  ★ 存储数据
                    │(buffer+FIFO)│     │     │(外部口) │ │ (写 DDR)  │
                    └──────┬──────┘     │     └─────────┘ └───────────┘
                           │            │
                    ┌──────▼────────────▼──────┐
                    │         SINK             │
                    │    ETF (circ buffer)     │  ★ 小容量片上 buffer
                    └─────────────────────────┘

                          ┌──────────────┐
                          │    HELPER     │  ★ 辅助设备
                          │ CATU (地址翻译) │
                          │ CTI (交叉触发)  │
                          └──────────────┘


  2. 寄存器布局 (ARM CoreSight 架构规范)
  ─────────────────────────────────────

  ┌──────────────────────────────────────────────────────────────────────────┐
  │  0x000                           CoreSight Component                     │
  │  ┌──────────────────────────────────────────────────────────────────┐    │
  │  │                    功能寄存器 (Component-Specific)                │    │
  │  │                                                                    │    │
  │  │  ETM:  TRCPRGCTLR  TRCVICTLR  TRCACVRn  TRCCIDCVRn ...          │    │
  │  │  TMC:  TMC_CTL  TMC_FFCR  TMC_DBALO/DBAHI  TMC_RRP/RWP         │    │
  │  │  CTI:  CTICONTROL  CTIINEN  CTIOUTEN  CTIGATE                   │    │
  │  │  Funnel: FUNNEL_FUNCTL   Replicator: REPLICATOR_IDFILTER0/1     │    │
  │  │                                                                    │    │
  │  │  ⚠ 写操作受 LAR Lock 保护，必须先 CS_UNLOCK(0xC5ACCE55)          │    │
  │  └──────────────────────────────────────────────────────────────────┘    │
  │                                                                            │
  │  0xF00 ── ITCTRL         集成模式控制                                      │
  │  0xFxx ── (reserved)                                                       │
  │  0xFA0 ── CLAIMSET       设置 Claim 标签 ─┐                               │
  │  0xFA4 ── CLAIMCLR       清除 Claim 标签  ├─ Debug 多主机互斥             │
  │  0xFB0 ── LAR            Lock Access      ── 写 0xC5ACCE55 解锁           │
  │  0xFB4 ── LSR            Lock Status      ── 读锁状态                     │
  │  0xFB8 ── ★ AUTHSTATUS   认证状态          ── NS 能不能访问这个组件       │
  │  0xFBC ── DEVARCH        设备架构版本                                     │
  │  0xFC8 ── DEVID          设备 ID                                          │
  │  0xFCC ── DEVTYPE        设备类型                                         │
  │  0xFD0 ── PIDR0-7        外设 ID 寄存器                                   │
  │  0xFF0 ── CIDR0-3        组件 ID 寄存器 (标识 CoreSight)                  │
  └──────────────────────────────────────────────────────────────────────────┘


  3. 安全模型 — AUTHSTATUS 与 Exception Level
  ─────────────────────────────────────────

  ┌─────────────────────────────────────────────────────────────────────────┐
  │                         SoC Hardware (TZPC)                              │
  │  ┌─────────────┐                                                         │
  │  │ Secure World │                                                         │
  │  │  EL3 (ATF)   │── TZPC ──→ sets debug/auth permissions               │
  │  │  EL1 (TEE)   │                                                         │
  │  └──────┬───────┘                                                         │
  │         │ TrustZone boundary                                              │
  │  ┌──────▼───────┐                                                         │
  │  │Non-Secure    │                                                         │
  │  │  EL2 (Hyper.)│                                                         │
  │  │  EL1 (Linux) │──→ MMIO read AUTHSTATUS @ probe time                   │
  │  │  EL0 (App)   │    if (NSID != 0x3) → -EACCES, 拒绝驱动加载            │
  │  └──────────────┘                                                         │
  └─────────────────────────────────────────────────────────────────────────┘

  AUTHSTATUS bit 含义:
  ┌────────────┬──────┬─────────────────────────────────────┐
  │ NSID[1:0]  │ 0x3  │ Non-Secure 可读写 (Linux 需要这个)   │
  │ NSID[1:0]  │ 0x1  │ Non-Secure 只读                      │
  │ NSID[1:0]  │ 0x0  │ Non-Secure 完全不能访问              │
  │ NSEID[1:0] │ 0x3  │ Non-Secure 特权 (EL1) 可访问         │
  │ NSEID[1:0] │ 0x1  │ Non-Secure 特权只读                  │
  └────────────┴──────┴─────────────────────────────────────┘

  ETM Exception Level Trace 过滤 (TRCVICTLR 寄存器):
  ┌─────────────────┬─────────────┬───────────────────────────┐
  │ EXLEVEL_NS_APP  │ BIT(4)=bit20│ trace NonSecure EL0 (用户态)│
  │ EXLEVEL_NS_OS   │ BIT(5)=bit21│ trace NonSecure EL1 (内核态)│
  │ EXLEVEL_NS_HYP  │ BIT(6)=bit22│ trace NonSecure EL2 (虚机)  │
  │ EXLEVEL_S_APP   │ BIT(0)=bit16│ trace Secure EL0 (TEE 用户) │
  │ EXLEVEL_S_OS    │ BIT(1)=bit17│ trace Secure EL1 (TEE 内核) │
  └─────────────────┴─────────────┴───────────────────────────┘


  4. 完整数据流 (以 Juno SoC 为例)
  ──────────────────────────────

  Cluster0 (2×A72)                      Cluster1 (4×A53)
  ┌──────┐ ┌──────┐          ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
  │ETM0  │ │ETM1  │          │ETM2  │ │ETM3  │ │ETM4  │ │ETM5  │   SOURCE
  │CPU0  │ │CPU1  │          │CPU2  │ │CPU3  │ │CPU4  │ │CPU5  │   (per-CPU)
  └──┬───┘ └──┬───┘          └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘
     │p0     │p1                │p0     │p1     │p2     │p3
     └───┬───┘                  └───────┴───┬───┴───────┘
  ┌──────▼──────┐              ┌────────────▼────────────┐
  │cluster0     │              │       cluster1          │     LINK
  │funnel       │              │       funnel            │     (MERG)
  │(2→1)        │              │       (4→1)             │
  └──────┬──────┘              └────────────┬────────────┘
         │p0                               │p1
  ┌──────▼─────────────────────────────────▼─────────────┐
  │                   main_funnel (2→1)                  │     LINK
  │  (STM 软件 trace 也在此注入)                         │
  └────────────────────────┬────────────────────────────┘
                           │
                    ┌──────▼──────┐
                    │    etf0     │                              LINKSINK
                    │  (4KB buf)  │                              (buffer)
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
     ┌────────▼──────┐    │    ┌───────▼──────┐
     │ csys1_funnel  │    │    │ csys2_funnel  │                  LINK
     └───────┬───────┘    │    └───────┬───────┘
             │            │            │
     ┌───────▼──────┐     │   ┌────────┴──────────┐
     │    etf1      │     │   │ port0=etf0       │
     │  (4KB buf)   │     │   │ port1=etf1       │
     └───────┬──────┘     │   └────────┬─────────┘
             │            │            │
             └────────────┼────────────┘
                          │
                ┌─────────▼─────────┐
                │    replicator     │                              LINK
                │    (1 → 2)        │                              (SPLIT)
                └────┬─────────┬────┘
                     │         │
          output0 ┌──▼──┐  ┌──▼──┐ output1
                  │TPIU │  │ ETR │                                 SINK
                  │外部 │  │ DDR │
                  └─────┘  └─────┘


  5. TRBE 路径 (ARMv8.4+, 无 Link)
  ────────────────────────────────

  传统:  CPU → ETM → [Funnel→ETF→Replicator→ETR] → DDR
              ↑                                 ↑
           ATB 总线                          AXI 总线外设
              多个 Link 组件在中间

  TRBE:  CPU → ETE ──内部硬连线──→ TRBE ──直接写──→ DDR (perf AUX buffer)
              ↑                      ↑
          CPU 内部              系统寄存器操作
                                  (TRBBASER/TRBLIMITR/TRBPTR)
              没有任何 Link 组件！


  6. 软件栈
  ─────────

  ┌─────────────────────────────────────────────────────────┐
  │  用户态 (EL0)                                            │
  │  perf record -e cs_etm//u -- ls                         │
  │  cat /sys/bus/coresight/devices/etm0/enable_source      │
  │  dd if=/dev/tmc_etr0 of=trace.bin                       │
  └──────────────────────┬──────────────────────────────────┘
                         │ syscall / ioctl / sysfs
  ┌──────────────────────▼──────────────────────────────────┐
  │  内核态 (EL1)                                            │
  │                                                          │
  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐ │
  │  │ coresight-core│  │ etm-perf     │  │ syscfg        │ │
  │  │ path 构建     │  │ PMU 集成     │  │ configfs 配置 │ │
  │  │ enable/disable│  │ AUX buffer   │  │ feature 注入  │ │
  │  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘ │
  │         │                 │                   │         │
  │  ┌──────▼─────────────────▼───────────────────▼───────┐ │
  │  │              设备驱动层                            │ │
  │  │  etm4x | stm | tmc-etr/etf/etb | funnel | replic. │ │
  │  │  cti   | catu| tpdm/tpda | trbe  | cpu-debug      │ │
  │  └──────────────────────┬─────────────────────────────┘ │
  │                         │ MMIO                           │
  └─────────────────────────┼───────────────────────────────┘
                            │
  ┌─────────────────────────▼───────────────────────────────┐
  │  CoreSight Hardware (ATB bus, Cross-trigger, Trace port) │
  └─────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════════════
  Summary:  SOURCE 产生 → LINK 路由 → SINK 存储 → 用户读出
            HELPER 辅助 (地址翻译/交叉触发)
            所有寄存器 EL1 访问，AUTHSTATUS 硬控权限，LAR Lock 软保护
            TRCVICTLR EXLEVEL bits 控制 trace 哪些 Exception Level
═══════════════════════════════════════════════════════════════════════════════
"""

with open('/Users/eelb1037/claude_cli/coresight_arch.txt', 'w') as f:
    f.write(diagram)

print(diagram)
