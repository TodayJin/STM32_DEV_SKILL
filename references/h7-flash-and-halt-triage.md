# 烧录路径与「卡死」三分诊断（H7 实板经验）

实板 bring-up 阶段最耗时间的不是写代码，而是「烧了但不生效」「看着卡死其实没卡」「判成 HardFault 白查」这三类假象。按下面顺序排，每条都能一次定案。

## 1. 烧 BIN，不烧 HEX

- **现象**：烧 `build/x.hex` 报 `O.K.`，`verify` 却报「12 字节不同」；差异处**板上读回 0xFF（擦除态）、ELF 里是 0x00**，重烧、整片 `erase` 后依旧。
- **做法**：改用 bin 路径：
  ```
  si SWD
device <型号>
speed 8000
connect
loadbin <绝对路径>/x.bin 0x08000000
Sleep 200
r
g
Sleep 300
exit
  ```
  实测同一次烧录换 `loadbin` 后 `verify` 立即 OK。
- **判定**：几处连续字节「板上 0xFF / 镜像 0x00」→ 换 bin 路径，**不要**怀疑 flash 坏、芯片坏或链接脚本。
- **注意**：bin 不带地址，必须显式给基地址；换型号/改链接脚本后先核基地址是不是 `0x08…`。

## 2. 烧完/读过之后必须 `r` + `g`

- `loadbin` / `loadfile` / `verify` / `read` / `info` 都会隐式 reset & halt。不显式 `r` + `g`，目标一直冻着，于是：RTT 一条抓不到、全局变量全是垃圾（连 `.noinit` 都是随机值）、`g_magic` 之类标志不对。
- **判据**：标志常量对不上 + 所有变量都像随机数 = **没在跑**，不是固件坏了。
- **纪律**：任何 `read`/`verify`/`info` 之后，再判运行状态之前先 `reset`（或 `monitor go`）一次；RTT 抓包前同理。

## 3. 「卡死」先分三类，别默认 HardFault

按顺序，每条一次定案：

1. **读 `CFSR`(0xE000ED28) / `HFSR`(0xE000ED2C)**：非 0 = 真异常（再看 MMFAR 0xE000ED34 / BFAR 0xE000ED38）；**全 0 = 没触发异常，别往 HardFault 上想**。
2. **读时基 + 栈上局部**：`uwTick`（或自己的 ms 计数）在走、而栈上的 `tickstart` 与它差值早已远超 `Timeout` → 卡在**带超时的 HAL 调用**里反复超时，不是死循环。大块 SPI 传输尤其像卡死：`HAL_SPI_Transmit` 每次等满 100ms，320 行屏幕 = 32 秒。
3. **`g_boot_stage`（`.noinit`，复位不清零）按段写值**：读出停在哪一号 = 死在哪个阶段，比猜日志快；还嫌粗就把标记加进可疑函数内部逐段二分。

现场速记：`g_magic` 对 + `g_boot_stage` 在推进 = 程序在跑；只有 `g_boot_stage` 死死停在某个号 = 那个阶段里面卡住。

## 4. 读寄存器/变量的写法

- 读裸地址优先用 gdb 表达式：`read "*(unsigned int*)0x58020410"`；直接给地址有时只回一段调用栈。
- 一次读多个符号：`read g_magic g_boot_stage uwTick g_tick` → `$1..$N` 按顺序返回。
- 判「外设有没有在动」：SPI 看 `SPI2->SR` 的 TXP/BSY，GPIO 看 `MODER/PUPDR/ODR/IDR`（ODR 看软件写了什么、IDR 看引脚实际电平，两者不一致 = 焊接/外部拉动）。

## 5. 探针速率怎么定（J-Link 类）

用 `scripts/jlink_speed_sweep.sh`：同一段 Flash 在不同 `speed` 下各 `savebin` 一次，比 md5 是否一致 + 记耗时。

- **md5 必须全一致**——不一致的那档就是跑不住的档。
- **看耗时拐点，不看标称上限**：USB 全速（12 Mbit）的探针，实测 4MHz→8MHz 有约 1.4× 提升，**8/12/15/20/24MHz 完全一样**（USB 已经是瓶颈）。所以这类探针固定 **8MHz**：吞吐拉满、时序余量最大。
- 高速度不是「更快」，只是「更没余量」；不要为了标称数字把 SWD 推到长线/排线的临界点。
