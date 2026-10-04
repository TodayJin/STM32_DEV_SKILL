# stm32-dev —— STM32 全流程开发技能

> **探针无关**：J-Link / ST-Link(V2·V3) / DAPLink(CMSIS-DAP) 插哪个用哪个，技能自己认、
> 自己选最合适的工具链，覆盖「环境自检 → 编译 → 烧录 → 调试 → 发现问题 → 改代码 →
> 重烧 → 继续验证」的完整闭环，循环直至功能正常才结束。

## 这是什么

一个技能包（skill），把 STM32 板上调试的**流程、方法论、27 个命令、以及 89 条实战踩坑**固化下来。
所有内容都来自真实板级调试，不是从手册转抄的。

| 文件 | 内容 |
|---|---|
| `SKILL.md` | 主文件：⓿ 开工前对照检查 + 九步流程 + **七条调试铁律** + 命令用法 |
| `PROBES.md` | **调试器手册**：三种探针的能力矩阵 / 各自最强用法 / 注意事项 / 没有它时怎么替代 |
| `SETUP.md` | **装环境手册**：按调试器看要装什么、怎么验证、本机现状、环境变量 |
| `OBSERVE.md` | 观测手段分层（串口探针帧 / RTT 上行 / RTT 下行 / DWT 打点 / 故障现场转储 / 黑匣子） |
| `PITFALLS.md` | **89 条实战坑** + 按主题索引 —— 遇到怪现象先搜这里 |
| `PRACTICES.md` | **工程实践手册**（正向）：上电与初始化时序 / 从站与总线协议 / 控制·标定·夹持判定 / 状态灯与现场可观测性 / 台架测试分层 / 上位机与 SDK / 工程流程 / 硬件板级 / 多轴差异 / 可以直接抄的东西 / 已知缺口 |
| `CHANGELOG.md` | 版本演进 |
| `scripts/stm32-dev.py` | 单文件引擎，27 个命令，**仅标准库**（只有 `rtt-send` 需可选的 `pylink-square`，串口观测需 `pyserial`），支持 `--json` |

`PITFALLS.md` 记「怎么被坑」，`PRACTICES.md` 记「怎么做对」—— 两份互补。

## 安装

把整个目录放进技能目录：

```bash
# 类 Unix
git clone <repo-url> ~/.dsh/skills/stm32-dev

# Windows
git clone <repo-url> %USERPROFILE%\.dsh\skills\stm32-dev
```

装完先跑一次 `setup`，它会告诉你缺什么（缺的东西加 `--fix` 自动用 pip 补，
系统级工具加 `--install` 才动）：

```bash
python scripts/stm32-dev.py setup
python scripts/stm32-dev.py setup --install      # 缺编译器/openocd 时自动装
```

## 快速上手

```bash
# 1) 插上调试器，让它自己认（顺手告诉你这个探针最强能干什么）
python scripts/stm32-dev.py probe detect
python scripts/stm32-dev.py probe info stlink     # 三种都有一份说明书

# 2) 记住用哪个，写进工程（之后所有命令自动按它选路）
python scripts/stm32-dev.py probe use stlink

# 3) 空目录一键出可编译工程（照 Cube 固件包里的 CMSIS + 启动文件 + 链接脚本）
python scripts/stm32-dev.py new --dir D:\proj\blink --device STM32G431CBT6

# 4) 开工前体检
python scripts/stm32-dev.py doctor --elf build/xxx.elf

# 5) 第一次烧板子先只看不跑（尤其是板上接了电机/执行器）
python scripts/stm32-dev.py flash --dry-run
python scripts/stm32-dev.py flash            # 默认刷完逐字节校验，校验不过退出码 1

# 6) 观测：串口自报帧不占调试器；要 RTT 就按探针自动选通道
python scripts/stm32-dev.py serial --port COM21 --seconds 5 --check-seq
python scripts/stm32-dev.py rtt --elf build/xxx.elf --check-seq
```

## 27 个命令

```
probe    setup   new      serial   preflight doctor  read    write   break   continue
step     info    attach   start    stop      svd     flash   verify  reset   rtt
rtt-send blackbox init-rtt init-fault cleanup  selftest build-verify
```

> **`preflight`** 是**接手任何已有工程的第一步**：拿这份技能里的结论把现有工程扫一遍 ——
> 烧录脚本有没有 `r`/`g` 之间的 `Sleep`、故障处理器有没有现场记录、看门狗有没有调试冻结、
> 观测通道齐不齐，再列 9 条机械扫不出来的人工对照项。**只读**，有 `[!!]` 退出码即 1，可进 CI。

> **`--dry-run`**（`flash` / `reset`）：只打印将要执行的命令，板子一点都不碰。
> 第一次面对接了执行器的板子、或想确认技能给你选了哪条烧录路时，先跑这个。

## 适用前提

- 已有 **GNU Make / CMake** 工程（没有就用 `new` 生成一个）
- 手上有 **SEGGER J-Link** / **ST-Link(V2·V3)** / **DAPLink(CMSIS-DAP)** 其中一种 + `arm-none-eabi-gdb`
- 不负责 Keil / CubeIDE 工程生成（那属于 `stm32-development-workflow`）

## 版本

**3.17.0**（2026-10-11）—— **调试器通用化**：J-Link / ST-Link(V3) / DAPLink 插哪个用哪个。
新增 `probe`（认探针 + 能力矩阵 + 注意事项）、`setup`（按探针体检，`--fix` 用 pip 补、
`--install` 装系统工具，并主动列出这个探针还能干什么）、`new`（空目录一键出可编译 Makefile 工程）、
`serial`（串口自报帧，不占调试器）；烧录/复位按探针自动选路（J-Link 走 Commander、
ST-Link 走 `STM32_Programmer_CLI -w -v -rst`、DAPLink 走 OpenOCD `program … verify reset`），
新增 `--dry-run`；RTT 也通用了（ST-Link/DAPLink 走 OpenOCD `rtt setup/start` + `rtt server`，
抓空会自动改用 RAM 搜索）；新增 `PROBES.md`、`SETUP.md`。命令数 23 → 27。

**3.16.0**（2026-10-06）—— 新增 **`preflight` 命令 + ⓿ 开工前对照检查**：把这个技能本身变成
**开工第一步的检查清单**。机械项自动判（烧录脚本 `r`/`g` 之间缺 `Sleep`、故障处理器只有 `while(1)`、
看门狗没 `__HAL_DBGMCU_FREEZE_*`、观测通道缺失），机械扫不出来的列 9 条人工项。
**只读**，有 `[!!]` 退出码即 1（可进 CI）。命令数 22 → 23，坑数仍 89。

**3.15.0**（2026-10-05）—— 新增坑 #80–#89（三条**把假象当根因**的静默故障：J-Link 脚本
`r`+`g` 后立刻 `exit` 把核留 halt → 「烧完板子像死了」；请求队列「发出即出队」→ 写指令静默丢失；
块读丢掉实际字节数 → 读未初始化栈、垃圾被固化进 flash）。坑数 79 → 89。

更早的版本见 `CHANGELOG.md`。

## 内容来源

- **坑 #1–#54**：H7 系列板级调试实战（原作者 EricSun）。
- **坑 #55–#89 + `PRACTICES.md`**：2026-09 ~ 10 一块 **STM32G431 多轴舵机控制板**的实测
  （飞特 FT_S 总线舵机 + 因时电缸 + 双 RS-485，上位机 Modbus RTU 从站），以及从单指夹爪
  转接板项目交接文档中去重后合并进来的工程经验。

> 正文里的数字均为**实测值**；未独立核实处已在原文标注。

## 许可

内部工程资料，作者 EricSun。
