# 板载通信口 bring-up：RS485 / CAN / 以太网（可自环验证）

适用：新板第一次验通信口——手上没有第二个节点、没有 CAN 分析仪、也没有逻辑分析仪。
原则：**先自己两路对打，再谈外部设备**。板上同时有 ≥2 路同类通道时，把两路对接就是最省事的闭环。

---

## 0. 先确认收发器真的在板上、真的上电

隔离收发器（CA-IS3098 / CA-IS3062 / ADM2582 等）常是“BOM 里有、板上没焊”或“隔离侧没电”。
在让用户接线之前先查：

| 测点 | 期望 | 含义 |
|---|---|---|
| VCC（逻辑侧 5V/3.3V） | 额定值 | DC-DC 输入 |
| VCCL / 逻辑基准脚 | 3.3V | I/O 电平基准，缺了 RO 输出乱 |
| VISO（隔离输出） | 5V 或 3.3V | 0V ⇒ 总线侧全死，表现是“RX 悬空噪声 + 发不出去” |
| GNDA ↔ GNDB | 不通（隔离） | 若导通说明隔离被击穿或焊错 |
| 总线 A-B 空闲差分 | ≈0V（有失效保护偏置时 0.2~1V） | 明显偏离先查 TVS/终端电阻 |

**症状先对齐再动手**：两路全 0 收且 RX 脚全是噪声时，八成是隔离侧没电，不是固件。

---

## 1. RS485 两路对打

接线：`A-A`、`B-B`（同一块板的两路，地可不接，长线才需要共地）。

固件要点：
- **DE 与 RE# 并接成单脚控向**时：发前置高，**发完等 `USART_ISR_TC`（移位寄存器空）再拉低**，不是等 `TXE`；随后写 `ICR = USART_ICR_TCCF` 清标志。等 TXE 就放总线 = 尾部字节被自己截断。
- 帧格式带自增序号 + 校验（如 `0xAA seq_lo seq_hi XOR`），收发都统计 `ok/bad`，丢帧与错帧分开计数。
- 双向各跑一遍（1→2、2→1），单向通只说明一半链路。

寄存器判据（收不到时先读这些，别改代码）：

| 现象 | 含义 |
|---|---|
| `ISR` 的 **PE/NF/ORE** 置位、收到 0 帧 | RX 脚悬空或在收噪声 ⇒ 收发器没驱动（隔离侧没电 / 芯片没焊 / DE 极性反） |
| `ISR` 的 **RXNE** 一直不置位、电平恒定 | 总线空闲且无偏置，或 A/B 接反 |
| 能收到但错帧多 | 波特率偏差 / 终端电阻缺失 / 双端都装了 120Ω 再叠加过长线 |

**先分“整板”还是“单路”**：同一块板上另一类总线（例如 CAN）能正常收发，就说明电源、隔离地、MCU 侧都没问题，故障锁在这一路收发器或它的接线，不要再回头怀疑整板供电。

**两路全 0 收时的下一刀：直流驱动测试。** 让发送侧 `DE` 常高、循环发 `0x00`（持续显性）约 10 秒，用万用表直流档量两端子的 1-2 脚：

- 两端都读到 ±1.5~2.5V ⇒ 驱动器与线都通，问题在接收侧（RO/DE 极性、RX 引脚配置）；
- 只有发送端有、对端 0V ⇒ 两根线里断了一根或接错端子；
- 两端都 0V ⇒ 驱动器没工作：量 DE 脚（应 3.3V）与隔离侧 VISO。

同时量接收侧 MCU 的 RX 引脚：显性时≈0V、隐性时≈3.3V；停在 1~2V 抖动 = RO 悬空。

---

## 2. CAN 两路对打

接线：`H-H`、`L-L`（H/L 单端接反会让差分反相，等于整条链路废掉；两端各 120Ω，短距离总阻 60Ω 正常）。

### 2.1 先做内部回环（不接线，五分钟定位）

**在让用户接任何线之前**，把这一路 FDCAN 切成 `FDCAN_MODE_INTERNAL_LOOPBACK`，自发自收 10 帧：
全收到 ⇒ 内核时钟、位定时、过滤器、消息 RAM **都对**，剩下的问题只可能在收发器或接线；
收不到 ⇒ 别去量总线，配置本身就没通。

内部回环里 `CCCR.MON=1` 是**正常的**（HAL 故意置位，让帧在内部折返而不驱动引脚），不要当成“总线监听模式导致发不出去”去改。`TEST.LBCK=1` 也属正常。

**H7 独有的致命默认值：消息 RAM 元素数量。** `FDCAN_InitTypeDef` 里除位定时外还有一组「元素个数」，
一个都不设就是全 0 ⇒ **TX FIFO / RX FIFO 长度都是 0**：

```c
h->Init.StdFiltersNbr       = 2;
h->Init.RxFifo0ElmtsNbr     = 16;   h->Init.RxFifo0ElmtSize = FDCAN_DATA_BYTES_8;
h->Init.RxFifo1ElmtsNbr     = 0;    h->Init.RxFifo1ElmtSize = FDCAN_DATA_BYTES_8;
h->Init.RxBuffersNbr        = 0;    h->Init.RxBufferSize    = FDCAN_DATA_BYTES_8;
h->Init.TxEventsNbr         = 0;
h->Init.TxBuffersNbr        = 0;
h->Init.TxFifoQueueElmtsNbr = 8;    h->Init.TxElmtSize      = FDCAN_DATA_BYTES_8;
```

现象非常有迷惑性：`HAL_FDCAN_Init` 返回 **HAL_OK**、`AddMessageToTxFifoQ` 也可能返回 OK、
`CCCR.INIT=0`（已 Start）、`TEC=REC=0`，但 TXFQS 的自由槽位是 0、`TXBTO` 永远不置位——
**帧既没进队也没上总线**。判据是读 `TXBC`/`RXF0C`/`SIDFC`：`F0S=0`、`NDTB=0`、`LSS=0` 就是元素数为 0。

F1/F4 的 bxCAN 没有这组字段，从 bxCAN 迁到 H7 时最容易漏（`hfdcan.Init` 是全局静态变量，
零初始化看起来“很干净”，正好把 FIFO 长度一起清成 0）。

**H7 的 FDCAN 内核时钟坑**：`RCC_D2CCIP1R.FDCANSEL` 复位默认是 **HSE**。工程只用 HSI（没开 PLL）时 FDCAN 没有时钟，`HAL_FDCAN_AddMessageToTxFifoQ` 照样返回 OK，但**一个字节都发不出去**。要么开 HSE（板上有 8MHz 晶振时最省事：`RCC_HSE_ON` + `FDCANSEL=00`），要么把 `FDCANSEL` 指到 PLL1Q/PLL2Q。

500kbps @ 8MHz HSE：bit time = 2µs = 16 tq ⇒ `Prescaler=1, SyncJumpWidth=1, TimeSeg1=13, TimeSeg2=2`。

寄存器判据（“TX 返回 OK 但对面没收到”）：

| 读什么 | 值 | 含义 |
|---|---|---|
| `FDCAN_TXFQS` (**0xC4**) | **0**（自由槽位 0） | 两种可能：元素数量配成 0（见 2.1），或 FIFO 真满且不排空（无时钟 / 无 ACK） |
| `FDCAN_TXBC` / `RXF0C` / `SIDFC` | `NDTB`、`F0S`、`LSS` 全 0 | 消息 RAM 一个元素都没配 ⇒ FIFO 长度为 0 |
| `FDCAN_ECR` | TEC=0 且上面 FIFO 满 | 同上（**从未尝试发送**）。若接了总线但无人 ACK，TEC 会一路涨到 128+，两者含义完全不同 |
| `FDCAN_PSR.LEC` | 7 | 复位值/无错误变化，不能当作“总线正常”的证据 |
| `FDCAN_CCCR.INIT` | 应为 0 | 1 = 仍停在初始化模式，什么都没发 |

**读这些寄存器前先查 CMSIS 头里的 struct 偏移**，不要凭记忆：FDCAN 的 `0x18` 是 `CCCR`、`0x1C` 是 `NBTP`、
`0x10` 是 `TEST`、`0x40` 是 `ECR`、`0x44` 是 `PSR`、`0x50` 是 `IR`、`0xC4` 是 `TXFQS`、`0xCC` 是 `TXBRP`；
误读一个偏移会得到“位域看着很合理”的假结论（例如把 `TSCV` 当成 `TXFQS` 读出一串 0，就误判成 FIFO 满），
然后照着假结论去改本来正确的配置。

---

## 3. 以太网：先分两层，别一上来写协议栈

板上若是“交换芯片 + PHY”结构（如 RTL8367N 5 口 + LAN8720 RMII）：

1. **交换层（不需要固件）**：PC 插 RJ1、另一台设备插 RJ2，两口应能直接互通；同时看 RJ45 的 link/act 灯、PC 侧协商速率。灯不亮 = PHY 供电/晶振/变压器/网线问题，与 MCU 固件无关。
2. **MCU 口（需要 ETH MAC + 协议栈）**：LAN8720 侧要 RMII 时钟、复位、MDIO 读 ID 通过后才有意义；ping 是最后一层。

先做第 1 层能省掉一整轮固件调试：交换层不通时写协议栈等于在坏地基上盖楼。

---

## 4. 测试结果怎么给用户

- 结果（ok/bad/丢帧/RPM/错误计数）**存全局，主循环心跳里周期性重打**，RTT 随时接上都能读；一次性开机日志经常在主机连上之前就滚掉了。
- 报告只给**数字 + 判据**（`485 1→2: ok=200 bad=0；CAN tx=200 rx=200 lec=0 tec=0`），不要用“应该没问题”。
- 全 0 收时，先给“硬件侧测点表”（VCC/VISO/A-B 差分）让用户量，而不是继续改固件——两路同时全 0 极少是代码问题。
