# H743 裸机工程：脚手架 → 接 HAL → 跑标称时钟 → 核心板上验证

适用：用本技能 `new` 在空目录搭 STM32H743 工程，然后接 Cube HAL、跑到 480MHz，并在只有 SWD 的核心板上
证明“真的在跑”。前提：`probe use jlink|stlink|daplink` 已定，`make flash` 的刷后校验必须过。

## 1. `new` 在 H743 上必须显式给内存参数

```bash
stm32-dev.py new --dir . --device STM32H743VIT6 --flash 2048K --ram 128K --probe jlink
```

H743 只有 Flash 能从型号编码推出（2048K）；**RAM 必须给**（不给会直接报错退出）。128K = **DTCM(0x20000000)**，
脚手架把 .data/.bss/桟放这里。要用 AXI SRAM(0x24000000, 512K) / SRAM1-3 / SRAM4(0x38000000, 64K)（DMA 缓冲、
大缓冲），自己在 `linker.ld` 里加 region。搭完先 `make` 确认出 `.elf`，再 `make flash`。

## 2. 脚手架是 CMSIS-only —— 要用外设就接 HAL

`new` 生成的 main.c 只有内核级代码；一旦需要 I2C/SPI/UART/CAN/ADC/ETH/USB，直接用 Cube 包里的 HAL，
不要手写寄存器层：

- 复制配置头：`cp <Cube>/Drivers/STM32H7xx_HAL_Driver/Inc/stm32h7xx_hal_conf_template.h ./stm32h7xx_hal_conf.h`，
  并按板子改 `HSE_VALUE`（模板默认 25MHz，与本板晶振不同就是错的）。
- Makefile 四改：`DEFS` 加 `-DUSE_HAL_DRIVER`；新增 `HAL_INC`/`HAL_SRC` 指向 Cube 包（引用源文件，不复制）；
  `CFLAGS` 加 `-I$(HAL_INC)` 与 `-MMD -MP`，文件末尾 `-include $(OBJS:.o=.d)`；HAL 对象用模式规则直接引包：
  `$(BUILD)/hal_%.o: $(HAL_SRC)/stm32h7xx_hal_%.c`（`stm32h7xx_hal.c` 本身单列一条）。
- **HAL 模块按需加**：hal / hal_cortex / hal_dma / hal_gpio / hal_pwr / hal_pwr_ex / hal_rcc / hal_rcc_ex 是底座，
  其余用到再加（IWDG→hal_iwdg、I2C→hal_i2c …）。缺模块的表现是**链接期 `undefined reference to HAL_xxx`**，
  按报错把对应的 `stm32h7xx_hal_xxx.c` 加进 `HAL_OBJS`。
- 本项目源码用 `$(BUILD)/%.o: %.c` 并**保留源码子目录**（`bsp/bsp.c` → `build/bsp/bsp.o`），
  配方首行 `-mkdir -p "$(dir $@)"`。⚠️ 本机 make 走 sh：写成 cmd 风格的反斜杠路径（`mkdir "build\bsp"`）
  会建出名字带反斜杠的垃圾目录，编译随即因无法写 `.d` 文件失败。

## 3. 时钟树：当成“单独一个改动”做（H743 → 480MHz）

480MHz 的前提与**顺序**（顺序错会停在无时钟状态）：

1. `HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY)`（H743 是内部 LDO，不是 SMPS）；
2. `__HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE0)` 并等 `PWR_FLAG_VOSRDY`；
3. 配 PLL1 → `HAL_RCC_ClockConfig(..., FLASH_LATENCY_4)`；
4. `SystemCoreClockUpdate()`，把实测值存全局变量供主机读。

PLL1 取值（VCO 必须 192~960MHz；PLL 输入落 4~8MHz 档就用 `RCC_PLL1VCIRANGE_2`）：

- HSI 64MHz：M=16 → 4MHz 参考，N=240 → 960MHz VCO，P=2 → 480MHz；
- HSE 8MHz（板上真有晶振时）：M=1 → 8MHz，N=120 → 960MHz，P=2 → 480MHz。
  APB1/2/3/4 各 /4 = 120MHz。

**PLL 切失败 = 内核没时钟 = 调试口一起没了**：HAL 在切完后会关掉不再作为时钟源的振荡器，所以“PLL 实际
没跑起来”的结局不是报错停住，而是 **SWD 连不上**（J-Link 报 `Can not attach to CPU`，试各种速度、连它
自己那句 connect-under-reset 都失败）。所以：

- **时钟树单独一次改**，不要和看门狗/外设初始化同批烧——混在一起根因无法判定（本次就是两样一起改，
  锁死后只能靠猜）；
- 每个时钟步骤前后各打一条 RTT 标记（改 PLL 前 / 切完 SYSCLK / 读完实测频率），哪条没出现就知道停在哪；
- 核心板 HSE 未知时先用 **HSI 跑 PLL**（HSI 一定可用）；确认没问题再换 HSE 源；
- 真搞成无时钟：**重试 SWD 速度没有用**，需要用户侧的物理动作（按住复位不放的同时下连接命令；
  或把 BOOT0 跳高进 ROM bootloader，那里 SWD 一定能连）→ **尽早开口问用户**，不要一个人在那里调速度阶梯。

## 4. 怎么证明“时钟真的跑对了”（不接任何外部硬件）

- 固件侧：SysTick 里自己数一个 `volatile g_ms`（和 HAL 的 `uwTick` 互查），主循环按 `g_ms` 差值每 500ms
  打一帧 RTT：`seq=… ms=… tick=…`（用差值定节拍，不要用空循环延时）。
- 主机侧：抓 8s，看 `ms` 是否按墙钟走（8s ≈ 8000）——**时钟配错时这个节奏会明显偏**；
  这是“480MHz 真的生效”的动态证据，光看 `SystemCoreClock` 变量只能说明你写了寄存器。
- 启动横幅当成一次性芯片身份证据：UID 寄存器 + `SystemCoreClock` + HAL 的 HCLK/PCLK 读数。
  抓这种一次性输出要在**复位后立刻抓**（必要时显式 `--address <_SEGGER_RTT>`）；主机读不到时注意
  ⚠️ **探针变量要真的被引用**：`-Wl,--gc-sections` 会把没有任何引用的全局整段回收，主机侧读它报
  `Missing ELF symbol` —— 那是链接期被丢掉，不是板子或调试器的问题（给变量一处引用或 `__attribute__((used))`）。

## 5. RTT 抓包“丢帧”先分诊

- 抓包起始常有主机追赶段，会出现一次大跳变：**看 `seq` 与另一个独立自增计数（如 `tick`）是否同步跳** ——
  同步＝固件确实连续发了、主机漏读（抓包侧问题）；不同步才是固件真丢。
- 给上行缓冲留余量（`SEGGER_RTT_Conf.h` 里 `#define BUFFER_SIZE_UP (4096)`）；**改完 .h 必须
  `make clean && make`** —— 脚手架原版 Makefile 没有头文件依赖，`make` 会报成功但一点没重编（`bss` 不变
  就是铁证），改了跟没改一样。
- 验收用 `rtt --elf … --check-seq`，再自己解析一遍日志确认跳变次数与位置。

## 6. 上电与调试的默认态

- 顺序：`HAL_Init()` → `bsp_clock_init()` → **时钟验证通过后**再加看门狗 → `SEGGER_RTT_Init()` → 打横幅。
- 看门狗必须配 `__HAL_DBGMCU_FREEZE_IWDG1()`（否则一进断点就被狗咬复位）；喂狗放主循环。
- 烧完先纯主机侧（RTT）确认在跑，再碰调试器。

## 7. 本用户的推进口径（固件类任务）

先做**能脱离外部硬件验证的逻辑**（时钟/时基/看门狗/故障记录/状态机/换算与保护阈值），
外设 bring-up 等板子回来再做；每一步都要有 RTT 可断言的验收（帧序号、时基、计数器），
而不是“能编译就算完”。

## 8. 没有外设怎么验证应用逻辑：假硬件 + RTT 断言自测

这是“先验逻辑、后验硬件”的具体做法（实测在核心板上一次跑完 50+ 条断言、零硬件接线）：

- **逻辑与寄存器分开**：逻辑模块只依赖一组函数指针（如 `set_en / set_vset_mv / read_sense / now_ms / log`），
  自己不去碰寄存器 —— 同一份逻辑因此旣能跑在真板上，也能在只有核心板时跑。
- **假实现**：采样值由用例写、输出动作（EN/DAC 写入）被记录下来、时间由用例推进（每步 +10ms）
  → 状态机与保护的时间行为可以被确定性地驱动。
- **上电跑一遍全部用例**：每条打 `[PASS]/[FAIL] <一句话>`，末尾给 `PASS=n FAIL=m` 总账，
  并把**失败项数作为函数返回值**（可接 CI / `build-verify --check`）。
- **用例至少覆盖**：换算往返（原始码↔工程值，含量程点、零点、两端钳位）；**上电安全默认**
  （全关、使能脚为关、可设定量取最保守值）；状态机全部迁移（含启动超时、运行中被请求关断）；
  每类保护各一条（过流去抖、过压、欠压、回读失败、启动超时、过温全局锁存）；
  边界拒绍（上下限、运行中禁止一步大幅调整）；“必须显式清故障才能恢复”。
- **协议层再补一类**：CRC 已知向量（CRC16-Modbus 对 `"123456789"` = 0x4B37）、**坏 CRC 丢弃不回包并计数**、
  **分包喂入**（同一帧拆 2+1+3 段仍能解析）、**噪声后自动重同步**、长度字段越界丢弃、每条命令的正常与异常路径
  （非法通道 / 越界 / 状态不允许）—— 串口与 DMA 分片必踩这几条。协议与通道逻辑都走函数指针接口时，
  上层还可以用 RTT 下行（`rtt-send`）当临时传输，把“主机→固件→响应”整条链路在核心板上先端到端跑通。
- **断言失败先分诊“逻辑错”还是“用例错”**：假输出没跟随设定值 → 逻辑正确地报了欠压，是用例错、不是逻辑错。
  改完重跑；不要因为“用例写得不对”就去改逻辑。
- **把上下限/钳位点这类口径值也写成断言输入** —— 这类常量最容易写错（实测：可调下限按标称 5.0V 写，
  而硬件满量程对应实际 5.18V，自测当场报出；这种修正还要回写到设计文档）。
- 自测输出有几 KB，先把 RTT 上行缓冲提到 4KB（见 §5），否则一上电就被后续日志冲掉。
