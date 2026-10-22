# LVGL 上 MCU（v9）· 移植、性能与「按规格稿实现」工作流

适用：STM32/H7 这类无 OS 裸机 + SPI 小屏（RGB565），要上成熟 GUI 框架而不是自己画。
配套阅读：`references/spi-display-touch-bringup.md`（显示/触摸 bring-up 与 DMA 提速）。

---

## 一、为什么用 LVGL，而不是继续手写

自己写「逐字填充 + 整页重绘」的界面必然：闪烁、换页慢、每个控件都要手算坐标和状态色。LVGL 自带脏矩形、控件树、动画、字体，把「哪块要重画」交给库。
**判断顺序**：先用现成框架（LVGL），只有 Flash/RAM 完全放不下时才退回手写最小 UI。

版本选择：
- **v9.x（最新主线）**：新渲染器、`lv_display_*` API。新项目优先。
- **v8.3 LTS**：教程最多、API 老；只在维护旧工程时用。

---

## 二、移植层（v9 API，约 90 行）

v9 与 v8 的接口差异（照着改名，不要照抄 v8 例子）：

| v8 | v9 |
|---|---|
| `lv_disp_drv_t` / `lv_disp_drv_register` | `lv_display_create(w,h)` |
| `lv_disp_draw_buf_init` + `lv_disp_drv.draw_buf` | `lv_display_set_buffers(disp, buf1, buf2, bytes, LV_DISPLAY_RENDER_MODE_PARTIAL)` |
| `lv_indev_drv_register` | `lv_indev_create` + `lv_indev_set_type` + `lv_indev_set_read_cb` |
| `LV_TICK_CUSTOM` 宏 | `lv_tick_set_cb(cb)` 运行时注册 |
| `lv_scr_load` / `lv_scr_act` | `lv_screen_load` / `lv_screen_active` |
| `lv_btn_create` | `lv_button_create` |
| `LV_COLOR_16_SWAP` | 取消；在 flush 里自己交换字节 |

关键四行（缺一条就花屏/黑屏/卡死）：

```c
s_disp = lv_display_create(320, 240);
lv_display_set_color_format(s_disp, LV_COLOR_FORMAT_RGB565);   /* 必须显式, 否则按 ARGB8888 排 -> 花屏 */
lv_display_set_flush_cb(s_disp, flush_cb);
lv_display_set_buffers(s_disp, buf, NULL, sizeof(buf), LV_DISPLAY_RENDER_MODE_PARTIAL);
```

flush 回调里两件事：**字节交换**（面板要高字节先发，v9 给小端 RGB565）→ **整块 blit 上屏**，最后 `lv_display_flush_ready(disp)`。

**blit 要「一个区域一次传输」，不要逐行发**：每行调一次 `HAL_SPI_Transmit_DMA` 再等它完成，240 行 ≈ **160ms/屏**（同一时刻 ping 板子，RTT 从 2ms 直接飙到 160ms）；改成 `CS` 拉低后按 ≤32KB 分块连发整个区域、发完再拉高，整屏 ≈ **2ms**。CS 粒度是阶梯（每字节 → 一行 → 整区域），**提速先粗 CS 粒度，再谈提时钟**——只提时钟会被每帧调用开销吃掉。

**字节交换必须写到独立的 DMA 缓冲，绝不能就地改 `px_map`。** LVGL 复用同一个绘制缓冲：就地交换把它的数据改坏，下一次局部刷新读到的是上帧的错序字节 → 典型现象是「卡片/边框/条形都画出来了，文字却没有」，或者白字出现红蓝彩边。这个 bug 只在**分块刷新**时明显，纯色测试图看不出来，极易被当成字体或渲染器问题查半天。

```c
static uint8_t *const s_fb     = (uint8_t *)0x24030000u;  /* LVGL 渲染缓冲(AXI) */
static uint8_t *const s_dmabuf = (uint8_t *)0x2403C000u;  /* 交换后的 DMA 缓冲(AXI, 独立) */

static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    uint32_t n = (uint32_t)(a->x2-a->x1+1) * (a->y2-a->y1+1);
    for (uint32_t i = 0; i < n; i++) {           /* 拷到独立缓冲再交换, 不动 px_map */
        s_dmabuf[2*i]     = px[2*i+1];
        s_dmabuf[2*i + 1] = px[2*i];
    }
    st7789_blit(a->x1, a->y1, a->x2-a->x1+1, a->y2-a->y1+1, s_dmabuf);
    lv_display_flush_ready(d);
}
```

只有 `LV_DISPLAY_RENDER_MODE_FULL`（整帧重画）才可以接受就地交换，分块模式一律走拷贝。

### 更省一条：让 LVGL 直接渲染成大端（零拷贝）
上面那套「拷到独立缓冲再交换」是**已验证可用**的基线。想再省掉每帧 25KB 的拷贝+交换，走 LVGL v9 官方的字节序格式：

```c
lv_display_set_color_format(s_disp, LV_COLOR_FORMAT_RGB565_SWAPPED);  /* 面板要高字节先 -> LVGL 直接按大端渲染 */
/* flush_cb 里不再交换, 直接把 px_map 交给 DMA */
st7789_blit_start(a->x1, a->y1, w, h, px_map);
```

省掉 CPU 拷贝的同时也省掉第二组 DMA 缓冲（双缓冲时直接复用 LVGL 的两块绘制缓冲）。
**代价与验证**：整个缓冲的字节序从此是大端，凡是要读它的人（dump 转 PNG、其它直接读缓冲的代码）都要按大端解码；切换后必须按第九节 dump 一帧确认「深底 + 白字 + 青条」，出现红蓝颠倒/彩边就是格式没生效或与面板不符，**立即回退到独立缓冲交换**。别把这条当无条件更优——它是「省 CPU、换字节序约定」。

### 再加一级：双缓冲异步流水（渲染与传输并行）
同步 blit 时 "渲染一块 → 阻塞等 SPI 传完 → 再渲染下一块"，两者永远串行，刷新率就是带宽的一半。把 flush 拆成"启动即返回"：

```c
/* 绘制层 */
void st7789_blit_start(x, y, w, h, pix)      /* set_window → DC_DATA → CS_LOW → HAL_SPI_Transmit_DMA */
void HAL_SPI_TxCpltCallback(SPI_HandleTypeDef *h)   /* CS_HIGH; busy=0; st7789_dma_done_hook(); */
int  st7789_busy(void);

/* 移植层 */
static uint8_t *const s_fb1=0x24030000, *const s_fb2=0x24038000;   /* LVGL 双绘制缓冲 */
static uint8_t *const s_dm1=0x24040000, *const s_dm2=0x24048000;   /* 各自的交换缓冲 */
void st7789_dma_done_hook(void) { if (s_disp) lv_display_flush_ready(s_disp); }
/* flush_cb: while(busy){} → 交换进 dmN → blit_start() → 不在这里调 flush_ready */
lv_display_set_buffers(s_disp, s_fb1, s_fb2, W*LINES*2, LV_DISPLAY_RENDER_MODE_PARTIAL);
```

两条硬约束：
- **`lv_display_flush_ready()` 只能在 DMA 完成中断里调**。在 `flush_cb` 里提前调 = LVGL 认为缓冲已空并立刻改写它，正在传输的数据被覆盖（撕裂纹/错行的来源）。
- **`st7789_dma_done_hook` 必须是强符号**。基类里写 `__attribute__((weak))` 的空实现时，如果移植层的定义漏了/写成了 `static`，链接器**静默**选弱符号 → 永远不 `flush_ready` → 主循环死锁在 `lv_refr.c: wait_for_flushing`，表面现象是"板子假死、ping 不通"。判据：halt 后 `info` 拿 PC → `arm-none-eabi-addr2line -f -e build/fw.elf <PC>`，落在 `wait_for_flushing` 就是这条；再用 `arm-none-eabi-nm build/fw.elf | grep _hook` 看是不是 `W`（弱）而不是 `T`。
- **单次 DMA 长度是 `uint16_t`（上限 65535 字节），不是 32KB**：320 宽 × 60 行 = 38400B 可以一次发完，加大绘制缓冲（行数）是降 flush 次数的正路。
- **绝不要把上限夹到小于本区域字节数**：曾为「保险」在 blit 里写 `if (total > 32768) total = 32768`，把 60 行区域的后 19 行静默丢掉——现象是**画面在 y≈120 处出现一条横线/下半截错位**，进新页面才消失；而 `g_spi_err` 一直是 0（每次传输本身都成功）。判据：错位/横线出现在**固定行高倍数**（40/60/80 行处）且与内容无关 → 先算 `w*2*h` 和 blit 里的长度上限。

---

## 三、三个必调参数（不调就「花屏 / 死循环 / 链接溢出」）

### 1. 栈：默认链接脚本 1KB 不够，给 16KB
LVGL 的文本换行/字模排版递归较深。栈 1KB 时现象是：**屏幕能出一点内容，随后主循环卡在 `lv_text_get_next_word` / `lv_text_*` 里出不来**（PC 在 lv_text 内反复跳，ping/业务全停）。
改 `_Min_Stack_Size = 0x4000;`（16KB）。8KB 能撑单页；**4 个页面常驻 + 中文 4bpp 字模排版时 8KB 仍可能踩栈**（表现同样是「只有边框没有文字」），一次给到 16KB 省一轮。LVGL 堆挪去 AXI 之后 DTCM 通常有富余，栈加大不会挤爆链接。**先怀疑栈，再怀疑 LVGL bug。**

### 2. 堆：不要放 DTCM，放 AXI（`LV_MEM_ADR`）
4 个页面 + 上百个对象时，`LV_MEM_SIZE` 放 DTCM 会把 128KB RAM 撑爆（链接报 `region RAM overflowed`）。
做法：给 LVGL 堆一个**固定地址**（`lv_conf.h` 里 `LV_MEM_ADR`，必须是编译期常量），放在链接脚本的 AXI 段之后：

```c
#define LV_MEM_ADR   0x24010000U      /* AXI SRAM, 在 .eth_dma 之后 */
#define LV_MEM_SIZE  (96U * 1024U)    /* 4 屏 / ~150 个对象实测占 61~64% */
```
并把链接脚本的 ETH_RAM 段放大（如 256K），链接完用 `nm` 查 `_eeth_dma` **必须小于** 堆地址，确认没叠上（DMA 缓冲 + LVGL 绘制缓冲都在这一段）。

**堆不够的表现是「没有任何报错的黑屏」**：`lv_obj_create` 返回 NULL，后面一连串 `lv_obj_set_*` 静默失败，屏上只剩 screen 底色（背光正常、SPI 传输计数正常增长、程序照跑 ping 通），极易误判成驱动/像素格式问题。
32KB 对 4 个页面远远不够（实测 `used_pct=100`、剩 12 字节 → 黑屏）；给到 96KB 后落到 61%，`free≈36KB` 才算安全。

**把堆用量做成可读全局量**（比猜快得多，任何一次「黑了/只有部分控件」先看它）：

```c
/* 刷新定时器里 */
lv_mem_monitor_t mon; lv_mem_monitor(&mon);
g_lv_free = mon.free_size; g_lv_biggest = mon.free_biggest_size; g_lv_used_pct = mon.used_pct;
```

再用 `arm-none-eabi-nm -n build/fw.elf | grep g_lv_` 取地址、`mem32` 读。`used_pct` 接近 100 / `free_biggest` 只有几十字节 = 堆爆了，不是渲染问题。

### 3. 像素格式：显式 RGB565
不写 `lv_display_set_color_format(...LV_COLOR_FORMAT_RGB565)` 时，v9 可能按 32 位排缓冲，而 flush 按 2 字节/像素交换 → **整屏花屏/错位**。
仅设 `LV_COLOR_FORMAT_DEFAULT` 不够，display 创建后再显式设一次。

---

## 四、构建（Makefile / Cube 工程）

1. 源码布局：v9 是 `src/`（实现）+ `include/lvgl/`（公共头）+ 根目录 `lvgl.h`。**三样都要拷进工程**，因为 `src/**/xxx.c` 里有 `#include "../../lvgl.h"`。
2. 头文件路径：`-IMiddlewares/lvgl9 -IMiddlewares/lvgl9/include -DLV_CONF_INCLUDE_SIMPLE`，业务代码 `#include "lvgl/lvgl.h"`。
3. 源文件列表**不要用 `$(shell find ...)`**：Windows 版 GNU Make 的 `$(shell)` 走 cmd.exe，没有 `find`，变量会静默为空 → 链接时报一堆 `undefined reference to lv_init`。改用递归 `$(wildcard src/*.c src/*/*.c src/*/*/*.c src/*/*/*/*.c)`。
4. 字体按 `lv_conf.h` 的 `LV_FONT_MONTSERRAT_xx 1` 打开；每加一个字号多占几十 KB Flash（2MB Flash 的项目可以放心开 6~8 档）。
5. 编译 400+ 个 .c 需要 `-j8`，首次全量 3~6 分钟；改 `lv_conf.h` 会触发全量重编，尽量一次把参数定好。
6. **`make` 说 `Nothing to be done for 'all'` 不等于"已是最新"**：改完 `.c` 之后仍可能因为时间戳精度/依赖表不全而不重编，接着烧的就是**上一版固件**——现象是「刚改的逻辑一点没生效」，非常容易被误判成代码写错。判据与做法：改完 `make` 必须先看到 `arm-none-eabi-size`/`objcopy` 那几行、且 `build/firmware.bin` 的**字节数变了**；没变就 `rm -f build/app/xxx.o`（或 `touch` 源文件）再 `make`，确认 `.bin` 大小变化后才烧。烧完再 `verify` 一次，把"跑的是旧固件"这条彻底排掉。

---

## 五、按「规格稿」实现界面（用户给 JSON/MD 时的工作流）

用户拿一份 `screen-spec.json`（逐元素 `[x,y,w,h]`、tokens、components、states）+ 说明文档时，**不要自己发挥布局**：

1. **坐标只认 JSON**：每个元素 `lv_obj_set_pos(o, x, y)` + `lv_obj_set_size(o, w, h)`，父级用满屏透明容器；**不用 flex/grid**（定值布局用自动排布只会偏）。
2. **component 模板展开**：JSON 的 `components.<name>.children` 是相对父容器的坐标；`screens[].elements[]` 里 `component: "tile"` 就按该模板建子元素，`box` 给父容器。
3. **配色只来自 tokens**，不要自己调色；状态色（正常/告警/关闭）按 `states` 表映射。
4. **示例读数是占位**：保留小数位格式（`%.2f V` / `%.2f A` / `%.1f W` / `%d%%`），数值换成真实采样；数字右对齐 + 固定宽度，刷新不抖。
5. **字号不齐时取邻近档**：spec 写 9/11 而库只有 8/10/12 时用 10/12，并在交付说明里写明「按邻近档取，坐标未改」——不要为了凑字号去改坐标或自己造点阵字。
6. **规格稿没给的后台数据**（灯效、NTP、SN、某路总线统计）先按占位显示，并在交付时列清「哪些字段是占位、等哪个 API」；不要编造数值当真实数据。
7. 交付前自己核对一遍：元素是否越界（x+w>320 / y+h>240）、重叠、文字是否被裁（`LV_LABEL_LONG_CLIP` + 固定宽度）、切页/返回/抽屉是否都能回到首页。

---

## 六、性能与刷新

- 500ms 定时器只改 label 文本和 bar 值（`lv_bar_set_value(..., LV_ANIM_OFF)`），不要整页 `lv_obj_invalidate`。
- 绘制缓冲给 320×40×2（≈25KB）足够；放 AXI（SPI DMA 要读）。
- 每屏对象数控制在 ~60 以内；4 屏同时常驻约 150 个对象，**堆给 96KB**（实测 61~64%）。要压内存就改成「进页时创建、退页时 `lv_obj_del`」，而不是把堆调小去试。
- **单个控件的"宽度不够"会让 LVGL 每帧重排**：label 的文本比容器宽（例：卡片右上的状态文字 "已关闭" 塞进 34px 位）时，LVGL 每帧都要重新算换行/尺寸并重绘那一小块。用「读最近一次 flush 区域」两秒采两次就能认出来：同一个 30×20 的小区域在不停刷、而 flush 总数高达 50~100 次/秒，就是某个对象在原地空转，不是业务在刷新。修法：给这类 label **固定 `lv_obj_set_size()` + `lv_label_set_long_mode(l, LV_LABEL_LONG_CLIP)`**，并把文案压到 2 个汉字（`输出`/`关闭`/`故障`），别用"已关闭"这种长词。
- **主循环是否被 LVGL 拖住，用 ping RTT 当探针**：同一台 PC 上 `ping` 板子，正常应 1~5ms；飚到 100ms+ 说明每次刷新都在阻塞主循环（缓冲太小/整页 invalidate/字号过大重排）。这是不用示波器就能量到「卡不卡」的判据。
- 若出现「刷新时整屏抖」，先查是不是自己在定时器里重画整页，而不是 LVGL 的脏区问题。
- **数值刷新要加迟滞**：ADC 噪声会让 `%d.%02d` 每次都变一位 → 每个 label 都 invalidate，实测可到 **230 次 flush/秒**（主循环被拖住、手感极差）。按物理量给死区（电压 20mV / 电流 20mA / 功率 0.2W），超死区才写 label；同样条件下 flush 掉到 ~35 次/秒。**字符串相同就不要 `lv_label_set_text`**（LVGL 不判重）。
- **先算带宽上限，再答应帧率**：SPI 32MHz ≈ 4MB/s，320×240×2 = 153KB → 全屏一帧 **38ms，最多 26fps**（实测整屏 fill 47ms）；只有「只刷脏区」时小区域（几 KB）才能到 60fps。向用户报「多少帧」之前先给这个算式，别把「全屏 60fps」当可交付项。
- **想提 SPI 时钟先看电平余量，不只是面板上限**：面板手册写的串行时钟上限（ST7789 ≈62.5MHz）只是必要条件。屏是 5V 供电、MCU 是 3.3V 推挽输出时，模块若按 5V CMOS 阈值（VIH≈0.7×VDD≈3.5V）判读，3.3V 本来就压线，32MHz 能跑是靠余量；再翻倍到 60MHz 时序余量同时减半，花屏概率极高。**这种情况要提速度先解决电平（电平转换/3.3V 供屏），不是改代码**。
- **动画也是带宽问题**：抽屉整块 196×240≈94KB，移一像素就要重画被覆盖与被揭开的整条 → 帧率天花板仍是全屏那一档；想让「看起来顺」，缩短位移距离、拉长时长、去掉整页淡入（`lv_screen_load_anim(FADE)` 每帧全屏重绘），而不是在动画里省几个控件。
- 颜色/边框这类样式改动比改文本贵（会 invalidate 整个对象）：状态没变就不要每帧重设 `bg_color`，缓存上一次的状态位，只在跳变时改。

---

## 七、中文界面：必须自己做子集字体（否则全是方块）

LVGL 内置的 `lv_font_montserrat_*` **只有拉丁字形**，任何中文都会渲染成方框（tofu）。症状很好认：布局、颜色、图标都正常，但所有中文位置是一排方块，且方块比文字重 → 帧率也被拖低。

做法是给界面用到的**这几个字**生成一份子集字体（不是整字库）：

1. 从源文件里收集实际用到的汉字（`0x4E00–0x9FFF`）。
2. 准备一个 TTF：Windows 的 `msyh.ttc` 是 TTC 集合，`lv_font_conv` 直接报 `Unsupported OpenType signature ttcf` → 先用 fonttools 抽出第 0 号字体存成 TTF。
3. `lv_font_conv` 生成 LVGL 格式（4bpp；深底小字号 1bpp 边缘毛刺明显）。
4. 在 `lv_conf.h` 之外**用 `LV_FONT_DECLARE(cjk10);`** 引用，界面统一用它渲染中文；`26/34` 这种纯数字大读数继续用 Montserrat。

```bash
# 1) 收集字形
python -c "s=open('app/ui.c',encoding='utf-8').read(); open('chars.txt','w',encoding='utf-8').write(''.join(sorted({c for c in s if '\\u4e00'<=c<='\\u9fff'})))"

# 2) TTC -> TTF（lv_font_conv 不认 ttcf）
python -c "from fontTools.ttLib import TTFont; TTFont(r'C:/Windows/Fonts/msyh.ttc', fontNumber=0).save(r'C:/tmp/msyh0.ttf')"

# 3) 生成 LVGL 字体（10px/12px 两档，ASCII 一并打包；★ 必须 --no-compress）
lv_font_conv --font C:/tmp/msyh0.ttf --size 10 --bpp 4 --no-compress \
  --format lvgl --lv-include lvgl.h --range 0x20-0x7E \
  --symbols "$(cat chars.txt)‹›·€°" -o app/fonts/cjk10.c
```

**`--no-compress` 不能省**：不加时 `lv_font_conv` 可能输出压缩位图（生成的 .c 里 `.bitmap_format = 1`），而固件没开解压 → **每个字渲染成实心方块**（不是缺字的空心 tofu，是所有笔画糊成一块），并且方块很重、把帧率一起拖下去。生成后回读一行确认：`grep bitmap_format app/fonts/cjk10.c` 必须是 **0**。

要点：
- 120 个汉字 + ASCII 在 10px/12px 4bpp 下只占 ~9KB / ~20KB Flash，比整字库小两个数量级；**换文案就要重新生成**（漏字 = 那几个字又变方块）。
- **改完文案先跑校验，别靠眼睛**：`python scripts/lvgl_font_subset_check.py --font app/fonts/cjk10.c --src app --src drv` 会把源码里用到但字体里没有的汉字逐个列出来（退出码 1），并直接打印带 `--no-compress` 的重建命令。新增一个按钮/标题就跑一次，成本两秒；不跑就只能等用户看见方框再回来改。
- 生成的文件名就是字体符号名（`cjk10.c` → `const lv_font_t cjk10`），Makefile 里把 `.o` 加进链接。
- 用 `npm i -g lv_font_conv` 装；`npx` 在离线/代理环境容易卡，直接调 `node <npm root -g>/lv_font_conv/bin/lv_font_conv.js` 最稳。
- 中文 10px 是可用下限（spec 若写 8/9，统一提到 10 并保持行高容器不变）。

---

## 八、黑屏 / 方块 / 卡顿 的三查顺序

按这个顺序，每条都有可读判据，不要一上来改驱动：

| 现象 | 先查 | 判据 |
|---|---|---|
| 全黑但背光亮 | **LVGL 堆** | `g_lv_used_pct≈100`、`free_biggest` 只有几十字节 → 调大 `LV_MEM_SIZE` |
| 全黑且背光也灭 / 传输计数为 0 | 面板初始化是否被跳过 | 换用 LVGL 后 `ui_init()` 不再被调用是常见疏漏——`st7789_init()/背光/gt911_init()` 必须挪到移植层的初始化函数里 |
| 花屏/错位/颜色怪 | **像素格式与字节序** | 是否显式 `LV_COLOR_FORMAT_RGB565`；flush 里是否做了 2 字节交换（v9 无 `LV_COLOR_16_SWAP`） |
| 中文全是**空心方框**（tofu） | 字体无 CJK 字形 | 见第七节，生成子集字体 |
| 字都是**实心方块**、笔画糊成一坨 | 字模被压缩而固件没开解压 | 重新生成时加 `--no-compress`，检查 `.bitmap_format == 0` |
| 只有卡片/边框/条，**没有文字** | flush 就地改了 LVGL 的 `px_map`，或栈不够 | 交换字节改到独立 DMA 缓冲；栈 16KB |
| 画面出来但极卡 | 栈 / 缓冲 / 整页 invalidate | 栈 16KB；Ping RTT 从 1ms 变 100ms=阻塞；看是否每帧重画整页 |
| 卡在 `lv_text_*` 不动 | 栈溢出 | `_Min_Stack_Size` 1KB → 16KB |

---

## 九、不用问用户也能验收画面：把帧缓冲 dump 成 PNG 自己看

「屏上是黑/花/只有框」这类描述既慢又不可靠。**LVGL 的绘制缓冲和 DMA 缓冲都在内存里，直接读出来渲染成图片自己看**，一轮就能定性，不需要用户描述、也不需要拍照：

```bash
# 1) 取缓冲地址（放在 AXI 的固定地址最好，省得每版重查符号）
arm-none-eabi-nm -n build/fw.elf | grep -E "s_fb|s_dmabuf"

# 2) 用 J-Link 存盘（★ 长度写十六进制，写完核对文件大小）
#    JLink.exe -CommanderScript dump.jlink
#    si SWD / device <型号> / speed 4000 / connect /
#    savebin C:/tmp/fb.bin 0x24030000 0x6400 / q

# 3) 转 PNG 后自己看图
python -c "from PIL import Image;d=open(r'C:/tmp/fb.bin','rb').read();w,h=320,40;\
px=[(((v:=(d[2*i]<<8)|d[2*i+1])>>11&0x1F)*255//31,((v>>5)&0x3F)*255//63,(v&0x1F)*255//31) for i in range(w*h)];\
im=Image.new('RGB',(w,h));im.putdata(px);im.save(r'C:/tmp/fb.png')"
```

判读要点：
- **字节序**：缓冲里是 flush 前（小端）还是 flush 后（大端）决定解码方式；两种都解一遍，哪种出「深底 + 白字 + 青条」就是对的。
- **只看最后一块**：`PARTIAL` 模式下缓冲只保留最后一次 flush 的区域（如 40 行）；想看整屏就临时切 `LV_DISPLAY_RENDER_MODE_FULL` 全画一帧再 dump。
- **`savebin` 长度给十进制会多写/少写**：用 `0x6400` 这种十六进制，并 `ls -l` 核对字节数（320×40×2 = 25600 = 0x6400）。
- 缓冲里**有文字、屏上没有** → 问题在 flush 之后（字节交换、SPI 时序、CS 粒度）；缓冲里**就没文字** → 问题在 LVGL 侧（栈/堆/字体/裁剪）。这一条判据能把排查范围一次砍半。
- **要按时间点看画面，`r`（复位）和 `g`（运行）必须写在同一个 J-Link 会话里**：只发 `r` 不发 `g`，CPU 停在复位向量、RAM 还是上一轮的内容，dump 出来的"开机 300ms 画面"其实是上次跑完的首页，据此判断动画没出现会一路查错方向。脚本形如 `connect / r / g / Sleep 350 / savebin ...`。
- **`[ui]`/诊断 printf 里的 `lbl_w=0 lbl_h=0` 不代表控件没尺寸**：`lv_obj_get_width()` 在自动尺寸布局前读到的是 0，看画面和缓冲才作数；别拿这个 0 去改布局。

---

## 十、交互层：遮罩、抽屉、手势（全是「点了没反应 / 一闪就没」）

### 1. 全屏遮罩会吞掉所有点击
抽屉/弹层的遮罩 `scrim`（铺满 320×240、`LV_OBJ_FLAG_CLICKABLE`）一旦创建在内容之后，它就**永远在上层**，即使 `bg_opa = TRANSP` 也照样拦截点击。典型症状：**点卡片完全没反应**（点击全被遮罩吃掉），而遮罩本身只在 `CLICKED` 里做「关闭抽屉」，看起来像「界面卡死」。
铁律：**关闭态直接 `lv_obj_add_flag(scrim, LV_OBJ_FLAG_HIDDEN)`**，打开抽屉时才 `clear_flag`；不要靠「透明度 0」当隐藏。

### 2. 手势打开的抽屉会被同一次手势关掉
`LV_EVENT_GESTURE`（左滑）在**手指抬起**时才触发，`menu_open()` 立刻让遮罩可见；紧接着这次抬起的 CLICK 往往就落在刚出现的遮罩上 → `menu_close()`，表现是**抽屉闪一下就没了**。
做法：记录打开时刻，**遮罩点击在打开后 400ms 内直接 return**；配合按下的沿触发，才不会同一次手势自伤。

### 3. 滚动条：屏幕对象也要关
`lv_obj_set_scrollbar_mode(o, LV_SCROLLBAR_MODE_OFF)` 只写在构造容器里不够——`lv_obj_create(NULL)` 出来的 **screen 本身也是可滚动对象**，它会带一条滚动条。四个 screen 都要单独设一次，并 `lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE)`。

### 4. 抽屉要「覆盖」而不是「联动」
规格稿里常写「首页整体左移 18px」的联动效果；用户实际要的往往是**纯覆盖**（遮罩 + 抽屉压在上面，首页不动）。联动会让两页看起来「粘在一起」，且每次动画多一整屏重绘。**默认先做纯覆盖**，要联动再单独加。

### 5. 点击去抖 + 沿触发
`lv_indev` 在按住期间会持续上报 PRESSED。只在**按下沿**（`pressed` 由 0→1）派发点击，并加 150~250ms 去抖；否则一次长按会连触发多次（开关类控件表现为「开了又关」）。

### 6. 抽屉要跟手：用 PRESSING + `lv_indev_get_vect()`，不要只靠 GESTURE
只挂 `LV_EVENT_GESTURE` 的抽屉是「松手才动」，用户会明确抱怨不跟手、不能中途反悔。做法是把抽屉当成可拖拽面板：

```c
/* PRESSED: 起手位置在右边缘(如 x>210)且抽屉关闭 -> s_drag=1, s_dx=320
   PRESSING: lv_indev_get_vect(indev,&v); s_dx += v.x; 夹到 [124,320]; lv_obj_set_x(drawer,s_dx);
             同时 dim_to(s_dx): 遮罩 opa = (320-s_dx)*150/196   ← 首页「被压暗后退」
   RELEASED / PRESS_LOST: 按位置吸附(s_dx<218 开, 否则关), 用 150ms 缓动补完 */
```

### 7. 拖拽「完全不跟手」甚至抽屉拉不出来：事件不会自动冒泡到父容器
LVGL 把 `PRESSED / PRESSING / RELEASED` 只发给**手指按下的那个对象**。首页被卡片铺满时，手指落在卡片上，挂在 screen / root 上的拖拽处理器**一次都不会被调用**——现象是「抽屉彻底拉不出来」，或只剩 `LV_EVENT_GESTURE`（松手才动）。两处都要改：

- 给可点击的子对象（卡片、列表项、按钮）加 `lv_obj_add_flag(obj, LV_OBJ_FLAG_EVENT_BUBBLE)`；
- 处理器挂在**子对象的直接父容器**上，不要挂到更上一层：冒泡只在每一层都带 `EVENT_BUBBLE` 时继续向上，中间断一层就静默收不到。

另外不要要求「必须从屏幕右边缘（x>210）起手」：**任意位置起手 + 18px 死区**（累计位移超过 18px 才算拖动，否则按点击处理）既能拖，又不影响点卡片；死区同时挡掉「手指抖一下就把抽屉拉出来」。

要点：
- **一次拖拽里能来回**：位移是累加的，用户往右推立刻回退，不在松手时才决定；
- **进入拖拽前先 `lv_anim_delete(drawer, NULL)`**，否则残留的开合动画会和手指抢位置；
- 抽屉自身也要挂同一组 PRESSED/PRESSING/RELEASED，否则打开后在抽屉上右滑拖不动；
- 首页「隐退」用遮罩透明度按位移比例变化（**不要平移首页**，也不要整页 `style_opa` 动画——那是整屏重绘）；
- `PRESS_LOST` 必须和 `RELEASED` 走同一个收尾函数，否则来电/切页后抽屉会卡在半开位置。

---

## 十一、开机顺序：屏要在外设初始化之前点亮

典型症状：**蜂鸣器/风扇动作先响，屏要等好几秒才亮**。根子是 `main()` 里把 `st7789_init()/lv_init()/ui_lvgl_init()` 排在了 `eth_phy_probe()`（MDIO 探测 + 自协商）、板级自检（蜂鸣器短鸣、风扇测速）、`lwip_init()` 之后——那些每项几百 ms 到几秒。

固定顺序：

```
HAL_Init → 时钟 → 安全引脚(EN 拉低) → RTT/黑匣子
→ 屏 + LVGL + 界面（先出画面）
→ 板级自检 / PHY / lwIP / 桥接服务
```

界面上要显示的网络状态、采样值本来就是"后到的数据"：先用占位（`--`）建界面，后续刷新定时器自然填上。反过来把屏放到最后，用户每次上电都要多等几秒，且看起来像"启动失败"。

### 开机动画（渐显 → 定格 → 渐隐 → 首页渐入）
用户要「开机动画」时，别用整页 `lv_screen_load_anim(FADE)`——那是每帧全屏重绘，在 SPI 屏上非常贵。用一个独立的 `scr_splash`（纯色底 + 居中品牌字），只对**文字对象**做透明度动画，代价只有那几行字：

```c
static void opa_anim_cb(void *o, int32_t v) { lv_obj_set_style_opa((lv_obj_t *)o, (lv_opa_t)v, 0); }
/* lv_anim 的 exec_cb 签名是 (void*,int32_t)，不能直接塞 lv_obj_set_style_opa（它要 3 个参数）*/
lv_screen_load(scr_splash);
// fade in 320ms -> lv_timer_create(hold_cb, 1100, NULL) -> fade out 240ms -> ready_cb 里 load(home) + home 渐入 280ms
```

- 总时长控制在 ~1.5s 内；定格时间用一次性 `lv_timer` 而不是 `HAL_Delay`（否则整套 LVGL/网络都被堵住）。
- 动画结束用 `lv_anim_set_ready_cb` 切页，不要在定时器里再轮询屏幕状态。
- **开机动画「看不见 / 一闪而过」几乎都不是动画参数问题**：LVGL 的动画只在 `lv_timer_handler()` 里推进。若在 `ui_init()` 之后还有**阻塞几秒的初始化**（蜂鸣器+风扇测速台架自检、PHY 自协商、DHCP、降速重试），这段时间主循环不转 → 画面定格在某一帧，等主循环终于跑起来时动画一帧跳完。修的是初始化顺序与耗时，不是把 duration 调大。

### 开机时间线打点（先量再改，别猜哪一步慢）
在 `main()` 每个初始化段之间打一行带毫秒的 RTT，一次就能看清几秒钟花在哪：

```c
#define BOOTT(tag) SEGGER_RTT_printf(0, "[t] " tag " %u  ", (unsigned)HAL_GetTick())
BOOTT("ui_in"); api_init(); BOOTT("api"); lv_port_init(); BOOTT("lvport");
ui_lvgl_init(); BOOTT("ui_done");
```

一次真实的时间线（同一块 H743 + 320×240 SPI 屏）与对应处置：

| 打点 | 实测 | 说明 / 处置 |
|---|---|---|
| `lvport`（屏驱动+LVGL 初始化） | ~700ms | 屏复位+SLPOUT 固定延时约占一半；**去掉多余的 SWRESET（硬件已复位过）**、复位后等待 120ms→60ms，可省约 200ms |
| `ui_done`（建界面） | 1.65s → 0.6s | 开机一次性建 4 个屏太贵；**只建首页 + 开屏，其余屏按需 `ensure_xxx()` 首次进入时再建** |
| 板级自检（蜂鸣器/风扇测速） | **6s** | 台架自检不能放在开机路径；默认关（编译开关），需要时手动跑 |
| 首帧到主循环 | 2.4s → 1.3s | 上面几条合起来的效果；用户感知的"开机慢"基本都在这 |

配套两条：① **开屏 Logo 出现 <1.5s、进首页 <3s** 是可接受的目标，超出就按上表逐段砍；② 让屏先亮、要显示的数据用占位符（`--`）后填，而不是等外设全初始化完再画第一帧。
