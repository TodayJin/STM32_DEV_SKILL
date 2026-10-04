---
name: stm32-dev
description: |
  STM32 全流程开发技能：环境自检→编译→烧录→调试→发现问题→改代码→重烧→继续验证，
  循环直至功能正常才结束。**调试器通用**：J-Link / ST-Link(V3) / DAPLink(CMSIS-DAP) 插哪个用哪个
  （自动探测，能力与坑见 PROBES.md），全系列 STM32。含芯片自动识别、SVD 寄存器解码、自动烧录+刷后校验、
  观测手段分层（串口探针帧/RTT 上行/RTT 下行/DWT 打点/故障现场转储/黑匣子）、**开工前对照检查
  （`preflight`：拿技能里的结论把现有工程扫一遍，机械项自动判、判不了的列人工清单）**、
  空项目脚手架（`new`：自动搭 Makefile 工程 + 按调试器配好）、串口观测（`serial`）、27 个命令、
  --json 输出；附录 OBSERVE.md（观测手段）PITFALLS.md（89 条实战坑）与 PRACTICES.md（工程实践手册：
  上电时序 / 协议从站 / 控制与标定 / 状态灯 / 台架测试分层 / 上位机 / 工程流程 / 硬件板级 / 多轴差异）、
  PROBES.md（三种调试器各自的坑 / 可用工具 / 最强用法 / 降级路径）、SETUP.md（工具链怎么装、装在哪）。
  适用前提：已有 GNU Make/CMake 工程，或用 `new` 现场生成；不负责 Keil/CubeIDE 工程生成
  （那属于 stm32-development-workflow）。
metadata:
  author: EricSun
  version: 3.17.0
  date: 2026-10-11
---

# STM32 Dev Skill（全流程生命周期版）

## 概述
一个**完整的 STM32 开发闭环**：环境自检 → 编译 → 烧录 → 调试 → 定位 → 改代码 → 重编译 → 重烧 → 继续验证，
**循环到功能正常才结束**。不依赖 Keil/CubeIDE；调试器用 J-Link / ST-Link / DAPLink 任意一种
（默认自动探测，`probe use` 能写进工程固定下来；三种的区别与注意事项见 PROBES.md）。

核心原则：**开发不是一次性的，是"发现-修复-重验"的循环**，直到验收标准达成才收尾。

**观测优先原则**：优先用"固件自己上报"的通道（串口/CAN 探针帧、RTT 上行、DWT 打点）看**运行中的**系统；
调试器只做"断点确认执行"和"单次现场快照"。选型与通用集成法见「🔬 观测手段分层」。

**求助优先原则**：开发过程中，一旦缺少必要资料（板级原理图/引脚定义、外设手册、通信协议或帧格式、目标行为与验收标准、硬件/接线信息等），**先停下来向用户明确要**，不要自己埋头硬干或凭假设硬做。缺资料硬做 = 返工 + 走偏。要资料要说具体（哪个芯片/外设、哪份文档或截图、要什么），拿不到的先把能确定的做掉，并把剩余不确定处明确问用户，确认后再继续。

---

## 目录（按需跳读，不必全读）

| 章节 | 什么时候看 |
|---|---|
| ⛳ 开工前对照检查 | **接手任何已有工程的第一步**：`preflight` 拿技能里的结论把工程扫一遍（机械项自动判 + 人工清单） |
| ⭐ 边改边测试 | **每次开工前必读**：一次只改一个变量 |
| ✅ 调试正确姿势 | 铁律：调试器只做两件事、观测变量归属、寄存器纪律、**提问式排障（物理事实交给用户）**、**上电/标定/判定的安全默认态** |
| 🚑 卡死急救 | 命令卡住/连不上/板子像死了 → 先 `cleanup` |
| 🧰 技能自检 & 脚手架 | preflight / selftest / init-rtt / init-fault / verify |
| 🔬 观测手段分层 | 选哪种观测通道 + 通用集成法（RTT/DWT/故障转储/黑匣子） |
| ①~⑨ 各阶段 | 具体命令（doctor/make/flash/read/rtt/blackbox…） |
| 📋 案例复盘 | "串口收不到"一整天：错误的排查路径 |
| 📚 附录 | `OBSERVE.md` 观测手段 / `PITFALLS.md` 89 条坑 / `PRACTICES.md` 工程实践手册（见文末） |

---

## 完整流程

```
⓿ preflight(开工前对照检查) → ① doctor(环境自检) → ② make(编译) → ③ flash(烧录) → ④ 调试观察(发现问题)
                                                              ↓
⑨ 全通过=结束  ←  ⑧ 重新验证(读变量/复测)  ←  ⑦ 重烧  ←  ⑥ 改代码  ←  ⑤ 定位根因
```

**循环直到**：所有目标功能验证通过、无残留问题、验收达成。

---

## 🔌 第 0 步：选调试器（`probe`）与建工程

**新工程开工前先问一句**：`probe detect` 看插着哪个 → 用一句话把结论说给用户听（「发现的是 ST-Link V3；它最强的是烧录校验、选项字节和救砖，实时日志比 J-Link 慢」）→ 问「就用它吗」。用户手上还没有探针时，把三种的取舍讲清楚（PROBES.md 第 0 节）再让他挑。**只有用户明确说没有某个条件（例如板子没引出 SWO、只有 4 针 SWD）才降级**，否则一律按这台探针的最强能力配。选完 `probe use <名>` 写进工程，后面全自动。

```bash
"$SKILL/scripts/stm32-dev.py" probe list             # 三种调试器各自的能力 + 本机缺什么工具
"$SKILL/scripts/stm32-dev.py" probe detect           # 插着的调试器认一认(带序列号)
"$SKILL/scripts/stm32-dev.py" probe use stlink       # 用 ST-Link, 并写进工程配置 .stm32-dev.json(以后不用再指定)
"$SKILL/scripts/stm32-dev.py" probe info stlink      # 这台调试器的能力/注意事项/最强用法/降级路径
"$SKILL/scripts/stm32-dev.py" setup --fix            # 一键体检 + 能自动补的直接补(写探针配置 / pip 装 pyserial·pyocd)
"$SKILL/scripts/stm32-dev.py" setup --install        # 还缺编译器/openocd 时连系统包也自动装(winget/apt)
"$SKILL/scripts/stm32-dev.py" new --dir . --device STM32G431CBT6   # 空文件夹 -> 能直接 make/flash 的最小工程
```

**不用记这些**：插上哪个调试器技能就自动用哪个；配一次（写进工程）之后所有命令都走对的那条路。
三种调试器的取舍、各自的坑、能用的额外工具 → **PROBES.md**；缺工具怎么装 → **SETUP.md**。

## ⛳ ⓿ 开工前对照检查（接手已有工程的第一步）

**原则：先拿技能里的结论把「现有工程」扫一遍，再动手。** 这份技能里的坑，有一大半是「接手别人
（或自己三个月前）的工程，上来就调，调了半天才发现地基是歪的」——省下的是「踩过了才想起来」那一遍。

```bash
python3 skills/stm32-dev/scripts/stm32-dev.py preflight            # 扫当前目录
python3 .../stm32-dev.py preflight --root /path/to/project        # 指定工程
python3 .../stm32-dev.py preflight --json                         # CI / 脚本里用
```

它分两半：

**① 机械能判的，直接判掉（有 `[!!]` 就退出码 1，可进 CI）**

| 检查 | 判据 | 坑 |
|---|---|---|
| 烧录/复位脚本 | `r` 与 `g` 之间、`g` 与 `exit` 之间必须有 `Sleep`（Makefile 里塞在一个 `printf "…\n"` 中的也算） | #80 / #22 |
| 故障处理器 | `HardFault/MemManage/BusFault/UsageFault` 不能只有 `while(1)`，要有 printf / RTT / 黑匣子 | #32 |
| 看门狗 | 用了 `HAL_IWDG_Init` 就必须有 `__HAL_DBGMCU_FREEZE_IWDG/WWDG`（否则一进断点就被狗咬复位） | #59 |
| 观测通道 | `.noinit` 段 / `g_bb` / RTT / 收发计数器 / 只读 `DBG_*` 寄存器，缺哪些列哪些 | OBSERVE.md |

**② 机械扫不出来的，列成人工清单**（`[ ]` 逐条对过）：请求队列是应答驱动还是发出即出队（#81）、
读回来的值有没有做合理性检查（#82/#83）、寄存器单位查过手册没有（#84）、SRAM/EPROM 双寄存器与写锁
（#85）、门卫条件互锁与派生状态缓存（#86/#87）、切模式的参数/模式书写顺序（#88）、模拟量零点标定入口
（#89）、中断共享变量的 volatile/临界区（#57/#58）、偶发现象的黑匣子是否就绪（#32）。

**这一步只读，不改任何东西。** 扫完再决定先修哪个——先修地基，别先修症状。

---

## ⭐ 边改边测试（本次排查的制胜方法论，务必遵守）

**原则：一次只改一个变量，改一丁点就烧录+验证，验证过了才改下一处。绝不大改一堆再一起测。**

### 1. 最小工程起手
新板子/新外设排查，先写"最小能跑"工程（纯 USART 自发回环,什么都不挂），确认:**工具链能编译、能烧录、引脚能配置、能收发**。这一步不通过,后面全是白搭。

### 2. 逐项加功能,每步必验
```
① 自发收(回环) → ② 协议解析 → ③ 寄存器映射 → ④ CAN/控制
每加一步: make → flash → 观测验证 → 通过才进下一步
```
- 每步都留下**可观测的探针帧**(固件主动外发状态:计数器/时间戳/标志),主机收——这是非侵入验证(见坑#17);
- 验证通过的步骤**不再回头动**(只在新步骤里叠加)。

### 3. 单变量差分法
现象异常时,先问"**我上次改了什么**",回退那一个改动重测。典型:
- 换引脚/换时钟/换发送方式/换帧逻辑,**一次只换一个**;
- 怀疑 `-O1`/优化问题 → 临时 `-O0` 试;
- 怀疑时钟 → 用**板厂官方例程**的时钟配置;
- 怀疑观测 → 换探针帧(不用调试器)。

### 4. 每步的"验收标准"要具体
不是"应该能跑",而是可断言:如"发 `01 03 00 00 00 01 84 0A` 收到 `01 03 02 12 34 B5 33`"、"回环收到的字节数 3 秒内从 0 涨到 ≥48"、"控制帧 1 秒 ≥990 帧"。**能自动跑脚本断言的最好**(Python + SerialPort + CRC),避免人工看漏。

### 5. 保留黄金版本
每通过一步,把能跑的固件 `cp` 成 `build/vN.elf` 备份;改坏了立刻回退黄金版本重测,不纠结在坏版本上。

### 6. 排查顺序（从外到内,排除法）
```
① 物理接线/电平(万用表/探针帧/回环) → ② 外设寄存器实测(JLink/bare addr) → ③ 时钟/波特率
→ ④ 代码逻辑(读代码+断点) → ⑤ 观测方法(换探针帧)
```
先排除物理和配置,再怀疑代码——这次"收不到"的假象,全被①②步的调试假象带偏,是⑤才破的。

---

## 📋 真实案例复盘："串口收不到" 浪费一整天（通用教训）

**现象**：某型号 STM32 开发板串口收不到主机数据，协议一直无响应。

**错误排查路径（都是坑,勿重蹈）**：
1. 读串口寄存器 → 用错偏移/基址 → "波特率寄存器是垃圾值" 假结论 → **改本来正确的配置**；
2. 读时钟源寄存器 → 用错地址 → "时钟源没写进去" 假结论 → **反复试各种时钟配置宏**；
3. 读 GPIO 输入寄存器 → 恒为某个值 → "引脚被钉死" 假结论 → **怀疑硬件/换引脚**；
4. 调试器反复读计数器不变 → "程序卡死" 假结论；
5. 逐个"修复"伪问题，一整天原地打转。

**正确路径（半小时内解决）**：
1. **写最小工程**（纯串口自发回环，无业务逻辑）；
2. **固件主动发探针帧**(标记+状态)经同一串口，主机收——第一次看到接收计数持续增长 = 回环通了，`硬件与引脚实好` 被证实;
3. **用官方例程配置**(时钟/引脚/外设时钟源照抄厂家的正确写法)；
4. 逐项加功能：协议→寄存器→使能→控制，每步烧录+探针验证；
5. 修复真 bug：漏调某外设初始化(进 Error_Handler)、帧边界按固定长度假设(不定长协议错位);
6. 最终：功能全通过、性能达标。

**一句话教训**：**先怀疑"我怎么读的"，再怀疑"它坏了"**。调试器读数是假象高发区；非侵入探针帧 + 最小工程 + 官方配置 = 排查正解。

**检查清单(下次"串口不通"直接走)**：
- [ ] 接线（原理图核脚：GND 共地？TX→RX 对？）
- [ ] 官方例程的时钟/引脚配置对照
- [ ] 固件自发探针帧确认通路
- [ ] 纯回环（自发自收）分"外设/接线"问题
- [ ] 读寄存器前先核地址（头文件 struct 偏移）
- [ ] 帧边界是否适配不定长协议

---

## ✅ 调试正确姿势（铁律，含这次一整天换来的教训）

**一句话：debug = 观察，不是动手；数据要靠"代码自己上报"，调试器只做"一次现场快照"和"断点确认执行"。**

### 1. 调试器只做两件事
- **① 断点确认程序执行**：`break 函数名` → `continue` → 命中 = 代码走到这了（顺序/分支验证）；
- **② 单次现场快照**：halt 后读 PC / 调用栈 / 局部变量，看清"这一瞬间在哪、为什么"。
- **绝不做**：连续读数据、改寄存器、测性能。

### 2. 看"在跑的数据"= 代码自己上报（非侵入）
- 需要观测的**计数/时间戳/状态/时延** → **在固件里定义 volatile 全局**，在关键路径赋值；
- 通过 **①串口探针帧**(每周期自发一条,主机解析) 或 **②主机一条寄存器读** 取值；
- 这样 **CPU 真实运行**、无 halt、无主机计时误差——测性能/测时序只能用这个（坑#25/#26）。
- **观测变量绝不能被业务代码清零**（坑#21）——只允许主机侧读/清。

### 3. 电源/时间线安全规则
- **烧录后不要立刻连调试器**（坑#22）：JLink/GDB server 连接即 halt，读到的全是复位态/冻结值——先等 1s，纯主机侧(串口)确认在运行，再考虑调试。
- **GDB server 一直控制 CPU**：`detach` ≠ 恢复运行；每次读后必须 `continue`/`go`，否则后续测试全部打在冻结状态上（最隐蔽的坑:探针帧"突然没了"、计数"不变"都是它）。
- **验证时序**：改码 → `make clean && make && make flash` → 主机侧验证 → 才允许碰调试器。

### 4. 寄存器读写纪律
- **读**：先核对地址（坑#19：各系列偏移/基址不同，查 `stm32XXXxx.h` struct 偏移）；注意**读有副作用的寄存器**（读 USART RDR 清 RXNE = 消费数据）。
- **写：绝不用调试器**（坑#24）——测试引脚/外设行为用**固件自己配置**或写一次性测试固件；误写了立即复位/重烧，不分析污染态。
- 要"改参数试行为"：改代码 → 重烧（本来就是唯一正路）。

### 5. 性能测量基准
- 微秒级：固件内 `DWT->CYCCNT` 打点（T1/T2 差值存全局）→ 主机读，**不要主机侧计时**（主机开销占 99%+,坑#25）；
- DWT 不稳定 → 降级 `HAL_GetTick` 毫秒差（判断节拍稳定/丢步够用）；
- 报告分三段写：**固件处理时延 / 端到端(含传输) / 主机 RTT**，各测各的，不混（坑#26）。

### 6. 把「只有用户的眼睛能定的事实」交给人（提问式排障）
有些事实**板上任何寄存器都测不出来**：机构往哪边动、到没到机械端点、有没有卡住、装没装、线插没插。
这类问题**不要靠日志、推理或口头回忆去定**（坑#50 的实例：同一个「哪端是张开」，用户在不同场合说过
两个互相矛盾的话，采信错的那条 → 写出「方向反了」的错误结论、还改了代码）。

**规矩：物理事实 → 造一个 30 秒能做完、看得见、且安全的动作，然后问用户。**

1. **先让板子摆出「看得见 + 数字对得上」的状态**：动作要小、慢、低力（例 `20~30%` 力、`0.10~0.15 rad`、
   分 2~3 小步、每步停 1.5 s），**全程高频测力矩、突变立即停车**；动完**保持使能顶住、先别失能**，
   让用户看着的同时报出确切读数 —— 他回答的就是这个读数下的状态。
2. **一次只问一件**。「往哪边动」和「到没到头」是两个问题，后者的答案依赖前者。
3. **选项 2~4 个，每个写清「选它 → 下一步做什么」**：`往张开动 → 正=张开、负端就是闭合端`。
   用户不是在答题，是在**选分支**。
4. **必须给一个「看不清/说不好」的出口**，并写明兜底（加长到 0.4 rad / 反向再走一次 / 失能让他用手掰）。
   逼用户在两个选项里二选一 = 让他猜，而猜出来的答案会被当成实测数据用下去。
5. **答案连数字一起落盘**：记「用户目视：正走 0.1155 rad = 钳口张开」，而不是「正=张开」——
   有数字的那条下次才能被验证。**新答案推翻旧答案时显式作废旧的**，并同步改掉所有基于它的文档与结论。

**反向也成立**：能用探针/寄存器测出来的，**不要拿去问用户**（"你觉得电机热不热"毫无意义，读线圈温度就行）。
只有人眼、人手才能获得的信息，才值得占用用户的注意力。

### 7. 上电、标定、判定：先把「安全默认态」定下来，再谈功能
有三类**不会报错的静默故障**，根子都在"默认态没定义好"（细节见 PITFALLS #55 / #68 / #69 / #70）：

- **上电默认态**：复位后位置基准是 0，而"全闭"常常恰好也是 0 → 直接使能 = **冲向全闭**。
  铁律：**① 直读当前位置 → ② 写进目标位置 → ③ 给速度/力 → ④ 最后才使能**，一步都不能省；
  上电**不自动使能、不自动回零**（#55；少了 ③ 就是 #52 的"命令生效但一步不走"）。
- **标定默认态**：一键标定**失败绝不写 flash**（旧参数继续有效），标定期间**拒绝外部使能/位置命令**（#68）。
- **判定默认态**：会漂的量（空爪闭合停位、夹持阈值）**绝不能是编译期常量** ——
  必须是**标定产物存 NVM**（#70）；阈值要"**量**出来 + 留余量"，不能拍（#69）。

**共同点：出问题时绝不留下一个"看起来正常"的状态** —— 要么明确失败并把它报出来，
要么保持**上一个已知有效值**。"会漂的常量 / 会失败还照写的 flash / 会冲向 0 的上电"这三样，
都属于"功能全对、现场出事"。

---

## ① 环境自检 & 引导

```bash
"$SKILL/scripts/stm32-dev.py" doctor --elf build/test.elf     # $SKILL = 技能根目录(见文末目录结构)
```
检测调试器（按探针：J-Link / ST-Link 官方 CLI / OpenOCD）、gdb、readelf、python3，识别芯片，定位 SVD，缺什么给指引。

## ② 编译

```bash
make              # 编译
make clean && make  # 干净重编(改Makefile/源文件后)
```
产物 `build/*.elf/.hex`。**调试前确认 ELF 最新**（否则旧固件值全垃圾）。

## ③ 烧录（按调试器自动选路，不用你操心）

```bash
# 方式1
make flash    # 若工程已配
# 方式2 (技能自带)
"$SKILL/scripts/stm32-dev.py" flash --elf build/test.elf --device STM32H743VI
# 底层按调试器走, 三种都不用图形界面(拿得到返回码):
#   J-Link  -> JLink.exe -device <DEV> -if SWD -speed 4000 -autoconnect 1 -CommanderScript flash.jlink
#   ST-Link -> STM32_Programmer_CLI -c "port=SWD freq=8000" -w <hex> -v -rst
#   DAPLink -> openocd -f interface/cmsis-dap.cfg -f target/<系列>x.cfg -c "program <elf> verify reset exit"
```
每改一次代码都要重烧，否则板子跑旧固件。

**`flash` 默认刷完自动校验**：读回板子 Flash 与 ELF 逐字节比对，输出 `VERIFY: 板子固件与 ELF 一致` ——
直接消灭坑#9（"板子跑旧固件"这个最常见的假 bug）。

**没验过就等于失败**：校验结果不一致、**校验无法完成**（缺 objcopy/readelf、ELF 解析不了、读回 Flash 失败）、
或者根本没有 ELF 可比 —— `flash` 一律返回退出码 1（烧录本身可能成功了，但"没被验证过"）。
确实不打算校验，必须显式写 `--no-verify`，那时才会打印 `VERIFY: 已按 --no-verify 跳过` 并返回 0。

**第一次动接了执行器（电机/电缸）的板子，先加 `--dry-run`**：`flash --dry-run` / `reset --dry-run`
只打印「将要执行的命令 + 技能给你选了哪条路」，板子一点都不碰（退出码 0）。确认无误再去掉它真执行。

### 📡 串口观测（`serial`：不占调试器，换任何探针都一样用）
```bash
"$SKILL/scripts/stm32-dev.py" serial --port COM21 --seconds 5          # 读 5 秒
"$SKILL/scripts/stm32-dev.py" serial --list                           # 列本机串口(自动挑最像目标板的一个)
"$SKILL/scripts/stm32-dev.py" serial --port COM21 --grep "ERR|FAULT"  # 只看关心的行(没命中退出码=1)
"$SKILL/scripts/stm32-dev.py" serial --port COM21 --check-seq --seconds 10   # 查自增序号有没有跳变(判丢帧)
```
串口是**唯一一条"不用调试器也能看运行现场"**的通道，可以和烧录/调试**同时**进行（调试器独占的是探针，不是串口）。
固件里每帧带一个自增 `seq=` 最省事 —— 没序号就判不了丢帧（详见 OBSERVE.md 的串口探针帧）。

## ④ 调试观察

```bash
"$SKILL/scripts/stm32-dev.py" read g_motor.enabled g_motor.mode   # 读变量
"$SKILL/scripts/stm32-dev.py" read 0xE000ED00 0xE0001000          # 读裸地址(内核/没有 SVD 的外设)
"$SKILL/scripts/stm32-dev.py" read 0xE000ED00 --size 1            # --size 1/2/4/8 指宽度(默认 4)
"$SKILL/scripts/stm32-dev.py" read "*(uint32_t*)0x20000000"       # gdb 表达式也行(符号名原样透传)
"$SKILL/scripts/stm32-dev.py" write g_motor.enabled=1             # 写变量
"$SKILL/scripts/stm32-dev.py" write 0xE000EDF0=0xA05F0003         # 写裸地址(例: DHCSR)
# 常用裸地址: 0xE000ED00 CPUID / 0xE000ED28 CFSR / 0xE000EDF0 DHCSR
#            0xE0001000 DWT_CTRL(bit0=1 才说明这芯片有 CYCCNT) / 0xE0001004 CYCCNT
# read/write/info/break 默认读完后自动恢复运行(detach ≠ 恢复运行, 坑#16);
# J-Link 发 `monitor go`, ST-Link/DAPLink 发 `monitor resume`(OpenOCD 没 go 这条命令); 想保持 halt 加 --keep-halted
"$SKILL/scripts/stm32-dev.py" svd --elf build/test.elf GPIOA.MODER   # 读寄存器并用 SVD 解码位域
"$SKILL/scripts/stm32-dev.py" verify --elf build/test.elf           # 校验板子固件 == ELF(坑#9); 不一致退出码=1
"$SKILL/scripts/stm32-dev.py" reset --elf build/test.elf            # 复位+运行
"$SKILL/scripts/stm32-dev.py" blackbox --elf build/test.elf         # 读 .noinit 黑匣子(上次崩溃现场)
"$SKILL/scripts/stm32-dev.py" init-fault --dir .                 # 生成故障转储+黑匣子模板代码
"$SKILL/scripts/stm32-dev.py" break main.c:176                   # 断点
"$SKILL/scripts/stm32-dev.py" continue / step 3 / info           # 运行控制
"$SKILL/scripts/stm32-dev.py" attach --elf build/test.elf --device STM32H743VI  # 交互
```

```bash
"$SKILL/scripts/stm32-dev.py" rtt --elf build/test.elf --seconds 8            # 抓 RTT 上行(只读)
"$SKILL/scripts/stm32-dev.py" rtt --elf build/test.elf --check-seq            # 抓完自动判丢帧
"$SKILL/scripts/stm32-dev.py" rtt --device STM32H743VI --address 0x20000768  # 手动指定控制块(默认已自动解析)
```
`rtt` 的行为：
- **按调试器自动选通道**：J-Link 走 `JLinkRTTLogger`；ST-Link / DAPLink 走 OpenOCD 的 `rtt setup` + `rtt start` + `rtt server start <端口> <通道>`（本地 TCP，默认端口 9090，`--rtt-port` 可改），抓完按 PID 收干净；
- **地址先解析、抓空会自愈**：先取 ELF 符号 `_SEGGER_RTT` 的地址（最快最准）；**一条数据都没抓到**就自动改用 RAM 搜索（`0x20000000 0x20000`）再抓一次 —— 这通常意味着**板子跑的不是这份 ELF**（坑#9，符号地址属于别的构建）；也可以自己给 `--address` / `--search`；
- **抓包前自动恢复运行**：避免"CPU 被 halt → 只抓到缓冲区快照"（行数 ≤2 时也会提示）；`--no-resume` 可关掉；
- **`--check-seq`**：自动找出递增计数列并报告稳态最大跳变/丢帧次数（首段追赶不计）。
- **下行（回灌）也能用**：`rtt-send --data "cmd\n"` —— J-Link 走 pylink 直连 DLL，ST-Link/DAPLink 走 OpenOCD 的 `rtt server`（**它是双向的**：往 socket 写 = 写目标的下行缓冲）。固件侧仍然要轮询 `SEGGER_RTT_HasKey()/GetKey()`。
RTT 控制块地址：`arm-none-eabi-nm <elf> | grep _SEGGER_RTT`（技能默认已自动解析）。
**互斥说明**：同一台探针同一时刻只能被一个程序独占 —— J-Link 的 `JLinkRTTLogger` 与 `JLinkGDBServerCL` 互斥（技能抓包前会自动 `stop`）；OpenOCD 的 RTT 与 GDB 服务在同一个进程里，不互斥。别的探针怎么抓观测通道见 PROBES.md。

### 🚑 卡死急救（第一反应就敲这个）
```bash
"$SKILL/scripts/stm32-dev.py" cleanup               # 默认只清本工具自己启的常驻 server(安全)
"$SKILL/scripts/stm32-dev.py" cleanup --dry-run     # 只看不杀
"$SKILL/scripts/stm32-dev.py" cleanup --all --force # 全局清场: 强杀所有 J-Link 进程(会打断 IDE/别的会话)
```
**任何"连不上/命令卡住/板子像死了"，先跑 cleanup，再谈别的** —— 十次有八次是残留进程占着 J-Link（`JLinkRTTLogger` 退出会留下 `JLinkGUIServer`）。注意：**杀包装脚本的父进程 ≠ 杀子进程**，用 Python/后台任务包一层启动 RTT 客户端时，客户端会变孤儿继续占设备。

芯片识别：自动从 ELF 读。失败用 `--device <完整型号>`。

### ⚡ 性能优化：先 start 常驻再反复 read（关键）
每命令默认会起/杀 GDB server（J-Link 是 `JLinkGDBServerCL`、ST-Link/DAPLink 是 `OpenOCD`，约 1.7s），反复读变量很慢。**优化后**：
```bash
"$SKILL/scripts/stm32-dev.py" start --device STM32H743VI   # 启动常驻 server(detached)
"$SKILL/scripts/stm32-dev.py" read ... read ...            # 复用, 延迟 ~180ms(非1978ms)
"$SKILL/scripts/stm32-dev.py" stop                         # 清理常驻 server
```
- `start` 用 detached 启动独立 server，跨命令存活（PID 记到 temp）。
- `read` 检测到 server 已运行就**复用不杀**。
- `stop` 按记录的 PID 清理。
- **实测**：优化前 1978ms → 优化后 ~195ms（降约 10 倍）。

- ⚠️ **多探针务必带 `--serial <S/N>`**：同时插两台同型号探针时，不给串号就由工具自己挑 —— 可能这次 read 打在 A 板、
  下次 flash 打到 B 板。串号会写进 pid 文件；若端口上跑的 server 不是本工具启动的（串号/设备名不符），
  命令会**拒绝复用**并提示先 stop。

## ⑤ 定位根因
- `info` → PC/寄存器/调用栈
- `x/8bx &var` → 原始内存（比 print 结构体可靠）
- 断点+单步 → 看走到哪
- **优先简单函数调用**（坑#11）

## ⑥ 改代码 → ⑦ 重烧 → ⑧ 重新验证
```bash
make clean && make     # 重编译
make flash             # 重烧
# 再读变量复测
"$SKILL/scripts/stm32-dev.py" read <var> --elf build/test.elf
```
**关键坑#9**：改完必须 make + 重烧，否则读到的还是旧值。

**自动闭环**（开发循环免手动）：
```bash
"$SKILL/scripts/stm32-dev.py" build-verify \
  --elf build/test.elf --device STM32H743VI \
  --read g_motor.motor_id g_motor.kp \
  --max-iter 3
```
自动: 编译 → 烧录 → 读变量 → **在板上求值 --check 断言**；一旦为真立刻成功退出(0)，
跑满 --max-iter 仍不为真则失败(退出码 1)。例子：`--check "g_motor.err < 2"`。
不给 --check 时就是单纯重复 N 次，全成功返回 0。

## ⑨ 验收 & 结束
- 目标功能全通过 → 收尾记录。
- 仍有问题 → 回⑤ 继续循环。
- 验收检查：变量初始化正确、协议转换数值正确、无 HardFault。

---

## 🧰 技能自检 & 脚手架（把方法变成可执行）

```bash
"$SKILL/scripts/stm32-dev.py" preflight                            # ★开工第一步: 对照技能扫现有工程
"$SKILL/scripts/stm32-dev.py" selftest --elf build/test.elf   # 无硬件自检技能自身机制
"$SKILL/scripts/stm32-dev.py" init-rtt   --dir .              # 把 SEGGER RTT 源码放进工程
"$SKILL/scripts/stm32-dev.py" init-fault --dir .              # 生成故障转储+黑匣子代码
"$SKILL/scripts/stm32-dev.py" verify     --elf build/test.elf # 板子固件 vs ELF 一致性
"$SKILL/scripts/stm32-dev.py" cleanup                         # 清残留 J-Link 进程
```
- **`preflight`**：**接手任何已有工程的第一步**。扫 `Makefile`/`.jlink`/CI 里的烧录脚本（#80）、故障处理器
  有没有现场记录（#32）、看门狗有没有调试冻结（#59）、观测通道齐不齐（OBSERVE.md），再列出 9 条机械扫不出
  来的人工对照项。**只读**；有 `[!!]` 就退出码 1，可直接进 CI。
- **`selftest`**：不接板子也能跑，验证序列分析/模板生成/ELF 解析/工具定位是否正常（改过技能后先跑它）；
- **`init-rtt`**：从 J-Link 安装目录复制 RTT 源码（新版安装包常不含 → 会提示官方仓库），并打印接线步骤；
- **`init-fault`**：生成 `blackbox.c/h` + `fault_dump.c/h`（裸函数取异常帧、有限次自动复位、300ms 冲刷），并打印链接脚本/初始化顺序；
- **`verify`**：读回 Flash 与 ELF 逐字节比对。**"板子像有 bug"先跑它**，能直接排除"跑的是旧固件"。


---

## 📚 附录（按需读取，不必全读）

| 文件 | 内容 | 什么时候读 |
|---|---|---|
| `OBSERVE.md` | **观测手段分层**：串口探针帧 / RTT 上行 / RTT 下行 / DWT 打点 / 故障现场转储 / 黑匣子，含通用集成法 | 要接观测通道、或不确定该用哪种时 |
| `PROBES.md` | **三种调试器**（J-Link / ST-Link / DAPLink）各自的：能力表、注意事项、能用的工具、最强用法、降级路径 | 换/买调试器、某个命令报"没探针"、想用某个高级功能时 |
| `SETUP.md` | **工具链怎么装**：CubeCLT / STM32CubeProgrammer / OpenOCD / pyOCD / Cube 固件包 / 驱动 | 新机器、缺工具、探针认不出来时 |
| `PITFALLS.md` | **89 条实战踩坑**（含按主题索引） | **遇到怪现象先搜它**；开工前扫一遍标题 |
| `PRACTICES.md` | **工程实践手册**：上电与初始化时序 / 从站与总线协议实现清单 / 控制·标定·夹持判定 / 状态灯与现场可观测性 / 台架测试分层 / 上位机与 SDK / 工程流程 / 硬件板级 / 多轴差异清单 | **新板 bring-up 前、写协议或标定模块前、收尾前**；想知道"怎么做对"（不是"怎么被坑"）时 |

### 🔟 十条最高频的坑（内联速查，全文见 PITFALLS.md）

1. 烧录/校验都走命令行，图形界面拿不到返回码：J-Link 用 `JLink.exe`、ST-Link 用 `STM32_Programmer_CLI -w -v`、DAPLink 用 `openocd ... program verify`（插哪个用哪个，`probe list` 看现状）
2. 设备名要完整：`STM32H743VI`（不是 `STM32H743`）
3. **板子跑旧固件**是最常见的假 bug → `verify` 命令一键比对
4. 调试器只做两件事：断点确认执行、单次现场快照；**别用它连续读数据**（halt 后全是冻结快照）
5. `detach` ≠ 恢复运行（命令已默认 `monitor go`，但概念要记住）
6. **烧录脚本里 `r` 与 `g` 之间不能省 `Sleep`**：`g` 后立刻 `exit` 会把核留在复位态 → "烧完板子像死了、串口一个字不回"（坑 #80）；烧完先等 1s，纯主机侧确认在跑
7. 绝不用调试器**写**外设寄存器做测试（污染现场，无法区分是代码还是你干的）
8. 读寄存器前先核对地址（各系列偏移/基址不同）
9. 观测变量只能由主机侧清零，业务代码不许清
10. 命令卡死/连不上 → 先 `cleanup`（残留 J-Link 进程独占设备）

## 环境变量
| 变量 | 默认 | 说明 |
|---|---|---|
| JLINK_GDB_SERVER | 自动 | JLinkGDBServerCL 路径（J-Link 专用） |
| STM32_DEBUG_GDB | 自动 | gdb 命令 |
| JLINK_GDB_PORT | 3333 | GDB 端口 |
| JLINK_SN | 空 | J-Link 序列号 |
| JLINK_RTT_LOGGER | 自动 | JLinkRTTLogger 路径（J-Link 专用，RTT 抓包） |
| STM32_DEV_PROBE | 自动 | 强制用一种调试器(jlink/stlink/daplink); 优先级: --probe > 本变量 > 工程配置 > 自动探测 |
| STM32_PROGRAMMER_CLI | 自动 | STM32_Programmer_CLI 路径(ST-Link 烧录/校验/救砖/选项字节) |
| OPENOCD | 自动 | openocd 路径(DAPLink 烧录 + ST-Link/DAPLink 的调试服务) |
| STLINK_GDB_SERVER | 自动 | ST-LINK_gdbserver 路径(可选: SWO/semihosting) |
| PYOCD | 自动 | pyocd 路径(可选: 列 DAPLink 探针) |
| STM32_CUBE_REPO | ~/STM32Cube/Repository | Cube 固件包目录(`new` 脚手架找 CMSIS 头/启动文件/链接脚本)

先跑 `probe list` 和 `setup` 看哪些缺、怎么装 —— 装法见 **SETUP.md**。

---

## 结构（技能完全自包含，换环境也能用）
本技能**不依赖任何外部文档/路径**——工具自动定位、芯片自动识别、SVD 自动查找、踩坑记录都在附录里。

```
stm32-dev/
├── SKILL.md            (本文件: 流程 + 方法论 + 命令用法 + 十条速查)
├── PROBES.md           (附录: 三种调试器 —— 能力/注意事项/可用工具/最强用法/降级路径)
├── SETUP.md            (附录: 工具链怎么装 + 驱动 + 环境变量)
├── OBSERVE.md          (附录: 观测手段分层 + 通用集成法)
├── PITFALLS.md         (附录: 89 条实战坑 + 主题索引)
├── PRACTICES.md        (附录: 工程实践手册 —— 上电时序/协议从站/控制与标定/状态灯/台架/上位机/流程/硬件板级)
├── CHANGELOG.md        (版本演进)
└── scripts/
    └── stm32-dev.py    (单文件引擎, 27 个命令, 仅标准库*; --json 输出)
```

* 只有两处要额外库：`serial` 要 `pyserial`（`pip install pyserial`）、**J-Link** 的 `rtt-send` 要 `pylink-square`（ST-Link/DAPLink 的 `rtt-send` 走 OpenOCD，不用额外库）；其余命令只用标准库。

**退出码约定（脚本化/agent 依赖它）**：命令返回 None = 成功(0)；返回整数 = 该退出码；异常 = 1。
**失败一定非零**：verify 不一致、flash 失败或刷后校验不一致、缺 ELF/hex、doctor 缺必需工具、
rtt 没抓到数据、--check-seq 检出丢帧、build-verify 断言未满足、cleanup --all 缺 --force、
识别不出芯片型号。--json 里的 ok 字段与退出码一致。

命令：`probe` `setup` `new` `preflight` `doctor` `read` `write` `break` `continue` `step` `info` `attach` `start` `stop` `svd`
`flash` `verify` `reset` `serial` `rtt` `rtt-send` `blackbox` `init-rtt` `init-fault` `cleanup` `selftest` `build-verify`