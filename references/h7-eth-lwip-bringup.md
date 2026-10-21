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

- **收**：在 `ethernetif_input()` 里给“进栈的帧”加计数。**PC 主动 ping 时才会涨**，别指望“等一会儿就有广播”。
- **发**：开机后每秒 `etharp_gratuitous(&gnetif)` 发 20 次；在 PC 上 `arp -a` 找板子 IP 对应的 `02-00-00-00-00-01`：
  - 有 → 板子→PC 通，问题只剩收包；
  - 没有 → 板子帧出不去（PHY/RMII/交换机）。
- **描述符现场**：RX 描述符 `DESC3` 的 `OWN`（bit31）与 `BUF1V`（**0x0100_0000**，不是 0x4000_0000）都要为 1；`DESC0` 应指向 AXI SRAM。
- **RX 帧长度**在 `heth.RxDescList.RxDataLength`（HAL v1.13 的 `HAL_ETH_ReadData(&heth,&app)` 只回指针，不回长度）。

---

## 4. lwIP 移植（NO_SYS=1，raw API）

Cube 包里自带完整模板：`Projects/<board>/Applications/LwIP/LwIP_TCP_Echo_Server`（NO_SYS=1、raw API、不带 RTOS），照它抄最快。

- 只编 `src/core`、`src/core/ipv4`、`src/netif`；**include 必须加 `Middlewares/Third_Party/LwIP/system`**（里面有 `arch/cc.h`），否则一上来就 `fatal error: arch/cc.h: No such file`。
- 初始化顺序：`lwip_init()` → `netif_add(&gnetif,&ip,&mask,&gw,NULL,ethernetif_init,ethernet_input)` → `netif_set_default` → `netif_set_up`。
- 主循环每轮：`ethernetif_input(&gnetif)` + `sys_check_timeouts()`。
- **HAL v1.13 的 ETH 接口**：
  - 收：`HAL_ETH_ReadData(&heth, &app)` + 实现 `HAL_ETH_RxAllocateCallback(uint8_t **buff)` 提供缓冲；
  - 发：`ETH_BufferTypeDef` + `ETH_TxPacketConfigTypeDef`（`Attributes=ETH_TX_PACKETS_FEATURES_CRCPAD`、`TxBuffer`）+ `HAL_ETH_Transmit(&heth,&cfg,timeout)`；
  - 老教程里的 `HAL_ETH_TransmitFrame` / `GetRxDataBuffer` 在这个版本里**不存在**，按头文件改（同 #92）。
- **裸机下“零拷贝 pbuf 池”容易拿不到缓冲**（`BUF1V` 不置位 / 0 帧）：改成“静态缓冲 + `pbuf_alloc`/`pbuf_take` 拷贝式接收”，稳定性优先。
- 没 DHCP 的现场用**静态 IP**：PC 插上没拿到地址时是 **169.254.x.x/16（APIPA）**，板子配同网段（如 `169.254.208.196/16`）就能直接 ping。

---

## 5. 现场/网络侧：别把别人的问题算到板子上

- **Windows 防火墙默认拦入站 ICMP**：PC↔PC ping 不通**不能**证明交换机坏。判二层看 `arp -a` 有没有学到对端 MAC。
- 交换机是独立芯片时：先确认“两台 PC 经这块板交换机能 ARP 互见”，再查 MCU 那个口。
- `ipconfig` 里 `192.168.x` 常常是 WLAN 网卡，**真正插板子的那块网卡**才是要看的那个（APIPA 段）。

---

## 6. 一条命令级别的排查序列

1. `arm-none-eabi-nm build/firmware.elf | grep -E "DMARxDscrTab|s_rx_pool"` → 地址必须是 `0x2400xxxx`。
2. 调试器读 `DMACTCR`/`DMACRCR` bit0 → 必须为 1；为 0 先查 `HAL_ETH_Start()` 有没有被调用。
3. 读 `MACPFR`（本板实测 bring-up 时 `0x618`）与 `RxDescList.RxDataLength`。
4. `g_eth_rx_frames` 在 PC ping 后是否增长：不涨 = 收不到；涨但 ping 不通 = 回包/校验。
5. 免费 ARP + PC `arp -a` 判发送方向。
6. **REF_CLK 在不在**（不用示波器）：把 PHY 送 MCU 的那根参考时钟脚临时配成 GPIO 输入，紧循环里数 `IDR` 跳变，5 ms 内**几千次以上**就说明时钟在（50MHz 会严重混叠，只要远大于 0 即可）；探完**必须恢复成 `GPIO_AF11_ETH`**，否则后面 ETH 也没时钟。
7. **PHY 内部回环**绕开交换机与网线：MDIO 写 BMCR `|= 0x4000`，回读应为 `0x7100`（确认写进去），再让 MAC 发一帧（`etharp_gratuitous` 就够）。
   - 收到 → MAC↔PHY 的 RX 通路正常，问题在交换机/线缆/对端口；
   - 仍收不到 → 问题在 **RXD0 / RXD1 / CRS_DV 三根接收线**或 PHY 自身。
8. 到第 7 步还没通就**停手**：上逻辑分析仪看 RXD0/RXD1/CRS_DV 有没有波形，再配合万用表打“PHY 侧 → MCU 侧”通断。**这一步之后不要再改固件** —— 继续调配置只会把已经验证过的部分弄坏，且每一轮都要重新回答所有问题。

> 分层原则：**“配置对不对”靠寄存器，“线通不通”靠波形/通断**。两类证据混在一起调，结论永远是浑的。
