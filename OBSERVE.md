# 观测手段分层（stm32-dev 技能附录）

> 主文件：`SKILL.md`；踩坑记录：`PITFALLS.md`。
> 本文件回答：**想看清运行中的系统，该用哪种通道、怎么接进工程。**

## 🔬 观测手段分层（选型 + 通用集成法）

**原则：能"代码自己上报"就别用调试器；能"不占业务口"就别抢串口；能"先留现场"就别只写 while(1)。**

| 手段 | 通道 | 能证明物理链路? | 需插调试器? | 典型用途 |
|---|---|---|---|---|
| 串口/CAN 探针帧 | 业务/独立外设 | **能** | 否 | 链路验证、脱机/现场日志 |
| RTT 上行 | SWD 读 RAM | 不能 | **是(独占)** | printf 级日志、高频状态 |
| RTT 下行 | SWD 写 RAM | 不能 | **是(独占)** | 运行时改参数/触发命令 |
| DWT 打点 | SWD 读 RAM | 不能 | **是(独占)** | µs/ns 级耗时测量 |
| 故障现场转储 | RTT 或串口 | 不能 | 视通道 | HardFault/断言现场 |
| 调试器断点/快照 | SWD | 不能 | 是 | 执行路径确认、单次现场 |

### 1. 串口/CAN 探针帧（最先建立，永远保留）
固件周期性外发"标记+计数+时间戳+状态"。**它是唯一能证明物理链路通不通的手段**（坑#17），
且不依赖调试器——现场、脱机、产线都能用。任何"外设通不通"的排查，先建探针帧。

#### 1.1 自报帧的推荐格式（实战验证过的一套，可直接照抄）
把"标记 + 序号 + 时间戳 + 结论码"四样凑齐，一条帧就同时回答"通不通 / 丢没丢 / 板子自己认为哪儿不对"：

```
[IA] CHK seq=1 id=1 name=clk ok=1 v=170
[IA] RESULT seq=9 t=430 code=3 pass=6/8 first=motor
[IA] HB seq=13 t=8430 code=3 pass=6/8 drop=2
```

- `seq` **只在真的发出去之后才 +1** ⇒ seq 跳号 = 线上确实丢了帧（不是"以为发了"）；
- `t` = 目标侧 `HAL_GetTick()`，用来对齐"这行是谁在什么时候说的"（`RESULT` 的 t 是那一轮的完成时刻）；
- `code` / `pass` / `first` = **结论码**：`code=0` 全过，`code=N` = 第 N 项检查没过、`first` 直接给名字，
  于是"该先查哪儿"写在帧里，不用人肉比对数值（实测一趟：`code=3 pass=6/8 first=motor` ⇒ 电机那项没过）；
- `ok=` / `v=` = 每一项的结论与数值（`v` 放"最想知道的那个数"：主频 MHz、错误码、寄存器原值、µs…）；
- `drop` = 想发但没发出去的次数（含重试）。它和 seq 跳号是两回事：drop 是自己的通道忙，跳号是线上丢。

**三条安全规则（照做才不会打扰真主机）**：
1. **让路**——只在总线安静 ≥300ms 且发送空闲时才插一行，否则记 `drop`，下一圈再试；
2. **闭嘴**——一旦收到任何一帧合法业务帧（= 真有主机在轮询），本次上电内**永久停止**；
3. **节奏**——初始化/自识别有结论后（或最多等几秒）先来一轮完整自检，之后"心跳 + 周期性重跑全检"。

**两个省事的做法**：
- 别用 `printf`/`snprintf` 拼这种固定格式的行（链 nano 下省 ~1.5KB flash，也避免格式串写错要到运行期才炸）；
  手写"追加字符串 + 十进制转换"十几个函数就够，`#if` 开关关掉时留空函数，调用方不用跟着改。
- 若固件本来就有 RTT：**把同一行也镜像进 RTT**（`SEGGER_RTT_WriteString`，仅开发期）。这样插着调试器时
  从 RTT 看到的就是"刚刚真的发上总线的那一行"，**不用接串口适配器也能验证自报通道**（本技能这次就是这么验的）。
  注意 RTT 环缓冲只有 1KB 且读侧可能停顿（实测停过 5 秒 → 那段镜像行被覆盖），所以镜像只当开发期旁路，别当唯一证据。

**陷阱（本技能踩过）**：RTT 镜像行与其它 RTT 打印如果都带 `seq=`，同一份抓包里就有两条**同名序列**，
`--check-seq` 会把它俩混成一条、报出假丢帧（实测："稳态最大跳变 704, 超步长 3 次 -> 有丢帧"）。
技能现在按"`=` 前面那一小段前缀"分组统计并列出同名序列；自己写分析脚本时也要按"来源"分组。

### 2. RTT 上行（printf 级日志，不占串口）
**关键认知：J-Link"自带 RTT"≠ 可用**。RTT = 目标侧代码 + 主机侧客户端，J-Link 只是通道：
- 目标侧：把 SEGGER 官方 RTT 源码（`SEGGER_RTT.c/.h` + `SEGGER_RTT_Conf*.h`，可选 `SEGGER_RTT_printf.c`、`SEGGER_RTT_ASM_ARMv7M.S`）加入工程并初始化；
- 源码位置：J-Link 安装目录 `Samples/RTT/...`（**新版安装包可能不含**，那就从 SEGGER 官方仓库取，BSD 许可）；
- 集成：加 include 路径 + 源文件即可；默认缓冲上行 1KB / 下行 16B，模式默认 `NO_BLOCK_SKIP`（**不阻塞固件**，满则丢）；
- 主机侧：`JLinkRTTLogger -Device <DEV> -If SWD -Speed 4000 -RTTChannel 0 <logfile>`（**只读**，适合脚本抓包断言）；
- **验收标准**：抓到的行里**序号/时间戳严格递增、无跳变**（例：`seq` 与 `tick` 同步 100 行/秒）；
- **非侵入证据**：attach/detach 后时间戳**从上次的值继续**（不是 0）→ 证明没复位、没 halt。
- **环形缓冲 = "晚挂上去也能拿到开机日志"（实测好用）**：RTT 上行缓冲是 **1024 B 的环形缓冲**（满则丢最旧的）。所以**不必卡在复位那一刻挂 Logger** —— 复位后过几秒再 `JLinkRTTLogger`，仍能取到启动横幅与各模块 init 日志（本项目就是这么抓到 `reset cause` / `boot_flags=0x0085` 和"扫描到几个舵机"这类关键启动信息的）。
  - 反过来：**日志太多会把开头冲掉** —— 要抓启动序列，就把启动阶段的输出保持精简，或者抓完立刻转存。
- **Logger 独占 J-Link**：`JLinkRTTLogger` 跑着的时候**不能同时 `make flash`**（抢同一台探针，会失败）。抓日志和烧录必须串行。

### 3. RTT 下行（主机→目标，运行时改参数）
主机侧发送：J-Link 用 RTT 客户端（telnet 到 RTT Server，`JLinkRTTClient` 默认端口 19021），或直接用技能的 `rtt-send`；**ST-Link/DAPLink 也支持**（OpenOCD 的 `rtt server` 是双向的，往 socket 写就是写目标下行缓冲），同样一条 `rtt-send`。固件在主循环轮询：
```c
if (SEGGER_RTT_HasKey()) { int c = SEGGER_RTT_GetKey(); /* 单字符命令 */ }
/* 或批量: unsigned n = SEGGER_RTT_Read(0, buf, sizeof(buf)); */
```
- 默认下行缓冲仅 16B，发命令要先在 `SEGGER_RTT_Conf.h` 调大 `BUFFER_SIZE_DOWN`；
- **不是中断驱动**，必须固件轮询；行尾 `\n` 断帧更稳；
- 用途：改 PID/限位/模式、触发自检，**复用已有的寄存器写入函数**，不用重烧。

### 4. DWT 打点（µs/ns 级耗时，唯一可信的性能数据）
```c
/* 初始化一次 */
CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
DWT->CYCCNT = 0;
DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

/* 打点：先存时间戳，出了区间再打印 */
uint32_t t0 = DWT->CYCCNT;  ...待测代码...  uint32_t dt = DWT->CYCCNT - t0;
```
- 换算：`us = 周期数 / (核心时钟 MHz)`；**先自检基准**——`HAL_Delay(1000)` 前后差值应≈核心频率；
- **32 位回绕**：`2^32 / 核心频率` 秒回绕一次（480M≈8.9s、168M≈25.6s），**无符号相减差值仍正确**，但两点间隔别跨回绕；
- **printf 绝不进计时区间**（RTT printf 本身百 µs~ms 级）——只做"先存后印"；
- 读计数器本身有数周期开销，测亚 µs 要扣除；
- 统计量报 **max/avg/min + 抖动**，上电后连采 3~5s（配合坑#25/#26 的分层报告）；
- 差值恒 0/不稳 → 降级 `HAL_GetTick` 毫秒粒度，别死磕。

### 5. 故障现场转储（性价比最高，务必做）
`Error_Handler()` / `HardFault_Handler()` 里**只写 `while(1)` = 出事零信息**。正确做法是"先留现场再停"：
```c
/* 裸函数取异常帧: 不产生序言, SP 才真正指向异常栈帧;
   EXC_RETURN(LR) 的 bit2: 0=MSP, 1=PSP —— 有 RTOS 任务也取对。
   切勿在普通 C 处理函数里调 __get_MSP(): 编译器序言已经动过 SP, 读到的帧是错的。 */
void hardfault_entry(uint32_t *frame);

__attribute__((naked)) void HardFault_Handler(void) {
  __asm volatile (
    "mov r0, lr        \n"
    "tst r0, #4        \n"
    "ite eq            \n"
    "mrseq r0, msp     \n"
    "mrsne r0, psp     \n"
    "b hardfault_entry \n"
  );
}

void hardfault_entry(uint32_t *f) {
  BlackBox_Record(BB_TAG_HARDFAULT, f);      /* ① 先写黑匣子(跨复位保留) */
  SEGGER_RTT_printf(0, "!! FAULT cfsr=%08X hfsr=%08X bfar=%08X mmar=%08X\r\n",
                    SCB->CFSR, SCB->HFSR, SCB->BFAR, SCB->MMFAR);
  SEGGER_RTT_printf(0, "   pc=%08X lr=%08X psr=%08X\r\n", f[6], f[5], f[7]);
  /* ② 复位前留 ~300ms 让主机把 RTT 缓冲读走(RTT 是缓冲, 复位即丢) */
  uint32_t t0 = DWT->CYCCNT, wait = (SystemCoreClock / 1000u) * 300u;
  while ((DWT->CYCCNT - t0) < wait) { }
  if (BlackBox_BootCount() < 3u) { NVIC_SystemReset(); }   /* ③ 有限次自动复位 */
  while (1) { }
}
```

**① 异常栈帧是硬件自动压的**（ARMv7-M 进异常时固定顺序）：
```
SP+0x00 R0 | +0x04 R1 | +0x08 R2 | +0x0C R3 | +0x10 R12 | +0x14 LR | +0x18 PC ★ | +0x1C xPSR
```
- `f[6]` = 出错指令地址（最有用），`f[5]` = 调用者返回地址；
- 用了 PSP（RTOS 任务）时要用 `EXC_RETURN` 的 bit2 判断取 MSP 还是 PSP；开了 FPU 惰性压栈时帧变长但基础帧偏移不变。

**② 故障寄存器定性**（`CFSR` @0xE000ED28 = MMFSR|BFSR|UFSR）：

| 位 | 名称 | 含义 |
|---|---|---|
| 8 | IBUSERR | 取指总线错误（跳到非法地址执行） |
| 9 | PRECISERR | 取数总线错误，**地址精确** |
| 10 | IMPRECISERR | 取数总线错误，**地址不精确**（写缓冲，靠 PC 推断） |
| 4/13 | MSTKERR/STKERR | 出入栈出错（常见**栈溢出**） |
| 15 | BFARVALID | BFAR 里的地址有效 |
| 16 | UNDEFINSTR | 未定义指令 |
| 17 | INVSTATE | 非法状态（**跳到偶数地址，丢了 Thumb 位**） |
| 19 | NOCP | 用了协处理器但没使能（**FPU 没开**） |
| 24 | UNALIGNED | 非对齐访问（需开 UNALIGN_TRP） |
| 25 | DIVBYZERO | 除零（需开 CCR.DIV_0_TRP） |

`HFSR` @0xE000ED2C 只看两位：bit30 `FORCED` = 有明细故障被升级 → **去读 CFSR**；bit31 `DEBUGEVT` = 调试事件。
**注意：MemManage/BusFault/UsageFault 默认未使能，全部升级成 HardFault**，所以 `FORCED=1` 是常态。`CFSR` 读不清零（写 1 才清），可放心读。

**③ 定位三步法**：
```
HFSR.FORCED → 有明细  →  CFSR 定性  →  PC 反查源码行
arm-none-eabi-addr2line -e <elf> -f -p <pc>
```
`lr` 给调用者、`pc` 给出错点，两者一夹基本锁定。

**④ 实测样例**（故意从非法地址取指触发）：
```
!! FAULT HardFault cfsr=00000100 hfsr=40000000 bfar=00000000 mmar=00000000
   pc=0800614C lr=90000000 psr=08000FB3 r0=90000000 ...
```
`cfsr=0x100`=IBUSERR + `hfsr=0x40000000`=FORCED + `lr/r0=0x90000000`（非法目标地址）→ 三者互证，转储可信。

**⑤ 黑匣子：让现场跨复位活下来**（治偶发死机，实测可用）
偶发死机（跑几小时才挂）光靠实时打印抓不到——**让设备自己复位重跑，把现场留在 RAM 里事后取**。
```c
/* 记录区放 .noinit 段: 不加载、不初始化 -> 启动代码不清零 -> 复位后仍在 */
typedef struct { uint32_t magic, boot_count, crash_flag, tag;
                 uint32_t cfsr, hfsr, bfar, mmar, pc, lr, psr, tick, seq; } blackbox_t;
static blackbox_t g_bb __attribute__((section(".noinit")));

/* 链接脚本 */
  .noinit (NOLOAD) : { . = ALIGN(4); *(.noinit) *(.noinit*) . = ALIGN(4); } >RAM

void BlackBox_Init(void) {              /* 启动时调用, 且必须在 RTT/串口初始化之后 */
  if (g_bb.magic == BB_MAGIC) {         /* magic 有效 = 不是冷启动 */
    ++g_bb.boot_count;
    if (g_bb.crash_flag) { /* 打印上次崩溃现场 */ g_bb.crash_flag = 0; }  /* 只报一次 */
  } else { g_bb.magic = BB_MAGIC; g_bb.boot_count = 0; g_bb.crash_flag = 0; }
}
```
- **`.noinit` 不能放进 `.bss` 范围**，否则启动清零代码会把它擦掉；链接脚本单独开段 + `(NOLOAD)`。
- **有限次自动复位**：`if (BootCount() < N) NVIC_SystemReset();` —— 能自恢复就恢复，连续 N 次失败就停在原地等检查（防复位风暴）。
- **应用侧进度**：主循环里顺手存一个 `seq`/状态机值，崩溃记录里就能看到「跑到哪一步」。
- `Error_Handler` 里加 `__builtin_return_address(0)` 可定位「哪个 init 返回非 OK」（配合坑#22）。
- 通道：有调试器走 RTT，脱机走串口探针帧。
- **实测完整链路**（故意从非法地址取指）：崩溃→实时打印→写黑匣子→等 300ms→复位→重启后打印 `BLACKBOX: last crash ... pc=90000000 lr=08000FE3 tick=5001 seq=50 boot_count=1`。

**⑥ handler 纪律**：不做重活（长 printf、等外设、阻塞发送）、不触发二次故障，只做最小必要记录后复位/死循环。成本约 1KB flash、**运行时开销 0**。

### 6. 选型速查
```
要验证"外设/接线通不通"     → 串口/CAN 探针帧（唯一能证明）
要看固件内部状态、打日志     → RTT 上行
要测耗时/时序抖动           → DWT 打点（经 RTT 或探针帧送出）
要运行时改参数不重烧         → RTT 下行
要查偶发死机/断言           → 故障现场转储 + 黑匣子
```
**冲突提醒**：同一台探针同一时刻只能被一个程序独占。J-Link 的 RTT Logger 与 GDBServer 互斥（抓日志前先 `stop`）；ST-Link/DAPLink 走 OpenOCD 时 RTT 与 GDB 服务在同一个进程里，不必二选一。两台不同探针可以一台抓 RTT、一台烧录，互不干扰。

### 7. 交叉校验纪律（避免"自证清白"）

观测的最大陷阱是**只用自己的解码去验证自己的解码**。三条硬纪律：

1. **外部器件自己的读数优先**。协议解码对不对，用对方能吐出来的**原始量**校验：器件内部的浮点寄存器、上位机的显示值、或第二个独立抓包工具（USB-CAN 适配器、逻辑分析仪）。实测教训：位置解码一直"看起来正常"，直到读器件自己的浮点位置寄存器，才发现真实绝对值早已超出打包字段量程（见 PITFALLS #39）。
2. **每个结论要有"证伪通道"**。想证明"命令没生效"，必须同时能看到"对方确实收到了命令"（回环计数/对方反馈帧计数），否则收到但被忽略、和根本没收到，现象完全一样。
3. **注入式测试要隔离变量**。用注入帧/回环帧驱动时，注入内容只能触碰**与被测项无关**的字段；喂狗、时钟、观测通道都不能顺带改被控量（见 PITFALLS #37、#38）。

---
