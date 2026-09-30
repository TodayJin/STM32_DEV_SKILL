# stm32-dev —— STM32 全流程开发技能

> 基于 **SEGGER J-Link + arm-none-eabi-gdb**（不用 openocd），覆盖
> 「环境自检 → 编译 → 烧录 → 调试 → 发现问题 → 改代码 → 重烧 → 继续验证」
> 的完整闭环，循环直至功能正常才结束。

## 这是什么

一个技能包（skill），把 STM32 板上调试的**流程、方法论、22 个命令、以及 79 条实战踩坑**固化下来。
所有内容都来自真实板级调试，不是从手册转抄的。

| 文件 | 内容 |
|---|---|
| `SKILL.md` | 主文件：九步流程 + **七条调试铁律** + 22 个命令用法 |
| `OBSERVE.md` | 观测手段分层（串口探针帧 / RTT 上行 / RTT 下行 / DWT 打点 / 故障现场转储 / 黑匣子） |
| `PITFALLS.md` | **79 条实战坑** + 按主题索引 —— 遇到怪现象先搜这里 |
| `PRACTICES.md` | **工程实践手册**（正向）：上电与初始化时序 / 从站与总线协议实现清单 / 控制·标定·夹持判定 / 状态灯与现场可观测性 / 台架测试分层 / 上位机与 SDK / 工程流程 / 硬件板级 / 多轴差异清单 / 可以直接抄的东西 / 已知缺口 |
| `CHANGELOG.md` | 版本演进 |
| `scripts/stm32-dev.py` | 单文件引擎，22 个命令，**仅标准库**，支持 `--json` |

`PITFALLS.md` 记「怎么被坑」，`PRACTICES.md` 记「怎么做对」—— 两份互补。

## 安装

把整个目录放进技能目录：

```bash
# 类 Unix
git clone <repo-url> ~/.dsh/skills/stm32-dev

# Windows
git clone <repo-url> %USERPROFILE%\.dsh\skills\stm32-dev
```

## 自检

```bash
python scripts/stm32-dev.py selftest
```

## 22 个命令

```
doctor  read   write  break  continue  step  info  attach  start  stop  svd
flash   verify reset  rtt    rtt-send  blackbox  init-rtt  init-fault
cleanup selftest  build-verify
```

## 适用前提

- 已有 **GNU Make / CMake** 工程
- **SEGGER J-Link** + `arm-none-eabi-gdb`
- 不负责 Keil / CubeIDE 工程生成（那属于 `stm32-development-workflow`）

## 版本

**3.14.0**（2026-09-28）—— 新增坑 #55–#79（多轴舵机夹爪：上电 / 标定 / 判定三类静默故障）、
新增附录 `PRACTICES.md`、`SKILL.md` 新增铁律 7「先把安全默认态定下来，再谈功能」。
坑数 54 → 79。

## 内容来源

- **坑 #1–#54**：H7 系列板级调试实战（原作者 EricSun）。
- **坑 #55–#79 + `PRACTICES.md`**：2026-09 一块 **STM32G431 多轴舵机控制板**的实测
  （飞特 FT_S 总线舵机 ×9 + 因时电缸 + 双 RS-485），以及从单指夹爪转接板项目交接文档中
  去重后合并进来的工程经验。

> 正文里的数字均为**实测值**；未独立核实处已在原文标注。

## 许可

内部工程资料，作者 EricSun。
