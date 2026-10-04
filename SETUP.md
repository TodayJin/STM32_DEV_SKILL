# SETUP.md — 工具链怎么装（Windows / Linux 都适用）

> 目标：`probe list` 全绿、`setup` 无缺口、`doctor` 通过。
> 技能只在**真的缺工具**时才需要这份文档 —— 它能自动发现 CubeCLT、OpenOCD、Cube 固件包。

## 0. 最快路径（Windows，一次装齐）

```powershell
# CubeCLT 在 winget 里没有包(2026-10 实测搜不到) -> 去 ST 官网下 STM32CubeCLT 安装器, 或装 CubeMX 时勾上
# （装完自带: 编译器 + make + cmake + ninja + gdb + STM32_Programmer_CLI + ST-LINK_gdbserver + SVD）
winget install xpack-dev-tools.openocd-xpack        # openocd（ST-Link/DAPLink 的 GDB 服务与烧录）
winget install --id Arm.GnuArmEmbeddedToolchain     # 若不想装整个 CubeCLT，只装编译器(gcc+gdb, 实测存在)
python -m pip install pyserial                      # 串口观测（serial 命令）
python -m pip install pyocd                         # 可选：列 DAPLink 探针 / 用 pyOCD 烧录
```

Linux：`apt install openocd gcc-arm-none-eabi make` + ST 官网下 STM32CubeCLT（或 STM32CubeProgrammer 的 CLI 包）。

## 1. 按调试器看需要什么

| 调试器 | 必需 | 可选（能解锁额外能力） |
|---|---|---|
| J-Link | SEGGER J-Link 软件包（含 JLink.exe / JLinkGDBServerCL / JLinkRTTLogger） | pylink-square（`rtt-send` 用） |
| ST-Link | STM32CubeProgrammer CLI（含在 CubeCLT 里）+ OpenOCD | ST-LINK_gdbserver（SWO / semihosting）、ST-Link 官方驱动 |
| DAPLink | OpenOCD + WinUSB 驱动 | pyOCD（列探针 / 备用烧录） |
| 任意 | arm-none-eabi 工具链（gcc/objcopy/readelf/gdb）、make 或 cmake+ninja | Cube 固件包（`new` 生成工程要用） |

## 2. 每一件的装法与验证

### ST 官方一套（推荐，Windows 上一步到位）
装完就有：`arm-none-eabi-gcc/gdb/objcopy/readelf`、`make/cmake/ninja`、
`STM32_Programmer_CLI.exe`、`ST-LINK_gdbserver.exe`、`STMicroelectronics_CMSIS_SVD`
（SVD 目录里是 STM32 全系的寄存器描述文件）。
验证：`STM32_Programmer_CLI --version`、`arm-none-eabi-gcc --version`。

### OpenOCD
- Windows：`winget install xpack-dev-tools.openocd-xpack`（装完是 `openocd.exe`，可能在 `.local\bin` 里有个 `openocd.cmd` 包装）。
- 验证：`openocd --version`（能打出 0.12.0 之类的版本号就算好）。

### pyOCD（可选）
`python -m pip install pyocd` → `pyocd list --probes`。

### Cube 固件包（`new` 空项目脚手架要用）
它提供 **CMSIS 设备头 + ST 官方启动文件 + 示例链接脚本**（技能从里面抄内存参数，比查表可靠）。
- 装法一：STM32CubeMX → Help → Manage embedded software packages → 勾 STM32Cube MCU Package for G4（等）。
- 装法二：ST 官网下 `STM32Cube_FW_G4_Vx.x.x` 解压到 `~/STM32Cube/Repository/`。
- 别的路径：设 `STM32_CUBE_REPO` 环境变量指向"包的上级目录"。

### 驱动（Windows 上 90% 的"认不出探针"都是这个）
- ST-Link：装 ST 官方驱动（CubeProgrammer 会带），别用 Zadig 改成 WinUSB（官方 CLI 要 ST 驱动）。
- DAPLink：要 WinUSB（厂商驱动或 Zadig）。
- J-Link：SEGGER 官方驱动；与 OpenOCD 的 libusb 共存时如果打架，用 `--probe` 明确指定走哪条路。

## 3. 环境变量（都能不设，设了是覆盖自动发现）

| 变量 | 作用 |
|---|---|
| STM32_DEV_PROBE | 强制用一种调试器：jlink / stlink / daplink |
| JLINK_GDB_SERVER / JLINK_RTT_LOGGER / JLINK_GDB_PORT / JLINK_SN | J-Link 工具路径 / GDB 端口 / 序列号 |
| STM32_PROGRAMMER_CLI | STM32_Programmer_CLI 路径 |
| OPENOCD | openocd 路径 |
| STLINK_GDB_SERVER / PYOCD | 可选的 ST-LINK_gdbserver / pyocd 路径 |
| STM32_DEBUG_GDB | gdb 命令（默认自动找 arm-none-eabi-gdb） |
| STM32_CUBE_REPO | Cube 固件包目录 |

## 4. 本机实测现状（2026-10-11，作为"装好长什么样"的样例）

- `C:\ST\STM32CubeCLT_1.22.0`：CubeProgrammer CLI v2.23.0、GNU-tools-for-STM32（gcc 14.3.1）、Make、ST-LINK_gdbserver v2.23.0、CMSIS-SVD 全齐。
- OpenOCD 0.12.0+dev → `C:\Users\<你>\.local\bin\openocd.CMD`（.cmd 包装，技能会自动用 `cmd /c` 起它）。
- Cube 固件包：`~/STM32Cube/Repository/STM32Cube_FW_G4_V1.6.3`、`STM32Cube_FW_H7_V1.13.0`。
- pyserial 3.5 已装（`serial` 可用）；pyOCD 未装（可选）。

## 5. 装完自检（三条命令）

```bash
"$SKILL/scripts/stm32-dev.py" probe list      # 三种调试器：本机哪些工具有、哪些缺（缺的会告诉你装法）
"$SKILL/scripts/stm32-dev.py" setup --fix     # 一键体检：调试器+编译链+芯片资料+工程；能自动补的直接补
"$SKILL/scripts/stm32-dev.py" doctor --elf build/xxx.elf
```