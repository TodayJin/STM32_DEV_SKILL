# H7 以太网 bring-up：RMII PHY → MAC/DMA → lwIP（裸机）

适用：STM32H7 板子第一次点网口（LAN8720/LAN8742 这类 RMII PHY），或“PHY link 灯亮、PC ping 不通”。
按层往下推，每层都要有一个**能读到数字**的判据，不靠猜。

```
PHY(MDIO) → MAC/DMA → 交换机/对端 → 协议栈(ARP/ICMP/TCP)
```

---

## 1. PHY 层：先确认 PHY 活着、链路是什么速率

用 GPIO 位翻转读 MDIO（不依赖 ETH 外设与它的时钟）：

- MDC 不能超过 2.5MHz：每个相位放几十条空循环（`for(volatile int i=0;i<60;i++)`）。
- 读返回 `(真值<<1)|1` 说明**采样点比 PHY 输出早半拍**：多采一位当最高位、整体右移还原；或者干脆 `v >>= 1`（ID/BMSR/BMCR 的 MSB 本来就是 0 时够用）。
- 期望值（LAN8720）：
  - ID1 = `0x0007`，ID2 = `0xC0F1`
  - BMSR（reg 1）= `0x782D` 表示 **link up + 100M + 自协商完成**（bit2 link、bit13/14 100M、bit5 AN done）
  - BMCR（reg 0）默认 `0x3100`
- BMSR 是 latching 的，**连读两次**才拿到稳定值。

### 1.1 RMII 的 50MHz REF_CLK 来自 PHY，而 PHY 要不要输出它由 strap 决定

- **先对引脚表**（LAN8720A/LAN8720Ai，24-QFN，手册 Table 2-8）：`pin1 = VDD2A`、**`pin2 = LED2/nINTSEL`**、**`pin3 = LED1/REGOFF`**、`pin13 = MDC`、**`pin14 = nINT/REFCLKO`**、`pin15 = nRST`、`pin16 = TXEN`、`pin17 = TXD0`、`pin18 = TXD1`、`pin7 = RXD1/MODE1`、`pin8 = RXD0/MODE0`。**相邻两脚就是两个不同 strap，凭记忆说错一个脚号会让用户去焊一个完全不相干的电阻。**
- `nINT/REFCLKO`（pin14）当 `nINTSEL` strap **拉到 VSS** 时输出 **50MHz REFCLKO**；悬空或上拉 = nINT 中断模式（默认），此时**引脚上没有时钟**。
- `nINTSEL` 是 **pin2（LED2 脚）** 的复用功能：裸片内部有上拉，所以 pin2 必须有**明确的 10k 级下拉到 GND**。这一脚不要靠「串电阻 + LED 到地」来拉低——LED 的正向压降（≈1.8V）让这条路径在采样时拉不低。**判据是 pin2 电压（刚上电瞬间 <0.2V），不是"灯闪不闪"**：REFCLKO 模式下 LED2 照常做 100M 指示，灯闪反证不了任何东西。
- `pin3 = LED1/REGOFF` 是另一个 strap：REGOFF 拉低 = 用片内稳压器给 VDDCR 供电（本类板子几乎都这么接）。往 pin3 上加下拉只影响 REGOFF，**不会让 pin14 出时钟**——把 1k 加在这里是白改一轮。
- **strap 在上电（POR）和 `nRST` 上升沿采样**：只按 J-Link 复位不一定够，稳妥做法是断电重上电。
- 这条链路是静默的：PHY 的 ID/BMSR/link 全部正常（那是 PHY↔对端 PHY 的事），MDIO 也照常可读，但 MAC 没有 TX/RX 时钟 → **一帧都收不到、`HAL_ETH_Transmit` 仍返回 HAL_OK**。

这一层过了，才说明“PHY 供电、晶振、RMII 参考时钟（PHY 的 REFCLKO 送 MCU）、MDIO 走线”都没问题。

---

## 2. MAC/DMA 层：四个必查点（踩过才知道的顺序）

### 2.1 描述符和收发缓冲必须放在 AXI SRAM

- **H7 的 ETH DMA 只能访问 AXI SRAM（0x2400_0000）与部分 SRAM**，**DTCM（0x2000_0000）它访问不到**。
- 症状：`DMACRCR` 的 `SR`（bit0）写 1 后**一秒内自己变回 0**，RX 永远 0 帧；`DMACSR` 出现 `RPS`。
- 做法：链接脚本单开一区，把描述符/缓冲全部收进去：

```ld
MEMORY {
  RAM     (xrw) : ORIGIN = 0x20000000, LENGTH = 128K
  ETH_RAM (xrw) : ORIGIN = 0x24000000, LENGTH = 64K
}
SECTIONS {
  .eth_dma (NOLOAD) : {
    . = ALIGN(32);
    *(.RxDescripSection) *(.TxDescripSection) *(.eth_dma)
    . = ALIGN(32);
  } >ETH_RAM
}
```

给描述符数组与静态缓冲加 `__attribute__((section(".eth_dma"), aligned(32)))`，用 `arm-none-eabi-nm` 确认地址是 `0x2400xxxx`。

- **发送侧同样中招**：lwIP 的 pbuf 在 DTCM，`HAL_ETH_Transmit` 走零拷贝时会把 DTCM 地址写进 TX 描述符 → DMA 读不到。解决：在 `.eth_dma` 里放 2 个 1600B 的静态 TX 缓冲，`pbuf_copy_partial()` 拷进去再发。

### 2.2 `HAL_ETH_Start()` 必须真的被调用

- 例程里它常藏在 `ethernet_link_check_state()` 的“link up”分支里；把那个函数简化成“只 `netif_set_link_up()`”时**最容易把 Start 一起删掉** → 收发全哑。
- 判据（不用猜）：读 `DMACTCR`/`DMACRCR` 的 **bit0（ST / SR）必须都是 1**；`DMACSR` 里出现 `TPS`/`RPS` 就是“停了”。

### 2.3 MSP 初始化是“别人板子的引脚表”

- Cube 例程的 `HAL_ETH_MspInit()` 是按它自己那块板写的（例如 NUCLEO-H7 用 PG11/PG13/PG2、PB13）。照搬到自己板上，**TX_EN / TXD0 常常根本没配成 AF11**。
- 做法：拿网表逐脚核 RMII 九根线（本板：PA1 REF_CLK、PA2 MDIO、PA7 CRS_DV、PC1 MDC、PC4 RXD0、PC5 RXD1、PB11 TX_EN、PB12 TXD0、PB13 TXD1，全部 `GPIO_AF11_ETH`）。
- 例程里通常**已经有**一个强定义的 `HAL_ETH_MspInit`，自己再在别处写一个会 `multiple definition`；正确做法是**改那一个**。

### 2.4 MAC 过滤：ARP 是广播，默认可能被丢

- 现象：PHY link 正常、DMA 已启动，但 ping 一律“无法访问目标主机”（PC 的 ARP 得不到回应）。
- 做法：bring-up 阶段先放宽：

```c
ETH_MACFilterConfigTypeDef fc;
HAL_ETH_GetMACFilterConfig(&heth, &fc);
fc.PromiscuousMode = ENABLE;
fc.BroadcastFilter = ENABLE;
HAL_ETH_SetMACFilterConfig(&heth, &fc);
```

收通之后再收紧成“自己的 MAC + 广播”。

### 2.5 RMII / MII 由 `SYSCFG->PMCR` 选，复位默认是 MII

- 只用 RMII 接线却没写这一位，MAC 就在“MII 的假信号”上等活动：PHY 一切正常（ID、BMSR=0x782D、REF_CLK 都在）、DMA 也启动了，但 **RX 恒 0 帧，连 PHY 内部回环都收不到**。
- 显式写死，别依赖 HAL 内部那句：

```c
__HAL_RCC_SYSCFG_CLK_ENABLE();
MODIFY_REG(SYSCFG->PMCR, SYSCFG_PMCR_EPIS_SEL, SYSCFG_ETH_RMII);   /* EPIS_SEL[23:21] = 0b100 */
```

- **读回时地址别搞错**：`SYSCFG->PMCR` 偏移是 **0x04**，不是 0x00。按猜的裸地址 `0x58000400` 读出来恒 0，会误判“现在是 MII”并白查一轮 —— 诊断统一用 `SYSCFG->PMCR` / `EthHandle.Instance->XXX` 这类结构体成员，让编译器算偏移。

---

## 3. 收发两侧的判据（先分开，再合起来）

### 3.1 先用回环把“谁的锅”分清楚（顺序：MAC 回环 → PHY 回环 → 数 RX 引脚）

1. **MAC 内部回环**（`SET_BIT(ETH->MACCR, ETH_MACCR_LM)`，发一帧后 `CLEAR_BIT`）：**绕开 PHY、绕开 RMII 引脚**。
   - 收到（描述符 `OWN=0` 且 `PL>0`）→ **MAC + DMA 接收没问题**，别再去改 DMA/描述符/缓存配置；
   - 收不到 → 才回去查 2.1~2.5。
2. **PHY 内部回环**（MDIO 写 BMCR `|= 0x4000`，回读 `0x7100` 确认）：测 PHY→MAC 的 RMII 接收段。
   **必须在同一个函数里：开回环 → 当场连发 10 帧 → 立刻轮询描述符**。分两处做（先开回环、过一会儿再采样）会得到"0 帧"的假阴性——那个 300ms 窗口里可能一帧都没发出去。
3. **直接数 RX 三根脚**（PC4/PC5/PA7 临时改 GPIO 输入 + 下拉，采样 200~300ms 数跳变，探完恢复 `GPIO_AF11_ETH`）：
   **采样循环里必须同时在发包**（AF 输入脚的 `IDR` 仍然反映引脚电平，不用改配置就能边发边读），否则测的是"没流量时的静默"。
   - 有跳变 → 信号到了 MCU，问题在 MAC 配置；
   - **恒 0**（且同窗口内确认发出去了 ≥10 帧）→ PHY 没在驱动 RXD0/RXD1/CRS_DV（回环模式下也一样）→ 剩下的只能是这几根线的通断或 PHY 本身；此时**停手**，让用户打 U21 pin7/8/11 ↔ PC5/PC4/PA7 通断、pin16/17/18 ↔ PB11/PB12/PB13 通断。
   - **实测结论：先补焊，再换芯片。** 本类板子出现过"MAC 回环收到 / PHY 回环 0 帧 / RX 三脚恒 0"，用户在 0.5mm 间距 QFN 上把 RMII 六脚补焊后，**同一份固件立刻恢复**（描述符命中 0 → 93）。手工焊的 QFN "看着有锡" 完全可能是虚焊；换 PHY 是最后一步，不是第一步。

> 三步的价值在于每一步都能**单独排除一层**，且结论是二值的。混在一起改配置，永远说不清是代码还是硬件。

### 3.2 软件测 REF_CLK 频率不可靠，别用它下结论

- 把 REF_CLK 脚当 GPIO 数 `IDR` 跳变的办法**会读到噪声**（悬空/高速脚都能数出几千次），既不能证明有 50MHz，也不能证明没有。
- 行级计数（TIM 外部时钟模式）与输入捕获两种写法在本类板子上出现过**互相矛盾**的结果（一个 0、一个显示捕获在变）。出现这种矛盾时，**先认定两个探针都不可信**，不要挑其中一个当结论——这一轮排查里正是靠这条挑错，先"确诊"了一个并不存在的 strap 故障，让用户白焊了一颗电阻。
- 结论：**时钟有无不要用软件数**。判断交给 3.1 的回环分层；真要测频率就用逻辑分析仪/示波器。
- ⚠️ `PITFALLS.md` 第 100 条里"用 TIMx 外部时钟模式数 PA1 边沿、100ms 应约 5,000,000"那句**判据不可用**（实测给过 0，而输入捕获同时在变）；以本节为准，遇到矛盾时不要择一采信。

### 3.3 帧进了描述符、HAL 却交不出来

现象：轮询 `RxDesc[i]->DESC3` 有 `OWN=0` 且 `PL>0`（回环/对端确实来帧了），`HAL_ETH_ReadData()` 却恒失败（`app==NULL`），收帧计数一直 0。
这说明**硬件侧已经收到**，问题在 HAL 的描述符簿记（`RxBuildDescIdx/RxBuildDescCnt` + 尾指针）或零拷贝缓冲供应（`HAL_ETH_RxAllocateCallback` 拿不到缓冲就整环停摆）。

兜底：自己走环，完全不经过 HAL 的取帧状态机。

```c
/* 缓冲放 .eth_dma（AXI），每个描述符一条 */
static uint8_t rxbuf[ETH_RX_DESC_CNT][ETH_RX_BUFFER_SIZE] __attribute__((aligned(32), section(".eth_dma")));

void eth_rx_rearm(uint32_t i) {
  ETH_DMADescTypeDef *d = (ETH_DMADescTypeDef *)EthHandle.RxDescList.RxDesc[i];
  d->DESC0 = (uint32_t)rxbuf[i]; d->DESC1 = 0; d->DESC2 = 0; __DMB();
  d->DESC3 = ETH_DMARXNDESCRF_OWN | ETH_DMARXNDESCRF_BUF1V;   /* BUF1V = 0x0100_0000 */
}
void eth_rx_setup(void) {                    /* HAL_ETH_Start() 之后调一次 */
  for (uint32_t i = 0; i < ETH_RX_DESC_CNT; i++) eth_rx_rearm(i);
  __DMB();
  ETH->DMACRDTPR = (uint32_t)&DMARxDscrTab[ETH_RX_DESC_CNT - 1];   /* H7 用尾指针, 没有 RCH 环尾位 */
}
```

取帧：判 `DESC3 & OWN` → `len = DESC3 & 0x3FFF` → `pbuf_take` 拷出 → 重挂该条并把 `DMACRDTPR` 指向它。
初始化阶段**绝不能边挂边推 tail**：DMA 会在中途开始取，结果是 desc0 空着、desc1/2 各压一帧，而索引一直停在 `OWN=1` 的那条 —— 看起来像"描述符里明明有帧，却永远读不到"。

- 错误位是**写回格式**的名字：`ETH_DMARXNDESCWBF_CE / _RWT / _OE`（写 `ETH_DMARXNDESCRF_CE` 编译直接报不存在）；`BUF1V` 是 `0x0100_0000`。
- **不要每秒重写 `DMACRCR.SR` 当"强启动"**：会搅乱尾指针与 DMA 状态机、把真实状态盖掉（本轮排查里就把现场弄浑过一次）。
- 这条路**已端到端验证**：ping 8/8、RTT 1~2ms、TCP 回显字节一致、吞吐 366KB/s（≈3Mbps，单缓冲轮询的上限）。用之前先跑 3.1 的回环测试确认帧进得了描述符。
- **同一个工程里只留一条取帧通路**：只要还留着任何一处 `HAL_ETH_ReadData`（哪怕是开机诊断、哪怕是"顺手测一下 PHY 回环"），它就会按自己的零拷贝池重挂描述符、改写 `RxBuildDescIdx`，把自管索引打乱 —— 症状是"回环测试能收到帧、正式收包恒 0"。诊断函数要么删掉，要么改成直接读描述符。

### 3.4 其它判据

- **收**：在 `ethernetif_input()` 里给“进栈的帧”加计数。**PC 主动 ping 时才会涨**，别指望“等一会儿就有广播”。
- **发**：开机后每秒 `etharp_gratuitous(&gnetif)` 发 20 次；在 PC 上 `arp -a` 找板子 IP 对应的 `02-00-00-00-00-01`：
  - 有 → 板子→PC 通，问题只剩收包；
  - 没有 → 板子帧出不去（PHY/RMII/交换机）。
  - 注意：`HAL_ETH_Transmit` 返回 `HAL_OK` **只代表描述符排进队列**，不代表线上真的发出去了——必须用 `arp -a` 或回环来验。
- **描述符现场**：RX 描述符 `DESC3` 的 `OWN`（bit31）与 `BUF1V`（**0x0100_0000**，不是 0x4000_0000）都要为 1；`DESC0` 应指向 AXI SRAM。
- **RX 帧长度**在 `heth.RxDescList.RxDataLength`（HAL v1.13 的 `HAL_ETH_ReadData(&heth,&app)` 只回指针，不回长度）。

---

## 4. lwIP 移植（NO_SYS=1，raw API）

Cube 包里自带完整模板：`Projects/<board>/Applications/LwIP/LwIP_TCP_Echo_Server`（NO_SYS=1、raw API、不带 RTOS），照它抄最快。

- 只编 `src/core`、`src/core/ipv4`、`src/netif`；**include 必须加 `Middlewares/Third_Party/LwIP/system`**（里面有 `arch/cc.h`），否则一上来就 `fatal error: arch/cc.h: No such file`。
- 初始化顺序：`lwip_init()` → `netif_add(&gnetif,&ip,&mask,&gw,NULL,ethernetif_init,ethernet_input)` → `netif_set_default` → `netif_set_up`。
- 主循环每轮：`ethernetif_input(&gnetif)` + `sys_check_timeouts()`。**轮询绝不能挂在 500ms 的 UI/上报节拍里**——实测往返延迟就等于 500ms、吞吐 1.5KB/s；挪到每圈后 RTT 1~2ms、366KB/s。刷屏这类慢操作（软件 SPI 整屏 2.7s）必须与 poll 解耦，否则刷屏期间网络完全停摆。
- **HAL v1.13 的 ETH 接口**：
  - 收：`HAL_ETH_ReadData(&heth, &app)` + 实现 `HAL_ETH_RxAllocateCallback(uint8_t **buff)` 提供缓冲；
  - 发：`ETH_BufferTypeDef` + `ETH_TxPacketConfigTypeDef`（`Attributes=ETH_TX_PACKETS_FEATURES_CRCPAD`、`TxBuffer`）+ `HAL_ETH_Transmit(&heth,&cfg,timeout)`；
  - 老教程里的 `HAL_ETH_TransmitFrame` / `GetRxDataBuffer` 在这个版本里**不存在**，按头文件改（同 #92）。
- **裸机下“零拷贝 pbuf 池”容易拿不到缓冲**（`BUF1V` 不置位 / 0 帧）：改成“静态缓冲 + `pbuf_alloc`/`pbuf_take` 拷贝式接收”，稳定性优先。
- 没 DHCP 的现场用**静态 IP**：PC 插上没拿到地址时是 **169.254.x.x/16（APIPA）**，板子配同网段（如 `169.254.208.196/16`）就能直接 ping。

---

## 5. 现场/网络侧：别把别人的问题算到板子上

- **Windows 防火墙默认拦入站 ICMP**：PC↔PC ping 不通**不能**证明交换机坏。判二层看 `arp -a` 有没有学到对端 MAC（要测 ping 就在目标机以管理员加放行规则：`netsh advfirewall firewall add rule name="ICMP" protocol=icmpv4:8,any dir=in action=allow`）。
- **看懂 Windows ping 的回显**：`来自 <本机IP> 的回复: 无法访问目标主机` 是**本机自己的 ARP 失败**，统计里的“已接收 N”是这些本地错误回显，**不是板子的回包**。真回包长这样：`来自 <板子IP> 的回复: 字节=32 时间<1ms TTL=...`。用这个区分“没通”与“通了”。
- **三种失败回显对应三种完全不同的问题，先分清再动手**：
  - `来自 <本机IP> 的回复: 无法访问目标主机` → 同网段、本机 ARP 解析失败，包没出去；
  - `请求超时` → 包发出去了，没人回（对端没收到或没应答）；
  - `PING: 传输失败。常见故障` / `Network is unreachable` → **本机根本没有到该网段的路由，连 ARP 请求都没发**。
- **`arp -a` 里有板子的 MAC 只能证明板子→PC 方向通**：那是板子发免费 ARP 时学会的，即使本机没有到它的路由也会留下条目。要证明“能通信”必须实际打通一次（ping 回显来自板子 IP，或 TCP 端口连上）。
- **接板子那块网卡的地址先对一遍**：它常常不是 169.254/16（接上办公网时 DHCP 会拿到 192.168.x/24，此时 ping 报“网络不可达”）。测之前给它加一个同网段副地址（**要管理员**，agent 的终端没有提升权限，这条命令交给用户跑）：
  ```
  netsh interface ipv4 add address name="以太网" address=169.254.208.195 mask=255.255.0.0
  ```
  用 `/dev/tcp/<ip>/<port>` 或 `curl` 验证时同样受这条路由约束——没路由时报的是 `Network is unreachable`，不是板子的问题。
- 交换机是独立芯片时：先确认“两台 PC 经这块板交换机能 ARP 互见”，再查 MCU 那个口。
- `ipconfig` 里 `192.168.x` 常常是 WLAN 网卡，**真正插板子的那块网卡**才是要看的那个（APIPA 段）。

---

## 6. 一条命令级别的排查序列

1. `arm-none-eabi-nm build/firmware.elf | grep -E "DMARxDscrTab|s_rx_pool"` → 地址必须是 `0x2400xxxx`。
2. 调试器读 `DMACTCR`/`DMACRCR` bit0 → 必须为 1；为 0 先查 `HAL_ETH_Start()` 有没有被调用。
3. 读 `MACPFR`（本板实测 bring-up 时 `0x618`）与 `RxDescList.RxDataLength`。
4. `g_eth_rx_frames` 在 PC ping 后是否增长：不涨 = 收不到；涨但 ping 不通 = 回包/校验。
5. 免费 ARP + PC `arp -a` 判发送方向。
6. **不要用软件数 REF_CLK 的边沿来判定时钟**（见 3.2）：GPIO 采样会读到噪声，行级计数与输入捕获还可能互相矛盾——两个探针打架时两个都不算证据。
7. **MAC 内部回环**（`MACCR.LM`）先确认 MAC+DMA 收得对；再用 **PHY 内部回环**（BMCR `|= 0x4000`，回读 `0x7100`，**开回环与发帧放在同一函数**）确认 PHY→MAC；最后**把 PC4/PC5/PA7 当 GPIO 数跳变（采样期间持续发包）**确认 PHY 到底有没有驱动接收线。
8. 到第 7 步还没通就**停手**：上逻辑分析仪看 RXD0/RXD1/CRS_DV 有没有波形，再配合万用表打“PHY 侧 → MCU 侧”通断。**这一步之后不要再改固件** —— 继续调配置只会把已经验证过的部分弄坏，且每一轮都要重新回答所有问题。

> 分层原则：**“配置对不对”靠寄存器，“线通不通”靠波形/通断**。两类证据混在一起调，结论永远是浑的。
