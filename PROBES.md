# PROBES.md — 三种调试器：能力、坑、最强的用法

> 技能插上哪个用哪个（`probe detect` 自动认），也能用 `probe use` 写进工程固定下来。
> 本文回答三件事：**能做什么、哪里会翻车、怎么把它的本事用满**。

## 0. 一句话选型

| 你的情况 | 选谁 |
|---|---|
| 调 STM32、手头就有或板子自带 | **ST-Link V3**（ST 官方工具链的亲儿子：烧录/校验/救援/选项字节最省事） |
| 要 **AI 自己高频读实时日志**（RTT 双向、MB/s 级） | **J-Link**（这是它唯一决定性的优势） |
| 几十块钱、开源、不限于 ST 芯片 | **DAPLink / CMSIS-DAP**（够烧够调，高级功能少） |
| 两个都想要 | 双持：ST-Link 管烧录/救援/选项字节，J-Link 管 RTT 实时观测；驱动能共存，但同一台探针同一时刻只能被一个程序占 |

## 1. 能力总览

| 能力 | J-Link | ST-Link V3 | DAPLink |
|---|---|---|---|
| 烧录通道 | JLink.exe（靠文本 O.K. 判定） | STM32_Programmer_CLI -w（**官方返回码**） | openocd program |
| 烧完逐字节校验 | 脚本读回 + 本地比对 | -v 自校 或 -u 读回 + 本地比对 | openocd program ... verify |
| 断点/单步/读变量 | GDB + JLinkGDBServerCL | GDB + OpenOCD | GDB + OpenOCD |
| RTT 上行 | **原生，MB/s 级** | OpenOCD `rtt server`（轮询读 RAM，慢 1~2 个数量级，但能用） | 同 ST-Link |
| RTT 下行（回灌） | 有（`JLinkRTTClient` 默认 19021，或 `rtt-send` 走 pylink） | 有（OpenOCD `rtt server` **是双向的**：往 socket 写 = 写目标下行缓冲；固件照旧轮询 HasKey/GetKey） | 同 ST-Link |
| SWO / ITM | 完整 | 有（V3SET 引出了 SWO；OpenOCD 要手配 swo/tpiu） | v2 才有 SWO 端点，小板多没引出 |
| 虚拟串口（VCP） | 无 | **有**（一根 USB 同时给 SWD + 串口） | 部分有 |
| 选项字节 / 读保护 | 要自己拼脚本 | **-ob / -rdu 一条命令** | 无官方支持 |
| 救砖（复位下连接 / 解锁） | 一般 | **mode=UR + -rdu 最强** | 弱 |
| 官方 HardFault 分析 | 无 | **-hf** | 无 |
| 半主机 semihosting | 有 | 有（ST-LINK_gdbserver --semihosting） | 有 |
| 多探针选号 | -select USB=<S/N> | sn=<S/N> | adapter serial <S/N> |
| SWD 时钟上限 | 高（10+ MHz） | V3：8 MHz SWD | 看具体实现 |

## 2. J-Link

**能用的工具**：JLink.exe（Commander 脚本）、JLinkGDBServerCL（调试服务）、JLinkRTTLogger（RTT 抓包）、
JLinkRTTClient（RTT 回灌）、JLinkRemoteServer、J-Flash。

**最强用法**
- RTT：`stm32-dev.py rtt --elf build/xxx.elf --check-seq` —— MB/s 级、能判丢帧，是"AI 自己看实时日志"的唯一正解。
- 多板同时插：所有命令加 `--serial <S/N>` 锁死某一台，避免"串板"。

**注意事项（都是踩出来的）**
- 烧录脚本里 `r` 与 `g` 之间必须有 Sleep（坑#80），否则"烧完板子像死了"。
- `detach` ≠ 恢复运行（坑#16）：技能默认读完发 `monitor go`。
- JLinkRTTLogger 与 JLinkGDBServerCL 抢同一台探针：**抓 RTT 前先 `stop`**（技能已自动处理）。
- J-Link 会顺手拉起 JLinkGUIServer（弹窗/残留）：`cleanup` 会清。
- 与 OpenOCD 的 libusb 驱动可能打架（同一台机器两套驱动并存时）。
- EDU 版禁商用：商业产品要用 Base/Plus 级别。

**没有它时**：RTT 实时性降级 → 用串口探针帧（`serial`）或 OpenOCD RTT；其余能力别的探针都有替代。

## 3. ST-Link V3（V3SET / V3MINI / V3MINIE）

**能用的工具**：STM32_Programmer_CLI（烧录/校验/读回/选项字节/救砖/-hf）、ST-LINK_gdbserver（GDB 服务，
带 SWO 时钟分频与 semihosting）、STM32CubeProgrammer 图形界面（**技能不用它：GUI 拿不到返回码**）、
OpenOCD（技能用它当 GDB 服务与 RTT 通道）。

**最强用法**
- 烧录校验一条命令：`STM32_Programmer_CLI -c "port=SWD freq=8000" -w <hex> -v -rst`（有返回码，好判）。
- 救砖：`mode=UR`（复位下连接）+ `-rdu`（解读保护，会全片擦除）。
- 现场诊断：`-hf` 官方 HardFault 分析、`-regdump`、`-pwr`（看型号支持）。
- 自带虚拟串口：一根 USB 同时给 SWD 和串口，少一根线就少一类"收不到数据"的坑 —— 直接配 `serial --port <那个 COM>`。
- 抓 RTT 日志：`rtt --elf build/x.elf --check-seq` —— 技能自动起 OpenOCD 的 `rtt server`（本地 TCP）并收干净，地址解析与丢帧判定和 J-Link 一个用法。
- 回灌 RTT 下行：`rtt-send "x" --expect pong`（要发的文本是位置参数）—— OpenOCD 的 `rtt server` 双向，不用 pylink；固件里仍要轮询 `SEGGER_RTT_HasKey()`。
- 取 RTT 源码：`init-rtt --dir .` —— J-Link 安装目录没有就自动从 SEGGER 官方仓库（BSD）取 6 个文件。

**注意事项**
- 机器上同时装了两个 `stlinkserver`（CubeCLT 里的 + 单独装的）会抢设备，只留一个。
- 官方 CLI、ST-LINK_gdbserver、OpenOCD 不能同时占同一台 ST-Link（技能里 `cleanup` 负责清场）。
- 克隆 ST-Link 会被官方工具拒：`not a genuine ST device`。
- OpenOCD 走 hla 驱动：单 AP 限制 → SWO 要手配 `swo create / tpiu create`；RTT 是轮询内存，吞吐低。
- OpenOCD 的 `rtt` 命令**必须先加载 target 配置**才存在（只给 interface 脚本时它报未知命令）；技能拼命令行时已经按这个顺序。
- **别给 OpenOCD 传 `telnet_port/tcl_port`**：0.11 用下划线、0.12 用空格，传哪个都报 DEPRECATED（技能里已经不传）。
- RTT 要固件里集成 SEGGER RTT 源码（`init-rtt` 能把源码放进去）。
- V3MINI/V3MINIE 有没有引出 SWO 引脚，要以你的板子丝印为准（V3SET 有）。

**没有它时**：J-Link 的 RTT 更快、跨厂商更强；DAPLink 更便宜但要自己配 OpenOCD 与驱动。

## 4. DAPLink / CMSIS-DAP

**能用的工具**：OpenOCD（技能默认用它）、pyOCD（可选：`pyocd list --probes` 列探针、`pyocd flash`）、各类 IDE。

**最强用法**
- `openocd -f interface/cmsis-dap.cfg -f target/stm32g4x.cfg -c "program <elf> verify reset exit"`（技能已封装成 `flash`）。
- 多探针：`-c "adapter serial <S/N>"`。

**注意事项**
- STM32_Programmer_CLI **不认 DAPLink**（别指望 ST 官方工具）。
- 板子没引出 SWO 就别指望 ITM；RTT 才只要 4 根 SWD。
- DAPLink 自己的固件太旧会连不上/速度慢：按厂商方法升级固件。
- 便宜的国产 DAPLink 常见限制：只有 4 线、无 VCP、无复位线。
- Windows 上要 WinUSB 驱动（Zadig 或厂商驱动），装错了 openocd 会报 `no device found`。

**没有它时**：不影响技能（自动走别的探针）。

## 5. 换探针时：每条命令走哪条路

| 命令 | J-Link | ST-Link | DAPLink |
|---|---|---|---|
| flash | JLink.exe Commander 脚本 | STM32_Programmer_CLI -w -v -rst | openocd program verify |
| verify | J-Link 读回 + 逐字节比 | CLI -u 读回 + 逐字节比 | 暂缺（flash 自带 verify） |
| reset | Commander 脚本 r/Sleep/g | CLI -rst | openocd reset run |
| read / write / info / break / continue / step / attach / blackbox | JLinkGDBServerCL + gdb | OpenOCD + gdb | OpenOCD + gdb |
| rtt | JLinkRTTLogger（原生，MB/s） | OpenOCD `rtt setup/start` + `rtt server`（本地 TCP，`--rtt-port`） | 同 ST-Link |
| rtt-send | pylink（J-Link DLL） | OpenOCD `rtt server`（TCP 双向，不用额外库） | 同 ST-Link |
| probe / setup / new / serial / doctor / preflight / svd / selftest | 与探针无关 | 同 | 同 |

## 6. 报错速查

| 现象 | 真实原因 | 怎么办 |
|---|---|---|
| `No debug probe detected` | 官方 CLI 没看到 ST-Link | 查 USB、驱动、有没有别的程序占着 |
| `Error: open failed` | openocd 打不开探针 | 探针没插/被占/驱动不对；先 `cleanup` |
| `unable to find a matching CMSIS-DAP device` | DAPLink 没被认出来 | 换 USB 口、装 WinUSB 驱动、换线 |
| `not a genuine ST device` | 克隆 ST-Link | 换正品，或用 DAPLink/J-Link 走 OpenOCD |
| 命令卡住不动 | 残留 GDB server / RTT logger 占着探针 | 先 `cleanup`，再谈别的 |
| 读回的变量值明显是旧的 | 板子上跑的是旧固件（坑#9） | `verify` 比一下，别急着查代码 |