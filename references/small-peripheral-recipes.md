# 小幅外设 bring-up 配方（有源蜂鸣器 / 4 线风扇 / 背光）

适用：板上由 GPIO / TIM / EXTI 直接驱动的小外设。共同点——**先钉住安全态（关），再谈功能**；每步都要有可读回的证据，不靠“听起来像在工作”。

---

## 1. 有源蜂鸣器（GPIO 驱动 NPN）

电路典型：`MCU → 1k → NPN.B`，`NPN.C → 蜂鸣器−`，`蜂鸣器+ → 5V`，`NPN.E → GND`，基极另有 100k 下拉。有源蜂鸣器内部已带振荡，**只要直流电平，不需要 PWM/频率**。

```c
void buzzer_beep(uint16_t ms)   /* 非阻塞: 记 deadline, 到点自己关 */
{
    HAL_GPIO_WritePin(GPIOA, GPIO_PIN_0, GPIO_PIN_SET);
    s_off_tick = HAL_GetTick() + ms;
    s_on = 1;
}
void buzzer_poll(void)          /* 主循环每帧调 */
{
    if (s_on && (int32_t)(HAL_GetTick() - s_off_tick) >= 0) buzzer_off();
}
```

规矩：
- **`buzzer_init()` 里先写低再切成输出**，避免上电瞬间“啪”一声或长鸣。
- 测试只响 **一次 100ms**，并回读确认已关（`bz=1` 表“已关”），别用 `HAL_Delay` 死等——主循环还得跑。
- 用户说“不要一直响，响一下就行”= 非阻塞单次 + 到时必关，而不是加 `while` 等。

---

## 2. 4 线风扇（PWM 调速 + TACH 测速）

接口：`1=PWM(MCU→风扇)`、`2=TACH(风扇→MCU，开漏，需 10k 上拉到 3V3)`、`3=V+`、`4=GND`。

**PWM**：目标 **25kHz**（四线风扇标准，避开可听频段），8 位分辨率 → 计数时钟 = 25k×256 = 6.4MHz。

```c
uint32_t pclk = HAL_RCC_GetPCLK1Freq();
if ((RCC->D2CFGR & RCC_D2CFGR_D2PPRE1) != RCC_D2CFGR_D2PPRE1_DIV1) pclk *= 2u;  /* 定时器时钟 = 2×PCLK1 */
prescaler = pclk / 6400000u;      /* ARR = 255 */
```

- `PSC` **按运行时钟算，不要写死**：同一份代码换了主频就变成别的频率，风扇啸叫/不转都从这里来。
- 通道启动后先把占空比设为 0 并停转，再由上层给值。
- **`0%` 要显式把 CCR 写 0 并确认停转**：占空比 0 但计数器仍在跑时，某些配置会漏出窄脉冲。

**TACH**：GPIO 输入 + 上拉 + 上升沿 EXTI 计数，不用输入捕获也能算转速。

```c
rpm = (pulses_in_window × 60000) / (pulses_per_rev × window_ms);   /* 多数 5V 风扇 2 脉冲/转 */
```

- 先按 **2 脉冲/转** 算，把**原始脉冲数一起打出来**——这样换算系数不对时能从数据反推，而不是只看到一个可疑 RPM。
- 验收看**单调性 + 0% 归零**，不是看单个数字：实测一例 `0%:0 / 40%:1950 / 70%:3780 / 100%:5460 rpm`，四档单调、0 档真停，就说明 PWM 与测速两条路都通。
- 换相噪声会串进模拟地：风扇与蜂鸣器的回流必须走 `GND`，不能落在 `AGND`（否则 NTC/ADC 读数随转速跳）。

---

## 3. 背光 / 使能类 GPIO

- 背光经 MOS 管时先确认**高有效还是低有效**，初始化时先关、点亮后再常开。
- 所有 **EN / 继电器 / 加热** 类输出，固件第一条语句就要钉到安全电平——复位后 JTAG 复用脚（PB3/PB4/PA13/PA14/PA15）可能是内部上拉，硬件还没使能就已经在驱动外设（PITFALLS #55 同类）。

---

## 4. 接线前先确认器件真的在图里

写驱动前，用**最新一版**网表确认这颗器件和它的引脚真的连了：设计文档可能超前于原理图，网表才是现状。查不到就先问用户要当前网表，别照着文档写一堆永远不响的代码。

---

## 5. 编译期坑

- 用 TIM 的工程要在 `HAL_OBJS` 里加 **`hal_tim.o` 和 `hal_tim_ex.o`**，否则链接报 `undefined reference to HAL_TIM_PWM_Init / HAL_TIM_PWM_ConfigChannel / HAL_TIM_PWM_Start`。
- EXTI 服务函数名必须是启动文件里的向量名（如 `EXTI15_10_IRQHandler`），并在 `HAL_GPIO_EXTI_Callback` 之外自己清标志，否则中断反复进。
