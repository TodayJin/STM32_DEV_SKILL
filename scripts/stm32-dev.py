#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stm32-dev: 探针无关的 STM32 全流程调试/开发工具(选型 + 烧录 + gdb + 观测 + 脚手架)。

调试器不写死: J-Link / ST-Link(V2/V3) / DAPLink(CMSIS-DAP) 各有后端, 命令层按探针能力选路;
选好的探针 + 序列号写进工程根的 .stm32-dev.json, 之后全自动, 不再问人。

  python3 <skill>/scripts/stm32-dev.py probe list        # 支持哪些调试器、各自能力
  python3 <skill>/scripts/stm32-dev.py probe detect      # 现在插着哪个
  python3 <skill>/scripts/stm32-dev.py probe use stlink --serial 0025...  # 选定并写进工程配置
  python3 <skill>/scripts/stm32-dev.py probe info jlink  # 单探针: 工具 / 最好的能力 / 注意事项 / 降级路径
  python3 <skill>/scripts/stm32-dev.py setup             # 一键: 探针 x 工具链体检, 缺什么怎么装
  python3 <skill>/scripts/stm32-dev.py doctor            # 环境自检
  python3 <skill>/scripts/stm32-dev.py flash             # 按探针选路, 默认刷后逐字节校验

目标: 别人下载到一个空环境也能用。自动检测环境、自动发现芯片、
自动定位/获取 SVD, 不写死芯片和路径。

流程: 首次运行 `doctor` 自检环境, 缺什么给指引。

  python3 <skill>/scripts/stm32-dev.py doctor          # 环境自检+引导
  python3 <skill>/scripts/stm32-dev.py read g_motor.enabled   # 读变量
  python3 stm32-dev.py read GPIOA.ODR               # 读寄存器
  python3 stm32-dev.py read "*(uint32_t*)0x20000000"  # 读内存(要写解引用表达式)
  python3 <skill>/scripts/stm32-dev.py write g_motor.enabled=1  # 写变量
  python3 <skill>/scripts/stm32-dev.py break main.c:145 # 断点
  python3 <skill>/scripts/stm32-dev.py continue | step 3 | info  # 运行控制
  python3 <skill>/scripts/stm32-dev.py attach --elf build/test.elf --device STM32H743VI

=== J-Link 后端的关键坑(踩坑记录, 详见 SKILL.md; 其他探针见 PROBES.md) ===
* 不用 openocd: J-Link 用 SEGGER 驱动, openocd 用 libusb, 二者冲突(LIBUSB_ERROR_NOT_FOUND)。
  本脚本绕开 openocd, 直接用 JLinkGDBServerCL + gdb。
* 烧录别用 STM32CubeProgrammer: 它连 J-Link 不可靠(No debug probe detected)。
  用 JLink.exe `loadfile <hex>` 烧录。
* 芯片型号必须完整: JLinkGDBServer 要 STM32H743VI, 给 STM32H743 会报
  "Failed to get index for device name"。脚本内置 _DEVICE_SUFFIX 补全表。
* SVD 命名相反: 用系列名 STM32H743 匹配 STM32H743.svd, 别带 VI。
* 读变量停在启动早期: 连上后可能停在 HAL_Init(), 变量是初始值, 先 continue 再读。
* server 必须杀干净: 每个命令起 server->gdb->杀 server, terminate+wait+kill 兜底,
  否则残留占 3333 端口导致误判"已运行"。
* JLink.exe 命令行探测不可靠: 不要用它自动探测板子, 直接 read 试连最稳。
"""


import argparse
import contextlib
import glob
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# 配置(可用环境变量覆盖, 默认自动探测)
# ---------------------------------------------------------------------------
# ---- 结构化输出(--json) ----------------------------------------------------
_JSON = False          # 是否 --json 模式
_JSON_DATA = {}        # 命令填充的结构化结果


def jset(**kw):
    """在 --json 模式下记录结构化结果(非 json 模式下无副作用)。"""
    if _JSON:
        _JSON_DATA.update(kw)


GDB_PORT = int(os.environ.get("JLINK_GDB_PORT", "3333"))
DEFAULT_SERIAL = os.environ.get("JLINK_SN", "")
_SERIAL = DEFAULT_SERIAL     # 运行时由 main() 用 --serial 覆盖(多探针时必须给)
_PROBE = ""                  # 生效的探针(调试器) id; main() 用 --probe / 工程配置覆盖
_SERVER_KIND = ""   # "jlink" / "openocd": 收尾恢复运行的命令不一样

# 常见安装位置(跨平台)
WINDOWS_JLINK_DIRS = [
    r"C:\Program Files\SEGGER\JLink_V974",
    r"C:\Program Files\SEGGER",
    r"C:\Program Files (x86)\SEGGER",
]
UNIX_JLINK_DIRS = [
    "/opt/SEGGER/JLink",
    "/usr/local/bin",
    "/usr/bin",
]
SVD_SEARCH_DIRS = [
    # STM32CubeCLT (ST 官方)
    "C:/ST/STM32CubeCLT_*/STMicroelectronics_CMSIS_SVD",
    "/opt/STM32CubeCLT_*/STMicroelectronics_CMSIS_SVD",
    # CubeMX 用户包
    os.path.expanduser("~/STM32Cube/Repository/STM32Cube_FW_*/Drivers/CMSIS/Device/ST/STM32*/Include"),  # noqa
    # 本技能目录
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
]


# ---------------------------------------------------------------------------
# 工具定位
# ---------------------------------------------------------------------------
def find_jlink_server():
    """定位 JLinkGDBServerCL(.exe)。环境变量 > PATH > 常见位置。"""
    env = os.environ.get("JLINK_GDB_SERVER")
    if env and os.path.isfile(env):
        return env
    # PATH
    for name in ("JLinkGDBServerCLExe", "JLinkGDBServerCL", "JLinkGDBServer"):
        p = shutil.which(name)
        if p:
            return p
    # 常见目录
    for d in WINDOWS_JLINK_DIRS + UNIX_JLINK_DIRS:
        for name in ("JLinkGDBServerCL.exe", "JLinkGDBServerCL"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    return None


def find_gdb():
    """定位 arm-none-eabi-gdb / gdb-multiarch。"""
    env = os.environ.get("STM32_DEBUG_GDB")
    if env and shutil.which(env):
        return env
    for name in ("arm-none-eabi-gdb", "gdb-multiarch", "gdb"):
        p = shutil.which(name)
        if p:
            return p
    return None


# 注: find_jlink_cmd 只有一处定义(在下面, 带 _looks_like_segger 校验, 避免命中 Java 的 jlink.exe)


def find_rtt_logger():
    """定位 JLinkRTTLogger(.exe)(RTT 抓包用, 只读)。环境变量 > PATH > 常见位置。"""
    env = os.environ.get("JLINK_RTT_LOGGER")
    if env and os.path.isfile(env):
        return env
    for name in ("JLinkRTTLogger.exe", "JLinkRTTLogger"):
        p = shutil.which(name)
        if p:
            return p
    for d in WINDOWS_JLINK_DIRS + UNIX_JLINK_DIRS:
        for name in ("JLinkRTTLogger.exe", "JLinkRTTLogger"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                return cand
    return None


def _jlink_gui_pids():
    """列出 JLinkGUIServer 进程 PID(仅 Windows; JLinkRTTLogger 会连带拉起它)。"""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JLinkGUIServer.exe", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith("jlinkguiserver"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def symbol_addr_from_elf(elf, name):
    """从 ELF 符号表取符号地址(用 readelf, 不依赖 nm)。返回 int 或 None。"""
    readelf = find_readelf()
    if not readelf or not elf or not os.path.isfile(elf):
        return None
    try:
        out = subprocess.run([readelf, "-sW", elf], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[-1] == name:
            try:
                return int(parts[1], 16)
            except ValueError:
                continue
    return None


def analyze_increasing_seq(text):
    """找出一条严格递增的数字列(如 seq=/tick=), 报告丢帧情况。

    返回 (字段名, 样本数, 稳态最大跳变, 超步长次数, 开头追赶区最大跳变, 正常步长) 或 None。
    """
    import re
    cand = {}   # name -> [values]
    for line in text.splitlines():
        for k, v in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(\d+)", line):
            cand.setdefault(k, []).append(int(v))
    best = None
    for k, vals in cand.items():
        if len(vals) < 5:
            continue
        inc = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
        if inc < (len(vals) - 1) * 0.9:
            continue
        deltas = [b - a for a, b in zip(vals, vals[1:])]
        # 开头 20% 是"抓包起始的追赶区"(先吐缓冲区旧内容, 再跳到实时数据), 不计入丢帧
        skip = max(1, len(deltas) // 5)
        lead = deltas[:skip]
        rest = deltas[skip:]
        if not rest:
            continue
        # 用中位数当"正常步长": tick 每次 +100 是正常的, 不能一律按 >1 判丢帧
        med = sorted(rest)[len(rest) // 2]
        base = max(1, med)
        gaps = sum(1 for d in rest if d > base)
        # 优先选"步长=1"的纯计数列(如 seq), 其次才看样本数
        score = (len(vals), 1 if med <= 1 else 0)
        if best is None or score > best[0]:
            best = (score, k, len(vals), max(rest), gaps, max(lead), base)
    return best[1:] if best else None


def find_python():
    return shutil.which("python3") or shutil.which("python")


def find_readelf():
    """arm-none-eabi-readelf(用于从 ELF 推断芯片)。"""
    for name in ("arm-none-eabi-readelf",):
        p = shutil.which(name)
        if p:
            return p
    # 从 gdb 同目录找
    gdb = find_gdb()
    if gdb:
        base = os.path.dirname(gdb)
        cand = os.path.join(base, "arm-none-eabi-readelf" + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(cand):
            return cand
    return None


# ---------------------------------------------------------------------------
# 芯片 / SVD
# ---------------------------------------------------------------------------
def infer_device_from_elf(elf):
    """从 ELF 的编译单元符号推断芯片型号。返回 e.g. 'STM32H743' 或 None。"""
    readelf = find_readelf()
    if not readelf or not elf or not os.path.isfile(elf):
        return None
    try:
        out = subprocess.run([readelf, "-s", elf], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout
    except Exception:
        return None
    # 从 startup_stm32h743xx.o 提取 h743 -> STM32H743
    m = re.search(r"startup_stm32([a-z0-9]+)", out, re.I)
    if m:
        return "STM32" + m.group(1).upper().replace("XX", "")
    # 回退: 只匹配系列
    m2 = re.search(r"(stm32h7xx|stm32g4xx|stm32f4xx|stm32f1xx|stm32l4xx|stm32f7xx|stm32f3xx|stm32f0xx|stm32g0xx|stm32u5xx|stm32h5xx|stm32c0xx)", out, re.I)
    if m2:
        return m2.group(1).upper()
    return None


def infer_device_generic(elf):
    """优先精确型号, 回退到只是系列。"""
    dev = infer_device_from_elf(elf)
    return dev


def find_svd(device):
    """根据 device 查找 SVD。返回路径或 None。"""
    if not device:
        return None
    # 生成候选文件名
    candidates = set()
    candidates.add(device + ".svd")
    # "STM32H743VI" -> 去掉尾部字母 -> "STM32H743" -> STM32H743.svd
    m = re.match(r"(STM32[A-Z0-9]+?)(?=[A-Z][a-z]?[0-9]*$|$)", device)
    core = re.match(r"(STM32[A-Z0-9]+?)([A-Z]{1,3}[0-9x]{0,2})?$", device)
    if core:
        candidates.add(core.group(1) + ".svd")
    # 通用: 去掉最后 1-3 个大写字母/数字 -> STM32H743
    stripped = re.sub(r"[A-Z]{1,3}[0-9]?[xX]?$", "", device)
    if stripped and len(stripped) > 5:
        candidates.add(stripped + ".svd")
    candidates.add(device.lower() + ".svd")
    # 搜索
    for dirs in SVD_SEARCH_DIRS:
        for dd in glob.glob(dirs):
            for cand in candidates:
                p = os.path.join(dd, cand)
                if os.path.isfile(p):
                    return p
    return None


# 注: SVD 的解析与解码实现在 load_svd() + cmd_svd() 里


# ---------------------------------------------------------------------------
# GDB 服务器
# ---------------------------------------------------------------------------
def _server_args(server, device, port, serial):
    """构造 JLinkGDBServerCL 的命令行。

    单独抽出来是为了能被自检覆盖 —— 以前这里写 args[args.index("USB")+1] 越界,
    只要指定串号就 IndexError。
    """
    sel = ("USB=" + serial) if serial else "USB"
    return [server, "-device", device, "-if", "SWD", "-speed", "4000",
            "-port", str(port), "-nogui", "-select", sel]


def _parse_bool(out):
    """从 gdb 的 print 输出里解析真假: "$1 = true" / "$1 = 0" / "$1 = 7" -> True/False/None。"""
    if not out:
        return None
    m = re.search(r"=\s*(true|false|\d+)\b", out)
    if not m:
        return None
    tok = m.group(1).lower()
    if tok == "true":
        return True
    if tok == "false":
        return False
    return int(tok) != 0


class JLinkServer:
    def __init__(self, device, port, serial):
        self.device = device
        self.port = port
        self.serial = serial
        self.proc = None

    def is_up(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def wait_up(self, seconds=6.0):
        """等端口真正可用(起 server 后立刻查会有瞬时失败, 单次判断不可靠)。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.is_up():
                return True
            time.sleep(0.2)
        return False

    def start(self, detached=False):
        server = find_jlink_server()
        if not server:
            return "ERROR: JLinkGDBServerCL 未找到。请装 SEGGER J-Link 驱动, 或设 JLINK_GDB_SERVER 环境变量。"
        if self.is_up():
            return "J-Link GDB server already listening on %d" % self.port
        _warn_stale_jlink()
        args = _server_args(server, self.device, self.port, self.serial)
        log = tempfile.gettempdir() + "/jlink-gdbserver.log"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached and os.name == "nt":
            # 分离进程, 不随本命令退出(供后续 read 复用)
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        self.proc = subprocess.Popen(
            args, stdout=open(log, "w"), stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        # detached 时记 PID 到文件, 供 stop 用
        if detached:
            self._write_pid(self.proc.pid)
        for _ in range(40):
            if self.is_up() and (self.proc is None or self.proc.poll() is None):
                return "J-Link GDB server started on %d (detached)" % self.port if detached else "J-Link GDB server started on %d" % self.port
            time.sleep(0.25)
        return "ERROR: J-Link GDB server did not come up (see %s)" % log

    @staticmethod
    def _pid_file():
        return os.path.join(tempfile.gettempdir(), "stm32-dev-jlink-server.pid")

    def _write_pid(self, pid):
        try:
            with open(self._pid_file(), "w") as f:
                f.write("%d %s %s" % (pid, self.device or "", self.serial or ""))
        except OSError:
            pass

    @classmethod
    def _clear_pid(cls):
        try:
            os.remove(cls._pid_file())
        except OSError:
            pass

    @classmethod
    def _read_pid_meta(cls):
        """返回 (pid, device, serial)。文件缺失/损坏 -> (None, "", "")。"""
        try:
            with open(cls._pid_file()) as f:
                parts = f.read().split()
            return int(parts[0]), (parts[1] if len(parts) > 1 else ""), (parts[2] if len(parts) > 2 else "")
        except (OSError, ValueError, IndexError):
            return None, "", ""

    @classmethod
    def _read_pid(cls):
        return cls._read_pid_meta()[0]

    def is_ours(self):
        """3333 端口上确实有 server 时, 判断它是不是本工具启动的那个。

        依据: 我们自己写的 pid 文件里的 PID 仍然存活, 且设备/串号一致。
        多探针环境下, 只凭"端口开着"就复用会连到别人的板子。
        """
        pid, dev, sn = self._read_pid_meta()
        if pid:
            if sn and self.serial and sn != self.serial:
                return False
            if dev and self.device and dev != self.device:
                return False
            for n in ("JLinkGDBServerCL", "JLinkGDBServer"):
                if pid in _pids_of(n):
                    return True
            return False
        # 没有 pid 文件: 看看占着这个端口的到底是不是 GDB server。
        # (上一次运行刚退出、端口还没释放, 或上次是被强杀的, 都会走到这里)
        owner = _port_owner_pid(self.port)
        if owner is None:
            return False
        for n in ("JLinkGDBServerCL", "JLinkGDBServer"):
            if owner in _pids_of(n):
                print("    [note] 端口 %d 上的 server 不是本工具记录的(可能是上次残留), "
                      "进程名是 %s, 按复用处理。" % (self.port, n))
                return True
        return False

    def stop(self):
        # 优先按 detach 记录的 PID 杀
        pid = self._read_pid()
        if pid:
            try:
                import signal
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, signal.SIGKILL)
                self._clear_pid()
                return "Stopped J-Link GDB server (pid %s)" % pid
            except Exception:
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            return "Stopped J-Link GDB server"
        return "No running server"


# ---------------------------------------------------------------------------
# OpenOCD GDB server(ST-Link / DAPLink 走这条; J-Link 有自己的 server)
# ---------------------------------------------------------------------------
_OCD_IFACE = {"stlink": "interface/stlink.cfg", "daplink": "interface/cmsis-dap.cfg"}


def _ocd_target(device):
    """芯片型号 -> OpenOCD 目标脚本名: STM32G431CB -> stm32g4x。认不出给 None(不猜)。"""
    m = re.match(r"STM32([A-Z])(\d)", (device or "").upper())
    if not m:
        return None
    return "stm32%s%sx" % (m.group(1).lower(), m.group(2))


class OpenOCDServer:
    """用 OpenOCD 起 GDB server, 给 ST-Link / DAPLink 提供和 J-Link 一样的 gdb 通道。

    接口与 JLinkServer 一致, 这样 with_server 能无差别复用。与 J-Link 的差别(踩坑点):
      * 探针选号用 `adapter serial <SN>`, 不是 J-Link 的 `-select USB=<SN>`;
      * 收尾恢复运行用 `monitor resume`, 不是 J-Link 的 `monitor go`;
      * 探针被别的程序占着/没插时 OpenOCD 可能直接退出, 错误只在日志里, 所以失败要把日志尾巴打出来。
    """

    def __init__(self, device, port, serial, probe="stlink"):
        self.device = device
        self.port = port
        self.serial = serial or ""
        self.probe = probe if probe in _OCD_IFACE else "stlink"
        self.proc = None
        self.cmd = []

    def is_up(self):
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def wait_up(self, seconds=6.0):
        """等端口可用; openocd 若已经自己退出了就不再等(区别于 J-Link 的纯超时)。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.is_up():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def argv(self, openocd):
        """拼 openocd 命令行; 认不出目标脚本给 None。"""
        tgt = _ocd_target(self.device)
        if not tgt:
            return None
        args = [openocd, "-f", _OCD_IFACE[self.probe], "-c", "transport select swd"]
        args += _ocd_speed_arg(self.probe)
        if self.serial:
            args += ["-c", "adapter serial %s" % self.serial]
        # 只留 gdb 端口: telnet/tcl 端口的命令名在 0.11(下划线) / 0.12(空格) 之间变过, 不写就不会踩
        args += ["-f", "target/%s.cfg" % tgt, "-c", "gdb_port %d" % self.port]
        return args

    def start(self, detached=False):
        openocd = find_openocd()
        if not openocd:
            return ("ERROR: 没找到 OpenOCD(ST-Link / DAPLink 靠它提供 GDB 通道)。"
                    "装法: winget install xpack-dev-tools.openocd-xpack, 或设 OPENOCD 指向 openocd.exe。")
        if self.is_up():
            return "OpenOCD GDB server already listening on %d" % self.port
        args = self.argv(openocd)
        if not args:
            return ("ERROR: 认不出芯片(%s)对应的 OpenOCD 目标脚本。用 --device STM32G431CB 这样给全型号。"
                    % (self.device or "?"))
        self.cmd = args
        log = os.path.join(tempfile.gettempdir(), "openocd-server.log")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detached and os.name == "nt":
            flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        self.proc = subprocess.Popen(_argv_for(args[0], *args[1:]),
                                     stdout=open(log, "w"), stderr=subprocess.STDOUT,
                                     creationflags=flags)
        if detached:
            self._write_pid(self.proc.pid)
        for _ in range(48):
            if self.is_up() and (self.proc is None or self.proc.poll() is None):
                return ("OpenOCD GDB server started on %d (detached)" % self.port if detached
                        else "OpenOCD GDB server started on %d" % self.port)
            if self.proc is not None and self.proc.poll() is not None:
                return ("ERROR: OpenOCD 退出了(没起 GDB 端口)。常见原因: 没插探针 / 探针被别的程序占着 / "
                        "目标脚本名不对。日志: %s" % log)
            time.sleep(0.25)
        return "ERROR: OpenOCD GDB server 没起来(see %s)" % log

    @staticmethod
    def _log_path():
        return os.path.join(tempfile.gettempdir(), "openocd-server.log")

    def tail_log(self, n=14):
        """失败时把 openocd 日志尾巴打出来 —— 真正的原因(opencod 的报错)只在这里。"""
        try:
            with open(self._log_path()) as f:
                return "".join(f.readlines()[-n:]).strip()
        except OSError:
            return ""

    @staticmethod
    def _pid_file():
        return os.path.join(tempfile.gettempdir(), "stm32-dev-openocd-server.pid")

    def _write_pid(self, pid):
        try:
            with open(self._pid_file(), "w") as f:
                f.write("%d %s %s" % (pid, self.device or "", self.serial or ""))
        except OSError:
            pass

    @classmethod
    def _clear_pid(cls):
        try:
            os.remove(cls._pid_file())
        except OSError:
            pass

    @classmethod
    def _read_pid_meta(cls):
        try:
            with open(cls._pid_file()) as f:
                parts = (f.read().split() + ["", ""])[:3]
            if parts[0].isdigit():
                return int(parts[0]), parts[1], parts[2]
        except OSError:
            pass
        return None, "", ""

    @classmethod
    def _read_pid(cls):
        return cls._read_pid_meta()[0]

    def is_ours(self):
        """占着端口的是不是本工具起的(多探针防串板): pid 文件 + 进程名 + 端口归属三重校验。"""
        pid, dev, sn = self._read_pid_meta()
        if pid and pid in _pids_of("openocd"):
            if sn and self.serial and sn != self.serial:
                return False
            return True
        owner = _port_owner_pid(self.port)
        if owner is None:
            return False
        if owner in _pids_of("openocd"):
            print("    [note] 端口 %d 上的 openocd 不是本工具记录的(可能是上次残留), 按复用处理。" % self.port)
            return True
        return False

    def stop(self):
        pid = self._read_pid()
        if pid:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    import signal
                    os.kill(pid, signal.SIGKILL)
                self._clear_pid()
                return "Stopped OpenOCD GDB server (pid %s)" % pid
            except Exception:
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            return "Stopped OpenOCD GDB server"
        return "No running server"


# ---------------------------------------------------------------------------
# gdb
# ---------------------------------------------------------------------------
def run_gdb(elf, commands, interactive=False, resume=True):
    """执行 gdb 命令。

    resume=True: 收尾恢复目标运行(按后端选命令: J-Link 用 `monitor go`, OpenOCD 用 `monitor resume`)。
      * `detach` 不等于恢复运行(坑#16) —— 读完变量后 CPU 一直停着, 后续 RTT/串口看起来像"板子死了";
      * 不能用 gdb 的 `continue`: --batch 下它会阻塞到目标停下(坑#8), 而 `monitor go` 立即返回。
    """
    gdb = find_gdb()
    if not gdb:
        return "ERROR: 找不到 arm-none-eabi-gdb / gdb-multiarch。请装 STM32CubeCLT 或设置 STM32_DEBUG_GDB。"
    go = "monitor resume" if _SERVER_KIND == "openocd" else "monitor go"
    cmds = ["target remote :%d" % GDB_PORT, "monitor halt"] + commands
    if not interactive:
        if resume:
            cmds += [go]
        cmds += ["detach", "quit"]
    fd, path = tempfile.mkstemp(suffix=".gdb")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(cmds) + "\n")
    argv = [gdb] + ([] if interactive else ["--batch"]) + ["-x", path]
    if elf:
        argv.append(elf)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        return (result.stdout or "") + (result.stderr or "")
    finally:
        os.remove(path)


# ---------------------------------------------------------------------------
# 命令生命周期
# ---------------------------------------------------------------------------
# 已知 STM32 系列 -> J-Link 完整设备名(常用后缀)。用于自动推断时补全。
_DEVICE_SUFFIX = {
    "STM32H743": "STM32H743VI", "STM32H723": "STM32H723ZG", "STM32H750": "STM32H750VB",
    "STM32H7A3": "STM32H7A3VI", "STM32G431": "STM32G431CB", "STM32G474": "STM32G474RE",
    "STM32F427": "STM32F427VI", "STM32F407": "STM32F407VG", "STM32F103": "STM32F103C8",
    "STM32F401": "STM32F401RE", "STM32L432": "STM32L432KC", "STM32L476": "STM32L476RG",
    "STM32F429": "STM32F429ZI", "STM32F746": "STM32F746NG", "STM32F767": "STM32F767ZI",
    "STM32F411": "STM32F411CE", "STM32F446": "STM32F446RE", "STM32G070": "STM32G070RB",
    "STM32U575": "STM32U575ZI", "STM32H563": "STM32H563ZI", "STM32C011": "STM32C011F4",
}


class SkillError(Exception):
    """命令级失败。由 main() 统一转成非零退出码 —— 不再出现"打印 ERROR 却退出 0"。"""


def resolve_device(args, elf=None, explicit=None):
    """确定芯片型号, 拿不到就抛 SkillError(绝不猜封装 —— 猜错会烧错型号)。

    优先级: explicit > --device > 从 ELF 启动文件符号推断。
    """
    dev = explicit or getattr(args, "device", None)
    if not dev:
        dev = infer_device_generic(elf if elf is not None else getattr(args, "elf", None))
    if not dev:
        raise SkillError("无法识别芯片型号: 请显式指定 --device(如 --device STM32H743VI)。"
                         "不猜封装是为了避免烧错型号。")
    return expand_device(dev)


def expand_device(dev):
    """把推断的系列名补全为 J-Link 认识的完整设备名(若能)。"""
    if dev and dev in _DEVICE_SUFFIX:
        return _DEVICE_SUFFIX[dev]
    return dev


def with_server(device, elf, gdb_commands, resume=True, serial=None):
    """连接 GDB server 执行命令。server 已运行则复用(不杀); 自己启动的用后杀。

    优化: 若外部已 start 一个常驻 server, 后续 read/write 复用, 延迟降到 ~180ms。
    resume: 收尾是否 monitor go 恢复运行(默认是, 见坑#16)。
    serial: J-Link 序列号(多探针时必须给, 否则可能连到另一块板)。
    失败一律抛 SkillError -> main 转成非零退出码。
    """
    dev = resolve_device(None, elf, explicit=device)
    sn = serial if serial is not None else _SERIAL
    global _SERVER_KIND
    kind = probe_for_work(verbose=False) or "jlink"
    _SERVER_KIND = "openocd" if kind in ("stlink", "daplink") else "jlink"
    srv = (OpenOCDServer(dev, GDB_PORT, sn, kind) if _SERVER_KIND == "openocd"
           else JLinkServer(dev, GDB_PORT, sn))
    # OpenOCD 常是 .cmd 包装件起的, 杀包装件会留下真进程当孤儿占着探针 -> 记下开工前的名单, 收尾杀差集
    ocd_before = set(_pids_of("openocd")) if _SERVER_KIND == "openocd" else set()
    managed = False
    if srv.is_up():
        if not srv.is_ours():
            raise SkillError("端口 %d 上有一个**不是本工具启动**的 GDB server(可能是别的探针/IDE/另一块板)。"
                             "要么先 stop, 要么确认是残留后用 cleanup --all --force 清掉。" % GDB_PORT)
    else:
        msg = srv.start()
        if msg.startswith("ERROR") or not srv.wait_up(6.0):
            raise SkillError(msg)
        managed = True   # 自己启动的, 用后杀掉
        if srv.proc:
            srv._write_pid(srv.proc.pid)   # 记下来, 后续命令才认得出是本工具启的
    try:
        out = run_gdb(elf, gdb_commands, resume=resume)
        if out.strip().startswith("ERROR:"):
            raise SkillError(out.strip())
        return out
    finally:
        # 仅杀自己启动的; 复用外部 server 时保留(供后续快速读取)
        if managed and srv.proc:
            try:
                srv.proc.terminate()
                srv.proc.wait(timeout=3)
            except Exception:
                try:
                    srv.proc.kill()
                except Exception:
                    pass
            srv._clear_pid()
            if _SERVER_KIND == "openocd":
                for _p in sorted(set(_pids_of("openocd")) - ocd_before):
                    _kill_pid(_p)


def infer_elf():
    for cand in ("build/test.elf", "build/interface-adapter.elf",
                 "build/firmware.elf", "build/*.elf"):
        hits = glob.glob(cand)
        if hits:
            return hits[0]
    return ""


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_doctor(args):
    print("=== STM32 调试环境自检 ===")
    gdb = find_gdb()
    readelf = find_readelf()
    py = find_python()
    rtt = find_rtt_logger()
    jlink = find_jlink_server()
    ocd = find_openocd()
    stcli = find_stm32_cli()
    pyocd = find_pyocd()
    # 这次用哪个调试器: 认出来就按它的工具链检查, 认不出就三家都报一遍(不拦)
    pid, serial, why = "", "", ""
    try:
        pid, serial, why = resolve_probe(args, verbose=False)
    except SkillError as e:
        print("  [!!] 调试器: %s" % e)
    if pid == "jlink":
        status = {
            "J-Link GDB server(调试)": ("OK  " + jlink) if jlink else "缺失 请装 SEGGER J-Link 软件包 (或设 JLINK_GDB_SERVER)",
            "JLinkRTTLogger(可选, RTT 抓包)": ("OK  " + rtt) if rtt else "缺失(可选, 只有 J-Link 抓 RTT 才用)",
        }
    elif pid == "stlink":
        status = {
            "STM32_Programmer_CLI(烧录/校验/救援)": ("OK  " + stcli) if stcli else "缺失 请装 STM32CubeCLT (或设 STM32_PROGRAMMER_CLI)",
            "OpenOCD(调试/断点/读变量)": ("OK  " + ocd) if ocd else "缺失 请装 openocd (或设 OPENOCD)",
        }
    elif pid == "daplink":
        status = {
            "OpenOCD(烧录 + 调试)": ("OK  " + ocd) if ocd else "缺失 请装 openocd (或设 OPENOCD)",
            "pyOCD(可选, 列探针/备用烧录)": ("OK  " + pyocd) if pyocd else "缺失(可选, pip install pyocd)",
        }
    else:
        status = {
            "J-Link 工具": ("OK  " + jlink) if jlink else "缺失(没定调试器时按它检查)",
            "STM32_Programmer_CLI": ("OK  " + stcli) if stcli else "缺失(ST-Link 走它烧录)",
            "OpenOCD": ("OK  " + ocd) if ocd else "缺失(ST-Link/DAPLink 的调试后端)",
        }
    status["arm-none-eabi-gdb"] = ("OK  " + gdb) if gdb else "缺失 请装 STM32CubeCLT 或设 STM32_DEBUG_GDB"
    status["arm-none-eabi-readelf"] = ("OK  " + readelf) if readelf else "缺失(可选, 用于自动识别芯片)"
    status["python3"] = ("OK  " + py) if py else "缺失 请装 python3"
    for k, v in status.items():
        print("  [%s] %s" % ("OK" if v.startswith("OK") else "!!", v))
    if pid:
        if pid == "stlink":
            _v, _vwhy = _stlink_variant_probe()
            if _v:
                print("  [OK] 这一代: %s   (%s)" % (_stlink_profile(_v)["label"], _vwhy))
                print("       SWD 时钟按这代给: %d kHz(OpenOCD) / %d kHz(官方 CLI)" %
                      (_stlink_freq(args, "ocd"), _stlink_freq(args, "cli")))
        print("  [OK] 这次用: %s   (依据: %s%s)" % (PROBE_DEFS[pid]["name"], why,
                  (", 序列号 " + serial) if serial else ""))
    tool_ok = {"jlink": bool(jlink), "stlink": bool(stcli), "daplink": bool(ocd)}.get(pid,
                                                                                     bool(jlink or stcli or ocd))

    # ELF 符号可用性(RTT 控制块 / 黑匣子)
    elf = args.elf or infer_elf()
    if elf and os.path.isfile(elf):
        rtt_sym = symbol_addr_from_elf(elf, "_SEGGER_RTT")
        bb_sym = symbol_addr_from_elf(elf, "g_bb")
        print("  [%s] RTT 控制块 _SEGGER_RTT: %s" % ("OK" if rtt_sym else "!!",
              ("0x%08X" % rtt_sym) if rtt_sym else "未找到(固件没集成 RTT?)"))
        print("  [%s] 黑匣子 g_bb: %s" % ("OK" if bb_sym else "--",
              ("0x%08X" % bb_sym) if bb_sym else "未找到(没接黑匣子, 可选)"))
    stale = _warn_stale_jlink()
    if pid in ("stlink", "daplink"):
        extra = []
        try:
            extra = [("openocd.exe", p) for p in _pids_of("openocd.exe")]
        except Exception:
            extra = []
        if extra:
            print("  [!!] 有残留 OpenOCD 进程(pid %s) -> 跑一次 cleanup 收场" % ", ".join(str(p) for _, p in extra))
        stale = stale + extra
    if not stale:
        print("  [OK] 无残留调试器进程")
    # 必需工具缺失 -> 退出码非零(doctor 的意义就是"环境是否就绪")
    missing_required = [k for k, v in (("arm-none-eabi-gdb", gdb), ("python3", py),
                                       ("调试器工具链", tool_ok)) if not v]
    jset(probe=pid, probe_source=why, serial=serial,
         tools={"jlink_gdb_server": jlink, "stm32_programmer_cli": stcli, "openocd": ocd,
                "pyocd": pyocd, "gdb": gdb, "readelf": readelf,
                "rtt_logger": rtt, "objcopy": find_objcopy()},
         stale_processes=[{"name": n, "pid": p} for n, p in stale])

    # 自动识别芯片
    dev = None
    if args.elf:
        dev = infer_device_generic(args.elf)
        if dev:
            print("--- 从 ELF 识别芯片: %s ---" % dev)
            svd = find_svd(dev)
            print("  SVD: " + (svd if svd else "未找到(不影响调试, 仅寄存器解读)"))
        else:
            print("--- 未能从 ELF 自动识别芯片, 请用 --device 指定 ---")

    # 板子连接提示(探测不可靠, 直接提示)
    print("--- 调试器 / 板子 ---")
    if pid == "jlink":
        print("  J-Link: 烧录用 JLink.exe, 调试/读变量用 JLinkGDBServerCL" + ("" if jlink else "  <- 但没找到, 先装驱动"))
    elif pid == "stlink":
        print("  ST-Link: 烧录/校验走 STM32_Programmer_CLI, 调试/读变量走 OpenOCD"
              + ("" if (stcli and ocd) else "  <- 上面缺的那个要先补上"))
    elif pid == "daplink":
        print("  DAPLink: 烧录和调试都走 OpenOCD" + ("" if ocd else "  <- 但没找到 openocd"))
    else:
        print("  还没定调试器: 先 probe detect 认出插着的那个, 再 probe use <名字>")
    print("  连不上先查: SWD(SWDIO/SWCLK/GND/VCC)接线、板子供电、--device 型号、探针有没有被别的程序占着。")
    print("--- 下一步 ---")
    print("  1) 编译出 ELF: make")
    print("  2) 读变量:   stm32-dev.py read g_motor --elf build/test.elf")
    print("  3) 若自动识别芯片失败: 加 --device STM32H743VI")
    if not pid:
        print("  0) 先定调试器: stm32-dev.py probe detect -> probe use <jlink|stlink|daplink>")
    if missing_required:
        print("!! 缺少必需工具: %s -> 退出码 1" % ", ".join(missing_required))
        return 1
    if not elf or not os.path.isfile(elf or ""):
        print("!! 提示: 没找到 ELF(--elf 可指定), 芯片识别/符号自检被跳过。")
    return 0


# ---------------------------------------------------------------------------
# preflight: 开工前, 拿技能里的结论把「现有工程」扫一遍
#
# 为什么单独一个命令: 这份技能里的坑, 有一大半是「接手别人/自己三个月前的工程,
# 上来就调, 调到一半才发现地基是歪的」。机械能判的直接判掉, 判不了的列成
# 人工对照清单 —— 省的是「踩过了才想起来」的那一遍。
# ---------------------------------------------------------------------------
_PF_SKIP_DIRS = {".git", "build", "Build", "out", "node_modules", "__pycache__",
                 ".venv", "venv", ".dsh", ".idea", ".vscode"}
_PF_TEXT_EXT = {".c", ".h", ".cpp", ".hpp", ".cc", ".mk", ".py", ".ps1", ".bat",
                ".cmd", ".sh", ".jlink", ".ld", ".s", ".S", ".txt", ".cmake"}
_PF_TEXT_NAME = {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "makefile.mk"}

# 故障类 handler: 死循环 = 复现一次、现场全丢(坑 #32)。Error_Handler 不算 ——
# CubeMX 生成的它就是 __disable_irq()+while(1), 报出来只会变成噪声。
# NMI_Handler 也不算 —— CubeMX 同样生成 while(1), 且几乎不会是真实故障现场。
_PF_FAULT_NAMES = ("HardFault_Handler", "MemManage_Handler", "BusFault_Handler",
                   "UsageFault_Handler", "HardFault")

# 机械扫不出来、但开工前必须人工逐条对过的(顺序 = 踩坑代价从高到低)
_PF_MANUAL = [
    ("#81", "设备请求队列是「应答驱动出队」还是「发出即出队」？",
     "发送成功 != 应答成功。发出即出队时, 一次超时就让那条写**静默消失**。"
     "修法: 收到合法应答才出队 + 重发/丢弃计数器(健康值 RETRY 小、DROP 恒 0)。"),
    ("#82/#83", "读回来的数值有没有先做「合理性检查」再喂状态机？",
     "判据: 厂商手册给的最大反馈值 < 该位宽上限 -> 高位是方向位/符号位。"
     "块读必须拿「实际回读字节数」, 不够就不解析 —— 否则读的是未初始化栈。"),
    ("#84", "每个寄存器/参数的单位, 都在手册里查过了吗？",
     "典型: 速度寄存器单位是「50 步/秒 每 LSB」而不是步/秒; 直接写 200 等于请求 10000。"),
    ("#85", "同一个功能有 SRAM / EPROM 两个寄存器吗？写之前要不要解锁？",
     "典型: 0x10 最大扭矩(EPROM, 上电才生效) vs 0x30 转矩限制(SRAM, 立刻生效); 0x37 写锁。"),
    ("#86/#87", "有没有「要 A 得先 B、要 B 得先 A」的门卫？派生状态被缓存了吗？",
     "典型: 「要标定得先在用、要在用得先标定」-> 全新板子永远开不了头。"
     "派生量(如「在用的轴数」)只在 Init 数一次, 后面全部读到陈旧值。"),
    ("#88", "切换工作模式时, 是「参数先写、模式寄存器最后写」吗？目标先锁到当前实际位置了吗？",
     "先写模式 -> 驱动器会拿上一次残留的目标参数立刻动一下。上电应该不动作。"),
    ("#89", "力/力矩/电流这类模拟量的零点, 有现场标定入口并能持久化吗？",
     "空载偏置(-437 g)不是「传感器就这样」, 是没标定。不标定, 力控模式全部偏。"),
    ("#57/#58", "中断和主循环共享的变量, 都是 volatile / 有临界区保护吗？",
     "单核也会烂: 主循环算一半被中断改掉、或读 64 位量读到半新半旧。"),
    ("#32", "有没有「现象只出现一次」的偶发问题还没抓？",
     "先确认黑匣子/故障记录能用, 再谈复现。死循环的 HardFault 等于没记录。"),
]


def _pf_iter_files(root, max_bytes=2 * 1024 * 1024):
    """遍历 root 下值得扫的文本文件, yield (相对路径, 内容)。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _PF_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            if ext not in _PF_TEXT_EXT and fn not in _PF_TEXT_NAME:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > max_bytes:
                    continue
                with io.open(full, "r", encoding="utf-8", errors="replace") as f:
                    yield os.path.relpath(full, root), f.read()
            except (OSError, IOError):
                continue


def _pf_flash_script_issues(text):
    """扫 J-Link 脚本: r 与 g 之间、g 与 exit 之间必须有 Sleep(坑 #80)。

    返回 [(行号, ['r','g',...], 说明)]。
    Makefile/Python 里的脚本是「一个字符串 + \\n」, 所以先把字面 \\n 换成真换行,
    再按行找命令。这样 `printf "loadfile X\\nr\\ng\\nexit\\n"` 也能被拆开看。
    """
    issues = []
    s = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    lines = [l.strip() for l in s.splitlines()]
    n = len(lines)
    stop = ("exit", "qc", "q")
    i = 0
    while i < n:
        if lines[i].lower() == "r":
            j = i + 1
            while j < n and lines[j].lower() not in ("g",) + stop:
                j += 1
            if j < n and lines[j].lower() == "g":
                mid = [x for x in lines[i + 1:j] if x]
                if not any(x.lower().startswith("sleep") for x in mid):
                    issues.append((i + 1, ["r"] + mid + ["g"],
                                   "r 与 g 之间没有 Sleep"))
                k = j + 1
                tail = []
                while k < n:
                    low = lines[k].lower()
                    tail.append(lines[k])
                    if low in stop or low.startswith("sleep"):
                        break
                    k += 1
                if tail and tail[-1].lower() in stop:
                    issues.append((j + 1, ["g"] + tail, "g 之后到 %s 之间没有 Sleep"
                                   % tail[-1].lower()))
                i = j + 1
                continue
        i += 1
    return issues


_PF_LOG_RE = (r"(printf|SEGGER_RTT|RTT_|BlackBox|blackbox|g_bb|fault_report|fault_record|"
              r"record_fault|report_fault|dump_|_dump|BSP_Log|log_printf)")


def _pf_body_has_log(body):
    """函数体里有没有「把现场留下来」的动作(printf / RTT / 黑匣子 / 调故障记录函数)。"""
    return bool(re.search(_PF_LOG_RE, body))


def _pf_fault_handler_defs(text):
    """yield (函数名, 函数体) —— 所有故障 handler 定义; 同名可能有多份(如 #if/#else 两版)。"""
    pat = r"void\s+(%s)\s*\([^)]*\)\s*\{" % "|".join(_PF_FAULT_NAMES)
    for m in re.finditer(pat, text):
        depth, idx, body = 1, m.end(), []
        while idx < len(text) and depth > 0:
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            body.append(ch)
            idx += 1
        yield m.group(1), "".join(body)


def _pf_func_logged(text):
    """粗扫函数定义 -> {函数名: 函数体里有没有现场记录}; 只用来判断「委派」的目标。"""
    out = {}
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\([^;{)]*\)\s*\{", text):
        name, idx, depth = m.group(1), m.end(), 1
        while idx < len(text) and depth > 0:
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            idx += 1
        if _pf_body_has_log(text[m.end():idx]):
            out[name] = True
        out.setdefault(name, False)
    return out


def _pf_iter_fault_handlers(text):
    """yield (函数名, 函数体) —— 只挑「自己没记录、也没委派出去」的故障 handler。"""
    for name, body in _pf_fault_handler_defs(text):
        if not _pf_body_has_log(body) and not re.search(r"\b(?:b|bl)\s+[A-Za-z_]\w*", body):
            yield name, body


def _pf_fault_verdicts(defs, logged_funcs, shcsr_on):
    """故障处理器判决。defs = [(rel, name, body, has_log)]。
    同名多份定义(#if/#else)取最好的一份: 自己记了 > 委派给记录函数 > 异常没使能 > 真报警。
    返回 (hard, soft), 每项 (rel, name, 原因)。"""
    best = {}
    for rel, name, body, has_log in defs:
        m = re.search(r"\b(?:b|bl)\s+([A-Za-z_]\w*)", body)
        tgt = m.group(1) if m else ""
        if has_log:
            rank, why = 3, "handler 自己就把现场留下了"
        elif tgt and (tgt in logged_funcs
                      or re.search(r"(fault|dump|report|blackbox|panic|crash)", tgt, re.I)):
            rank, why = 2, "现场委派给 %s()(按名字判断那边会记, 建议自己确认一次)" % tgt
        elif not name.startswith("HardFault") and not shcsr_on:
            rank, why = 1, "该异常没在 SHCSR 里使能 -> 会升级成 HardFault(那边有记录), 一般不用管"
        else:
            rank, why = 0, "只有死循环, 没有 printf / RTT / 黑匣子, 也没委派给记录函数"
        key = (rel, name)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, why)
    hard = [(r, n, w) for (r, n), (k, w) in sorted(best.items()) if k == 0]
    soft = [(r, n, w) for (r, n), (k, w) in sorted(best.items()) if k in (1, 2)]
    return hard, soft


def cmd_preflight(args):
    """开工前: 拿技能里的结论, 把现有工程扫一遍。"""
    root = os.path.abspath(args.root or ".")
    print("=== 开工前对照检查: 现有工程 vs stm32-dev 技能 ===")
    print("工程根目录: %s" % root)
    if not os.path.isdir(root):
        print("ERROR: 目录不存在: %s" % root)
        return 1

    bad = 0
    flash_files, flash_issues = [], []
    fault_defs, logged_funcs, shcsr_on = [], set(), False
    wdg_files, has_freeze = [], False
    has_noinit, has_bb, has_rtt = False, False, False
    counter_files, diag_files = [], []

    for rel, text in _pf_iter_files(root):
        if "loadfile" in text or rel.lower().endswith(".jlink"):
            iss = _pf_flash_script_issues(text)
            if iss:
                flash_files.append(rel)
                for ln, seq, why in iss:
                    flash_issues.append((rel, ln, why, " ".join(seq)))

        for name, body in _pf_fault_handler_defs(text):
            fault_defs.append((rel, name, body, _pf_body_has_log(body)))
        for fname, flog in _pf_func_logged(text).items():
            if flog:
                logged_funcs.add(fname)
        if "Drivers/" not in rel.replace("\\", "/") and re.search(
                r"SCB->SHCSR|SCB_SHCSR_(?:MEM|BUS|USG)FAULTENA", text):
            shcsr_on = True

        if re.search(r"(HAL_IWDG_Init|HAL_WWDG_Init|IWDG->KR|WWDG->CR|\bHAL_IWDG_Refresh)", text):
            wdg_files.append(rel)
        if re.search(r"__HAL_DBGMCU_FREEZE_(IWDG|WWDG)", text):
            has_freeze = True

        if re.search(r"\.noinit\b", text):
            has_noinit = True
        if re.search(r"\bg_bb\b", text):
            has_bb = True
        if re.search(r"(SEGGER_RTT_|_SEGGER_RTT\b|RTT_printf)", text):
            has_rtt = True
        if re.search(r"(stat_rx_bytes|rx_frames|tx_frames|crc_err|rx_overrun|"
                     r"tx_drop|ore_cnt|drop_cnt)", text):
            counter_files.append(rel)
        if re.search(r"\b(DIAG|DBG)_[A-Z][A-Z0-9_]*", text):
            diag_files.append(rel)

    # ---- 1) 烧录/复位脚本(机械可判定, 直接算失败) ----
    print("--- 1. 烧录 / 复位脚本(坑 #80, #22) ---")
    if flash_issues:
        bad += 1
        for rel, ln, why, seq in flash_issues:
            print("  [!!] %s 第 %d 行附近: %s" % (rel, ln, why))
            print("       序列: %s" % seq)
        print("       修法: r 和 g 后面各补一行 Sleep 1200。")
        print("             正确形态: loadfile <hex> / r / Sleep 1200 / g / Sleep 1200 / exit")
        print("       根因: `g`(resume) 之后紧跟着 `exit`, J-Link 会在 MCU 还没真跑起来时")
        print("             关掉调试会话, CPU 停在复位态 -> 对外总线一个字都不回,")
        print("             现象像「烧完板子就死了」。* JLink 自己不会报任何错。")
        print("       更省事: 别手写 .jlink, 直接用本技能的 flash(按探针自动选路 + 刷后逐字节校验)。")
    else:
        print("  [OK] 未发现 r/g/exit 缺 Sleep 的脚本")
    print("       注意: 除 Makefile 外, CI / 上位机 / IDE 里的 .jlink 也要一起看。")

    # ---- 2) 故障处理器 ----
    print("--- 2. 故障处理器有没有现场记录(坑 #32) ---")
    hard, soft = _pf_fault_verdicts(fault_defs, logged_funcs, shcsr_on)
    if hard:
        bad += 1
        for rel, name, why in hard:
            print("  [!!] %s  %s(): %s" % (rel, name, why))
        print("       修法: python3 stm32-dev.py init-fault 生成 .noinit 黑匣子, 在 handler 里")
        print("             记 CFSR/HFSR/PC/LR/SP; 退一步也要 RTT 打一句。")
        print("       为什么: 死循环 = 复现一次、现场全丢, 只能靠猜。")
    else:
        print("  [OK] 未发现「只死循环」的故障处理器")
    for rel, name, why in soft:
        print("  [--] %s  %s(): %s" % (rel, name, why))

    # ---- 3) 看门狗与调试冻结 ----
    print("--- 3. 看门狗与调试冻结(坑 #59) ---")
    if wdg_files and not has_freeze:
        bad += 1
        print("  [!!] 用了看门狗但项目里没有 __HAL_DBGMCU_FREEZE_IWDG/WWDG")
        print("       涉及: %s" % ", ".join(sorted(set(wdg_files))[:6]))
        print("       修法: 初始化里先 __HAL_RCC_DBGMCU_CLK_ENABLE(), 再")
        print("             __HAL_DBGMCU_FREEZE_IWDG(); 否则一进断点就被狗咬复位,")
        print("             看到的现象是「单步走着走着板子重启了」。")
    elif wdg_files:
        print("  [OK] 有看门狗且有调试冻结")
    else:
        print("  [--] 未发现看门狗(裸 Keil/Makefile 工程常见), 不需要冻结")

    # ---- 4) 观测通道 ----
    print("--- 4. 观测通道(OBSERVE.md) ---")
    print("  [%s] 黑匣子 .noinit 段%s" % ("OK" if has_noinit else "--",
          "" if has_noinit else "  (没有 -> 掉电/重启后上一轮的现场没了)"))
    print("  [%s] 黑匣子变量 g_bb%s" % ("OK" if has_bb else "--", ""))
    print("  [%s] RTT 打印%s" % ("OK" if has_rtt else "--",
          "" if has_rtt else "  (三个 UART 全占时, 调试口只剩 RTT/SWD)"))
    print("  [%s] 逐字节/逐帧计数器%s" % ("OK" if counter_files else "--",
          "" if counter_files else "  (只有「帧数」分不清「没收到」和「收到解不出」)"))
    print("  [%s] 只读诊断寄存器%s" % ("OK" if diag_files else "--", ""))
    if not (has_rtt or counter_files):
        print("       建议: 开工第一件事就是把这三件套建起来 —— RTT 打印 + 收发计数器")
        print("             (rx_bytes / rx_frames / crc_err) + 几个只读 DBG 寄存器。")
        print("             没有它们, 后面每一次「板子没反应」都要重新猜一遍。")

    # ---- 5) 人工对照 ----
    print("--- 5. 机械扫不出来、必须人工逐条对过的 ---")
    for no, q, why in _PF_MANUAL:
        print("  [ ] %-8s %s" % (no, q))
        for line in _pf_wrap(why, 74):
            print("            " + line)

    # ---- 结论 ----
    print("--- 结论 ---")
    if bad:
        print("  %d 项必须处理(上面 [!!])。改完再跑一次 preflight 确认清零。" % bad)
        print("  技能全文: PITFALLS.md(89 条) / PRACTICES.md(正向手册) / OBSERVE.md(观测手段)")
        return 1
    print("  [OK] 机械项全过。请把上面 [ ] 的 9 条人工过一遍再动板子。")
    print("  技能全文: PITFALLS.md(89 条) / PRACTICES.md(正向手册) / OBSERVE.md(观测手段)")
    return 0


def _pf_wrap(s, width):
    """按宽度折行(中英文混排按字符数近似)。"""
    out, cur = [], ""
    for ch in s:
        cur += ch
        if len(cur) >= width or ch == "\n":
            out.append(cur.rstrip())
            cur = ""
    if cur:
        out.append(cur)
    return out


def _resume_cmd():
    """收尾恢复运行用哪条 monitor 命令: J-Link 是 `go`, OpenOCD(ST-Link/DAPLink) 是 `resume`。
    写死 `monitor go` 会让 ST-Link/DAPLink 的 continue 直接失败(OpenOCD 没这条命令)。"""
    kind = _PROBE or _SERVER_KIND        # _PROBE 是这条命令选定的探针, 最权威
    if not kind:
        try:
            kind = probe_for_work()
        except Exception:
            kind = ""
    return "monitor resume" if kind in ("stlink", "daplink") else "monitor go"


_CAST = {1: "unsigned char", 2: "unsigned short", 4: "unsigned int", 8: "unsigned long long"}


def _read_expr(t, size=4):
    """裸地址 -> gdb 能读的表达式; 符号名(或任意 gdb 表达式)原样返回。
    有了它就能读没有 SVD 的东西: DWT / ITM / SCB / TPIU, 例如 0xE0001004(DWT CYCCNT)、
    0xE000ED00(CPUID)、0xE000ED28(CFSR)。"""
    m = re.fullmatch(r"\s*(0[xX][0-9a-fA-F]+|\d+)\s*", t or "")
    if not m:
        return t
    return "*(volatile %s *)%s" % (_CAST.get(int(size or 4), "unsigned int"), m.group(1))


def _write_expr(var, size=4):
    """裸地址 -> gdb 能写的左值; 符号名原样返回。例: write 0xE000EDF0=0xA05F0003"""
    m = re.fullmatch(r"\s*(0[xX][0-9a-fA-F]+|\d+)\s*", var or "")
    if not m:
        return var
    return "*(volatile %s *)%s" % (_CAST.get(int(size or 4), "unsigned int"), m.group(1))


def cmd_read(args):
    elf = args.elf or infer_elf()
    if not elf:
        print("ERROR: 未找到 ELF。请用 --elf 指定, 或先 make。")
        return 1
    size = getattr(args, "size", 4)
    cmds = ["print %s" % _read_expr(t, size) for t in args.targets]
    out = with_server(args.device, elf, cmds, resume=not args.keep_halted)
    print(out)
    jset(targets=list(args.targets),
         values=re.findall(r"\$\d+ = (.+)", out))


def cmd_write(args):
    elf = args.elf or infer_elf()
    if not elf:
        print("ERROR: 未找到 ELF。")
        return 1
    outs = []
    bad = 0
    for t in args.targets:
        if "=" in t:
            var, val = t.split("=", 1)
            lhs = _write_expr(var, getattr(args, "size", 4))
            outs.append(with_server(args.device, elf, ["set %s = %s" % (lhs, val), "print %s" % lhs],
                                    resume=not args.keep_halted))
        else:
            outs.append("!! 用法错误(应为 VAR=value): %s" % t)
            bad += 1
    print("\n".join(o for o in outs if o))
    return 1 if bad else 0


def cmd_break(args):
    elf = args.elf or infer_elf()
    if not elf:
        print("ERROR: 未找到 ELF。")
        return 1
    cmds = (["break %s" % args.point, "info breakpoints"] if args.point else ["info breakpoints"])
    print(with_server(args.device, elf, cmds, resume=not args.keep_halted))


def cmd_contr(args):
    """恢复运行。用 monitor 命令而不是 gdb 的 `continue`:
    --batch 下 `continue` 会一直阻塞到目标停下(坑#8), monitor 命令立即返回。
    J-Link 用 `monitor go`, OpenOCD(ST-Link/DAPLink) 用 `monitor resume` —— 见 _resume_cmd()。"""
    print(with_server(args.device, args.elf or infer_elf(), [_resume_cmd()], resume=False))


def cmd_step(args):
    cmds = ["step"] if args.n <= 1 else ["step %d" % args.n]
    # 单步的意义就是停在每一步, 因此不自动恢复运行
    print(with_server(args.device, args.elf or infer_elf(), cmds, resume=False))


def cmd_info(args):
    print(with_server(args.device, args.elf or infer_elf(),
                      ["info registers", "bt", "info locals"],
                      resume=not args.keep_halted))


def cmd_start(args):
    """启动常驻 GDB server, 供后续 read/write 复用(延迟更低)。按探针选后端。"""
    dev = resolve_device(args)      # 不猜型号: start 也要给对设备名
    kind = probe_for_work(verbose=False) or "jlink"
    if kind in ("stlink", "daplink"):
        srv = OpenOCDServer(dev, GDB_PORT, args.serial or _SERIAL, kind)
    else:
        srv = JLinkServer(dev, GDB_PORT, args.serial or _SERIAL)
    msg = srv.start(detached=True)
    print(msg)
    if msg.startswith("ERROR"):
        if isinstance(srv, OpenOCDServer):
            print(srv.tail_log())
        return 1
    print("常驻 GDB server 已启动(%s)。后续 read/write 将复用, 延迟更低" % kind)
    print("停止: stm32-dev.py stop")
    return 0


def cmd_attach(args):
    dev = resolve_device(args)
    kind = probe_for_work(verbose=False) or "jlink"
    srv = (OpenOCDServer(dev, GDB_PORT, args.serial or _SERIAL, kind) if kind in ("stlink", "daplink")
           else JLinkServer(dev, GDB_PORT, args.serial or _SERIAL))
    msg = srv.start(detached=True)
    print(msg)
    if msg.startswith("ERROR"):
        return 1
    gdb = find_gdb()
    print("启动交互式 GDB: %s %s  然后输入: target remote :%d (server 已常驻)" % (gdb, args.elf or "", GDB_PORT))


def cmd_stop(args):
    """停掉可能存在的常驻 server(J-Link 与 OpenOCD 都试一遍, 谁在停谁)。"""
    msgs = []
    for srv in (JLinkServer(args.device or "STUB", GDB_PORT, ""),
                OpenOCDServer(args.device or "STUB", GDB_PORT, "", "stlink")):
        m = srv.stop()
        if m and m != "No running server":
            msgs.append(m)
    print("\n".join(msgs) if msgs else "No running server")


# 注: cmd_svd 只有一处定义(在文件后半, 支持定位 SVD + 读寄存器解码位域)


def find_jlink_cmd():
    """定位 SEGGER JLink.exe(烧录用)。优先 SEGGER 目录, 避免命中 Java 的 jlink.exe。"""
    # 1) 先从 SEGGER 常见安装目录找确切的 JLink.exe
    for d in WINDOWS_JLINK_DIRS + UNIX_JLINK_DIRS:
        for name in ("JLink.exe", "JLink"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and _looks_like_segger(cand):
                return cand
    # 2) 兜底: PATH 里找, 但排除 Java jlink(太小/路径含 jre)
    p = shutil.which("JLink")
    if p and _looks_like_segger(p):
        return p
    return None


def _looks_like_segger(path):
    """判断是否为 SEGGER 的 JLink 工具(排除 Java jlink / 其它同名)。"""
    low = path.lower()
    # SEGGER 路径特征, 或排除 java/jre 的 jlink
    if "segger" in low:
        return True
    if "\\jre\\" in low or "\\jdk" in low or "\\java" in low:
        return False
    # 文件大小: SEGGER JLink.exe 约 400KB, Java jlink 很小
    try:
        size = os.path.getsize(path)
        if size > 200000:
            return True
    except OSError:
        pass
    return False


def _stlink_conn(serial="", freq=8000, mode=""):
    """拼官方 CLI 的 -c 连接串(参数名/取值以 STM32_Programmer_CLI --help 为准)。"""
    parts = ["port=SWD"]
    if serial:
        parts.append("sn=%s" % serial)
    if freq:
        parts.append("freq=%s" % freq)
    if mode:
        parts.append("mode=%s" % mode)
    return " ".join(parts)


def _stlink_serial(args=None):
    """这次用哪个 ST-Link: --serial > 工程配置 > 环境变量; 没给就空(单探针时不用给)。"""
    s = ((getattr(args, "serial", "") if args else "") or _SERIAL or "").strip()
    return "" if s == DEFAULT_SERIAL else s


def _mk_hex(elf):
    """官方 CLI 烧 hex 最稳: 没有就现场用 objcopy 从 elf 生成一个。"""
    hexfile = os.path.splitext(elf)[0] + ".hex"
    if os.path.isfile(hexfile):
        return hexfile
    oc = find_objcopy()
    if not oc:
        return None
    try:
        p = subprocess.run([oc, "-O", "ihex", elf, hexfile],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except Exception:
        return None
    return hexfile if (p.returncode == 0 and os.path.isfile(hexfile)) else None


class _DryRun(Exception):
    """--dry-run: 只把将要执行的命令打出来, 绝不碰板子。"""
    pass


_DRY_RUN = False


def _dry_stop(argv):
    """--dry-run 时打印命令并抛 _DryRun; 否则返回 False(照常真执行)。
    烧录/复位是仅有的"会让板子动起来"的操作, 所以它们支持先看后跑。"""
    if not _DRY_RUN:
        return False
    print("DRY-RUN: 只打印不执行 -> %s" % " ".join(str(x) for x in argv))
    raise _DryRun()


_STLINK_CLI_DEAD = False     # 这只 ST-Link 官方 CLI 用不了(克隆件/被别的程序占着): 记住, 直接走 OpenOCD
_PROBE_WARNED = False        # "一个调试器都没探测到"这条提示只打一次


def _stlink_channel(args=None):
    """ST-Link 的烧录/校验走哪条软件栈: 官方 CLI 还是 OpenOCD。
    优先级: --via > 环境变量 STM32_DEV_STLINK_CHANNEL > 工程配置 > auto。"""
    # 注意 --via 的默认值就是 "auto"(=没指定), 不能当成"用户明确要求 auto"把环境变量/配置挡掉。
    c = (getattr(args, "via", "") if args else "").strip().lower()
    if c in ("cli", "openocd"):
        return c
    e = os.environ.get("STM32_DEV_STLINK_CHANNEL", "").strip().lower()
    if e in ("cli", "openocd"):
        return e
    try:
        c = str((load_config() or {}).get("stlink_channel", "") or "").strip().lower()
    except Exception:
        c = ""
    return c if c in ("cli", "openocd") else "auto"


def _stlink_cli_blocked(low):
    """官方 CLI 报的是"这只探针用不了"(克隆件/被占), 不是接线或芯片问题 -> 该换通道。"""
    return any(k in low for k in ("dev_connect_err", "not a genuine st device",
                                  "no debug probe detected", "unable to connect to st-link",
                                  "error in initializing st-link device", "st-link is not"))


def _free_stlink_for_cli(verbose=False):
    """官方 CLI 与 OpenOCD 不能同时占同一只 ST-Link: 跑 CLI 前先把我们上次留下的 OpenOCD 收掉。
    真机踩到: 残留 openocd 占着探针 -> CLI 报 DEV_CONNECT_ERR, 而且 -l 会吐一个假串号。"""
    pids = _pids_of("openocd")
    srv = OpenOCDServer("", GDB_PORT, "")
    if srv.is_up():
        srv.stop()
        if verbose:
            print("先把上次留下的 OpenOCD 收掉(它占着探针, 官方 CLI 会连不上)。")
        time.sleep(0.5)
        pids = _pids_of("openocd")
    if pids and verbose:
        print("注意: 还有别的 OpenOCD 在跑(pid %s) —— IDE / ST-LINK_gdbserver 也会抢探针。" %
              ", ".join(str(p) for p in pids[:4]))
    return pids


def _flash_stlink_via_ocd(args):
    """官方 CLI 用不了 -> 换 OpenOCD 走同一只 ST-Link(硬件没变, 只换软件栈)。"""
    global _STLINK_CLI_DEAD
    _STLINK_CLI_DEAD = True
    print("-> 改用 OpenOCD 烧这只 ST-Link(同一根 USB, 只是换个软件栈)。")
    rc = _flash_openocd(args, "stlink")
    if rc == 0:
        merge_config({"stlink_channel": "openocd"})
        print("  已记住: 这只 ST-Link 以后直接走 OpenOCD(想换回官方 CLI: flash --via cli)。")
    else:
        print("  OpenOCD 也没烧成 -> 看上面的 openocd 报错; 先 cleanup 收掉占探针的进程再试。")
    return rc


def add_stlink_args(s):
    """ST-Link 专用开关: 走哪条软件栈 / SWD 时钟给多少。"""
    s.add_argument("--via", choices=["auto", "cli", "openocd"], default="auto",
                   help="ST-Link 烧录/校验走哪条: auto=先试官方 CLI, 用不了自动换 OpenOCD(默认)")
    s.add_argument("--freq", type=int, default=0,
                   help="SWD 时钟 kHz(默认按这只 ST-Link 的世代给最优值: V2 1800 / V3 4000)")


def _flash_stlink(args):
    """ST-Link 后端烧录: STM32_Programmer_CLI -w(写) -v(官方逐字节校验) -rst(复位运行)。
    比 J-Link 强的一点: 校验由官方工具做, 退出码可信, 不用再拉一次 gdb 去比对。
    官方 CLI 用不了这只探针(克隆件/被占)时自动换 OpenOCD, 不用人管。"""
    if _stlink_channel(args) == "openocd" or _STLINK_CLI_DEAD:
        print("ST-Link 走 OpenOCD 通道(这只探针的官方 CLI 用不了, 或你指定了 openocd)。")
        return _flash_openocd(args, "stlink")
    cli = find_stm32_cli()
    if not cli:
        print("ERROR: 找不到 STM32_Programmer_CLI(装 STM32CubeCLT / CubeProgrammer, 或设 STM32_PROGRAMMER_CLI)")
        print("  也可以直接走 OpenOCD 通道: flash --via openocd")
        return _flash_openocd(args, "stlink")
    elf = args.elf or infer_elf()
    target = args.hex or ((os.path.splitext(elf)[0] + ".hex") if elf else None)
    if target and not os.path.isfile(target) and elf and os.path.isfile(elf):
        target = _mk_hex(elf)
    if not target or not os.path.isfile(target):
        print("ERROR: 找不到要烧的文件(--hex 或 ELF): %s" % (target or args.hex or "(无)"))
        return 1
    _free_stlink_for_cli(verbose=True)
    conn = _stlink_conn(_stlink_serial(args), freq=_stlink_freq(args, "cli"),
                        mode="UR" if getattr(args, "ur", False) else "")
    cmd = [cli, "-c", conn, "-w", target]
    if args.no_verify:
        print("VERIFY: 已按 --no-verify 跳过(未校验板子固件)。")
    else:
        cmd.append("-v")
    cmd.append("-rst")
    print("ST-Link 烧录: %s" % " ".join(cmd))
    _dry_stop(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        print("FLASH 超时(ST-Link 没响应; 先跑 cleanup 再试)。")
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-2000:])
    low = out.lower()
    if args.no_verify:
        ok = (r.returncode == 0) and ("error" not in low) and (
            ("download" in low) or ("programming" in low) or ("ok" in low))
    else:
        ok = (r.returncode == 0) and (("verified successfully" in low) or ("download verified" in low))
    if ok:
        print("FLASH OK: %s" % target)
        print("  ST-Link 官方 -v 已逐字节校验; 已复位运行。")
        jset(flashed=True, hex=target, probe="stlink", verified=not args.no_verify)
        return 0
    if _stlink_cli_blocked(low):
        print("ST-Link 官方 CLI 用不了这只探针(克隆件 / 探针被别的程序占着)。")
        if _stlink_channel(args) == "cli":
            print("  你指定了 --via cli, 那就自己处理: 关掉占探针的程序(IDE、ST-LINK_gdbserver), 或换只非克隆的 ST-Link。")
            return 1
        return _flash_stlink_via_ocd(args)
    print("FLASH 失败(原因看上面的官方输出): 探针/接线/供电/读保护。")
    if ("no st-link" in low) or ("no stlink" in low):
        print("  没认到 ST-Link: 换 USB 口/线, 关掉占用它的程序(IDE、stlinkserver)。")
    if ("read out protection" in low) or ("rdp" in low):
        print("  被读保护挡住: %s -c \"%s\" -rdu   (解保护会全片擦除)" % (cli, conn))
    return 1


def _reset_stlink(args):
    """ST-Link 后端复位: 官方 CLI -rst(复位完自动运行); CLI 用不了这只探针时换 OpenOCD。"""
    if _stlink_channel(args) == "openocd" or _STLINK_CLI_DEAD:
        return _reset_openocd(args, "stlink")
    cli = find_stm32_cli()
    if not cli:
        print("ERROR: 找不到 STM32_Programmer_CLI")
        return _reset_openocd(args, "stlink")
    _free_stlink_for_cli(verbose=True)
    conn = _stlink_conn(_stlink_serial(args), freq=_stlink_freq(args, "cli"))
    cmd = [cli, "-c", conn, "-rst"]
    print("ST-Link 复位: %s" % " ".join(cmd))
    _dry_stop(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        print("RESET 超时。")
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-800:])
    ok = (r.returncode == 0) and ("error" not in out.lower())
    if not ok and _stlink_cli_blocked(out.lower()) and _stlink_channel(args) != "cli":
        print("ST-Link 官方 CLI 用不了这只探针 -> 改用 OpenOCD 复位。")
        return _reset_openocd(args, "stlink")
    print("RESET %s (ST-Link)" % ("OK" if ok else "失败"))
    return 0 if ok else 1


def _ocd_prog_argv(probe, serial, device):
    """拼一条 OpenOCD 命令行(烧录/复位用), 不依赖 gdb。"""
    ocd = find_openocd()
    if not ocd:
        print("ERROR: 没找到 openocd(DAPLink 烧录、OpenOCD 调试后端都要它)。")
        print("  装法: winget install xpack-dev-tools.openocd-xpack   或设环境变量 OPENOCD 指向 openocd.exe")
        return None
    tgt = _ocd_target(device)
    if not tgt:
        print("ERROR: 认不出 %s 属于哪个系列, 拼不出 openocd 的目标脚本名。" % device)
        print("  -> 用 --device 给准确型号(例 STM32G431CB)")
        return None
    argv = _argv_for(ocd, "-f", _OCD_IFACE.get(probe, "interface/cmsis-dap.cfg"),
                     "-c", "transport select swd")
    argv += _ocd_speed_arg(probe)
    if serial:
        argv += ["-c", "adapter serial %s" % serial]
    argv += ["-f", "target/%s.cfg" % tgt]
    return argv


def _run_openocd(argv, timeout=300):
    _dry_stop(argv)
    print("  %s" % " ".join(argv))
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:
        print("ERROR: 跑 openocd 失败: %s" % e)
        return None


def _ocd_fail_hint(out):
    low = (out or "").lower()
    if "open failed" in low or "unable to find a matching" in low:
        print("  -> 探针没插好, 或正被别的程序占着(GDB server / 另一个 openocd)。")
    elif "init mode failed" in low or "dp initialisation failed" in low:
        print("  -> 连不上目标芯片: 查供电, 或 SWD 引脚被固件关了(STM32 可以拿 ST-Link 用 mode=UR 救)。")
    elif "no device found" in low:
        print("  -> 芯片没响应: 查上电顺序 / 复位线 / SWDIO 接触。")
    elif not low.strip():
        print("  -> openocd 一句话都没输出, 通常是探针驱动没装好。")
    else:
        print("  -> 上面 Error 那行就是原因; 拿不准就把输出发我。")


def _flash_openocd(args, probe):
    """DAPLink 等探针的烧录路径: 走 OpenOCD 的 program 命令(自带逐字节校验)。"""
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    serial = args.serial if args.serial and args.serial != DEFAULT_SERIAL else ""
    img = elf if (elf and os.path.isfile(elf)) else (_mk_hex(elf) or "")
    if not img:
        print("ERROR: 要烧的文件找不到(既没 ELF 也没 HEX)。")
        print("  -> 加 --elf 指定, 或先 make 一下")
        return 1
    argv = _ocd_prog_argv(probe, serial, dev)
    if not argv:
        return 1
    cmd = 'program "%s"' % img.replace("\\", "/")
    if not args.no_verify:
        cmd += " verify"
    cmd += " reset exit"
    argv += ["-c", cmd]
    print("=== 烧录(%s / OpenOCD): %s ===" % (probe, os.path.basename(img)))
    r = _run_openocd(argv)
    if r is None:
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-1800:])
    ok = (r.returncode == 0 and "error" not in out.lower()
          and (args.no_verify or "verified ok" in out.lower()))
    if not ok:
        print("!! 烧录失败(退出码 %s)。" % r.returncode)
        _ocd_fail_hint(out)
        return 1
    print("OK: 已烧录%s, 并复位运行。" % ("" if args.no_verify else " + 逐字节校验"))
    jset(flashed=True, file=img, probe=probe, verified=not args.no_verify)
    return 0


def _reset_openocd(args, probe):
    """DAPLink 等探针的复位路径: OpenOCD 的 reset run。"""
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    serial = args.serial if args.serial and args.serial != DEFAULT_SERIAL else ""
    argv = _ocd_prog_argv(probe, serial, dev)
    if not argv:
        return 1
    argv += ["-c", "init", "-c", "reset run", "-c", "shutdown"]
    print("=== 复位运行(%s / OpenOCD) ===" % probe)
    r = _run_openocd(argv, timeout=90)
    if r is None:
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or "error" in out.lower():
        print(out[-1200:])
        print("!! 复位失败(退出码 %s)。" % r.returncode)
        _ocd_fail_hint(out)
        return 1
    print("OK: 已复位并恢复运行。")
    jset(reset=True, probe=probe)
    return 0


def cmd_flash(args):
    """烧录 hex/elf 到板子。按探针选路: J-Link 走 JLink.exe, ST-Link 走官方 CLI。"""
    kind = probe_for_work()
    if kind == "stlink":
        return _flash_stlink(args)
    if kind == "daplink":
        return _flash_openocd(args, "daplink")
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    jlink = find_jlink_cmd()
    if not jlink:
        print("ERROR: 找不到 JLink.exe。请装 SEGGER J-Link 驱动。")
        return 1
    # 找 hex
    hexfile = args.hex or (elf and os.path.splitext(elf)[0] + ".hex")
    if not hexfile or not os.path.isfile(hexfile):
        print("ERROR: 找不到 hex 文件: %s" % hexfile)
        return 1
    # * 两个 Sleep 不能删(坑#80)!
    #   原来写的是 "loadfile ...\nr\ng\nexit\n": `g`(resume) 之后紧跟着 `exit`,
    #   J-Link 会在 MCU 还没真正跑起来时关掉调试会话, 结果是 CPU 停在复位后的状态
    #   —— 程序不跑, 串口/485 一个字都不回, 现象像是"烧完板子就死了"。
    #   实测: `r g exit` = 0/24 应答; `r Sleep 1200 g Sleep 1200 exit` = 24/24。
    #   四种写法 JLink 都不报错, 只能靠"烧完能不能通信"分辨。
    _dry_stop([jlink, "-device", dev, "-if", "SWD", "-speed", "4000", "-autoconnect", "1",
               "-CommanderScript", "<临时脚本: loadfile %s; r; Sleep 1200; g; Sleep 1200; exit>" % hexfile.replace("\\", "/")])
    fd, script = tempfile.mkstemp(suffix=".jlink")
    with os.fdopen(fd, "w") as f:
        f.write("loadfile %s\nr\nSleep 1200\ng\nSleep 1200\nexit\n" % hexfile.replace("\\", "/"))
    gui_before = _jlink_gui_pids()   # JLink.exe 退出会留下 JLinkGUIServer, 收尾要清
    rc = 1          # 默认按失败算, 只有确认成功才置 0
    try:
        r = subprocess.run(
            [jlink, "-device", dev, "-if", "SWD", "-speed", "4000",
             "-autoconnect", "1", "-CommanderScript", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        out = (r.stdout or "") + (r.stderr or "")
        print(out[-1500:])
        if "O.K." in out or "Downloading file" in out:
            print("FLASH OK: %s -> %s" % (hexfile, dev))
            jset(flashed=True, hex=hexfile, device=dev)
            rc = 0
            # 刷后校验是这条命令的"安全网": 只要没真验过(不一致 / 跳过 / 没 ELF),
            # 整体就算失败(rc=1)。确实不想校验必须显式 --no-verify。
            if args.no_verify:
                print("VERIFY: 已按 --no-verify 跳过(未校验板子固件)。")
            elif not elf or not os.path.isfile(elf):
                print("VERIFY: 没有 ELF, 无法校验板子固件。")
                print("  -> 要么给出 --elf, 要么显式加 --no-verify 表示不校验。")
                rc = 1
            else:
                ok, msg = _do_verify(elf, dev)
                print("VERIFY: " + (msg if msg else "跳过"))
                if ok is True:
                    pass
                elif ok is False:
                    print("  -> 板子可能还在跑旧固件(坑#9), 重烧后再验。")
                    rc = 1
                else:
                    print("  -> 校验没能完成(上一条已说明原因), 按失败处理;")
                    print("     若确实不需要校验, 请显式加 --no-verify。")
                    rc = 1
        else:
            print("FLASH 可能失败, 检查 J-Link 连接/供电。")
            rc = 1
    except subprocess.TimeoutExpired:
        print("FLASH 超时(J-Link 无响应)。")
        rc = 1
    finally:
        os.remove(script)
        for pid in (_jlink_gui_pids() - gui_before):
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
            except Exception:
                pass
    return rc


def _eval_on_target(args, expr):
    """在板子上求值一个 C 表达式, 返回 True / False / None(无法判定)。

    gdb 的 print 输出形如 "$1 = true" / "$1 = 0" / "$1 = 5"。
    """
    return _parse_bool(with_server(args.device, args.elf, ["print %s" % expr]))


def cmd_build_verify(args):
    """编译-烧录-读变量 循环 (debug 生命周期闭环辅助)。

    --check 是**真断言**: 在板上求值该 C 表达式, 一旦为真就提前成功退出(0);
    跑满 --max-iter 仍不为真 -> 失败(1)。不给 --check 时单纯重复 N 次, 成功返回 0。
    """
    build_cmd = args.build_cmd or "make"
    flash_cmd = args.flash_cmd or "make flash"
    for it in range(args.max_iter):
        print("\n=== 迭代 %d/%d ===" % (it + 1, args.max_iter))
        for cmd in (build_cmd, flash_cmd):
            print(">> " + cmd)
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            except subprocess.TimeoutExpired:
                print("超时(180s): %s" % cmd)
                return 1
            if r.returncode != 0:
                print((r.stdout or "")[-1500:] + (r.stderr or "")[-1500:])
                print("失败: %s" % cmd)
                return 1
        time.sleep(args.wait)
        if args.read:
            out = with_server(args.device, args.elf,
                              ["print %s" % v for v in args.read])
            print(out)
        if args.check:
            verdict = _eval_on_target(args, args.check)
            if verdict is True:
                print("CHECK PASS: %s 为真 -> 闭环成功, 第 %d 轮结束" % (args.check, it + 1))
                jset(check=args.check, check_result=True, iterations=it + 1)
                return 0
            if verdict is None:
                print("CHECK: 无法判定 %s 的真假(看上面的 gdb 输出), 继续下一轮" % args.check)
            else:
                print("CHECK: %s 仍为假, 继续下一轮" % args.check)
    if args.check:
        print("\n=== %d 轮跑完, %s 始终不为真 -> 失败 ===" % (args.max_iter, args.check))
        jset(check=args.check, check_result=False, iterations=args.max_iter)
        return 1
    print("\n=== 循环结束 (%d 次) ===" % args.max_iter)
    return 0


# ===== OpenOCD RTT 抓包: ST-Link / DAPLink 走这条 =====
# 实测(xPack OpenOCD 0.12, 先 -f target/xxx.cfg 再 help rtt):
#   rtt setup <address> <size> [ID] / rtt start / rtt stop
#   rtt server start <port> <channel> [message] / rtt server stop <port>
#   rtt channels / rtt channellist / rtt polling_interval
# 注意: rtt setup / rtt start 挂在 target 上, 必须 -f target/xxx.cfg 之后才存在
# (裸跑 openocd -c "help rtt" 只有 rtt server start/stop, 别据此以为没有这功能)。
# init 语义(读 openocd 源码 src/openocd.c 确认): handle_init_command 里有 static initialized
# 守卫 -> 显式 -c "init" 之后, 开机那次自动 init 是空操作, 不会初始化两遍。
_RTT_TCP_PORT = 9090


def _rtt_tcp_port(args):
    """OpenOCD RTT server 监听的本地 TCP 端口。"""
    return int(getattr(args, "rtt_port", 0) or _RTT_TCP_PORT)


def _rtt_addr_span(args, elf):
    """给 OpenOCD 的 rtt setup 定搜索窗口: 返回 (addr, size); 地址解析失败返回 None。

    手册原文: "OpenOCD searches for a control block with the identifier ID starting at
    the memory address address within the next size bytes" —— 所以给的是**搜索窗口**。
    有 ELF 符号 _SEGGER_RTT 就开小窗口(控制块就在那儿); 没有就按 --search 或全 RAM 搜。
    """
    if args.address:
        try:
            return int(str(args.address), 0), 0x400
        except ValueError:
            return None
    sym = symbol_addr_from_elf(elf, "_SEGGER_RTT")
    if sym:
        print("RTT 控制块地址: 0x%08X (ELF 符号 _SEGGER_RTT)" % sym)
        return sym, 0x400
    if getattr(args, "search", None):
        try:
            return int(str(args.search[0]), 0), int(str(args.search[1]), 0)
        except ValueError:
            return None
    print("提示: 未从 ELF 解析到 _SEGGER_RTT -> 在 0x20000000 起 128KB 内搜控制块(慢一些)")
    return 0x20000000, 0x20000


def _ocd_rtt_argv(ocd, probe, serial, device, addr, size, tcpport, channel):
    """OpenOCD RTT 抓包命令行: setup(搜控制块) -> start(开始轮询) -> server(转成 TCP 流)。

    末尾 catch {resume} 是必需的: init 之后核通常停在 halt(和 J-Link 的坑#16 同源),
    不 resume 就只能读到缓冲区里的旧快照; 而核本来就在跑时 resume 会报错,
    所以要用 Jim Tcl 的 catch 兜住(实测: 核不在跑时 catch {resume} 返回 1 且不中断命令行)。
    """
    tgt = _ocd_target(device)
    iface = _OCD_IFACE.get(probe)
    if not tgt or not iface:
        return None
    argv = _argv_for(ocd, "-f", iface, "-c", "transport select swd")
    if serial:
        argv += ["-c", "adapter serial %s" % serial]
    argv += ["-f", "target/%s.cfg" % tgt,
             "-c", "init",
             "-c", 'rtt setup 0x%08X 0x%X "SEGGER RTT"' % (addr, size),
             "-c", "rtt start",
             "-c", "rtt server start %d %d" % (tcpport, channel),
             "-c", "catch {resume}"]
    return argv


def _kill_pid(pid):
    """强杀一个 pid(Windows 用 taskkill; 失败静默)。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _rtt_capture_ocd(args, probe):
    """ST-Link / DAPLink 抓 RTT: 起一个 openocd, 用它的 RTT server 把上行通道转成本地 TCP 流。

    与 J-Link 的差别: openocd 的 RTT server 和 gdb server 是同一个进程(不用像 J-Link 那样
    在 logger 与 GDBServer 之间二选一), 但同一台探针同一时刻只能被一个进程用 ->
    起之前先把常驻 server 停掉。抓完把 openocd 连同 cmd 包装一起杀干净, 否则探针一直被占。
    返回 (outfile, logfile) 或 None(失败原因已打印)。
    """
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    span = _rtt_addr_span(args, elf)
    if span is None:
        print("ERROR: 地址解析失败(要 0x 开头的十六进制): %s" % (args.address or args.search))
        return None
    addr, size = span
    ocd = find_openocd()
    if not ocd:
        print("ERROR: 找不到 OpenOCD —— ST-Link/DAPLink 抓 RTT 靠它。装法见 SETUP.md, 或设 OPENOCD 环境变量。")
        return None
    sn = args.serial or _SERIAL or ""
    port = _rtt_tcp_port(args)
    argv = _ocd_rtt_argv(ocd, probe, sn, dev, addr, size, port, args.channel)
    if not argv:
        print("ERROR: 认不出 %s 属于哪个 STM32 系列, OpenOCD 需要 -f target/<系列>x.cfg。" % dev)
        print("       -> 用 --device STM32G431CBT6 这样的完整订货号, 或给 --elf。")
        return None
    for srv in (JLinkServer(dev, GDB_PORT, sn), OpenOCDServer(dev, GDB_PORT, sn, probe)):
        try:
            if srv.is_up():
                print("检测到常驻 GDB server -> 先 stop(它和 RTT 抓包抢同一台探针)")
                srv.stop()
                time.sleep(0.5)
        except Exception:
            pass
    outfile = args.out or os.path.join(
        tempfile.gettempdir(), "stm32-dev-rtt-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    logfile = os.path.join(tempfile.gettempdir(), "stm32-dev-openocd-rtt.log")
    for p in (outfile, logfile):
        try:
            os.remove(p)
        except OSError:
            pass
    print("== RTT 抓包(OpenOCD + %s): device=%s channel=%d %.1fs -> %s =="
          % (PROBE_DEFS.get(probe, {}).get("name", probe), dev, args.channel, args.seconds, outfile))
    print("   " + " ".join(argv))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    before = set(_pids_of("openocd"))
    lf = open(logfile, "wb")
    proc = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, creationflags=flags)
    sk = None
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            sk = socket.create_connection(("127.0.0.1", port), 0.5)
            break
        except OSError:
            time.sleep(0.3)
    if sk is None:
        print("!! OpenOCD 没能把 RTT 服务挂到 127.0.0.1:%d(进程%s)。"
              % (port, "已退出" if proc.poll() is not None else "还在跑"))
        tail = _tail_text(logfile)
        if tail:
            print("   后台日志: %s" % tail)
        else:
            print("   后台日志是空的(openocd 被强杀时缓冲没落盘)。手动跑一遍看现场:")
            print("     " + " ".join(argv))
        _kill_pid(proc.pid)
        for pid in (set(_pids_of("openocd")) - before):
            _kill_pid(pid)
        lf.close()
        return None
    print("已连上 RTT 通道 %d, 开始接收 %.1fs ..." % (args.channel, args.seconds))
    buf = bytearray()
    sk.settimeout(0.3)
    t_end = time.time() + max(1.0, args.seconds)
    while time.time() < t_end:
        try:
            chunk = sk.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    try:
        sk.close()
    except OSError:
        pass
    # 收场: cmd /c 包了一层(openocd.CMD), 只杀 cmd 会留下 openocd.exe 占着探针。
    _kill_pid(proc.pid)
    for pid in (set(_pids_of("openocd")) - before):
        _kill_pid(pid)
    try:
        lf.close()
    except Exception:
        pass
    with open(outfile, "wb") as fh:
        fh.write(bytes(buf))
    return (outfile, logfile)


def _rtt_report(args, outfile, logfile=None, hints=None):
    """RTT 抓包结果的统一判读(J-Link 与 OpenOCD 两条路共用)。

    退出码: 没抓到任何数据 = 1; --check-seq 判定丢帧 = 1; 正常 = 0。
    """
    data = ""
    if os.path.isfile(outfile):
        try:
            with open(outfile, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError:
            pass
    lines = [l for l in data.splitlines() if l.strip()]
    if not lines:
        print("!! 没抓到任何 RTT 数据。排查顺序(坑#27/#29/#30):")
        print("   1) 固件里是否真的集成了 SEGGER RTT 并调用了写接口(探针支持 RTT 不等于固件有);")
        print("   2) 控制块搜索失败 -> 加 --search <起始地址> <长度>(如 --search 0x20000000 0x20000);")
        print("   3) 调试器是否被其它进程占用(先 stop, 关掉 RTT Viewer/IDE/另一个 gdb server);")
        print("   4) M7/M55 开 D-Cache 时缓冲区需在非缓存区;")
        print("   5) 搜索命中了 RAM 里残留的旧控制块(换过固件/改过缓冲区位置) -> 用 --address 显式指定。")
        for h in (hints or []):
            print("   " + h)
        if logfile and os.path.isfile(logfile):
            try:
                with open(logfile, "r", encoding="utf-8", errors="replace") as f:
                    print("   --- 后台日志尾部 ---")
                    print("\n".join(f.read().splitlines()[-15:]))
            except OSError:
                pass
        return 1
    print("抓取到 %d 行 / %.1f 秒" % (len(lines), args.seconds))
    jset(outfile=outfile, lines=len(lines), seconds=args.seconds, address=args.address)
    if len(lines) <= 2:
        print("!! 行数极少 -> 疑似 CPU 处于 halt 状态(只拿到缓冲区快照, 没有新数据)。")
        print("   -> python stm32-dev.py continue    # 恢复运行后重抓")
    if args.check_seq:
        res = analyze_increasing_seq(data)
        if res is None:
            print("--check-seq: 没找到递增计数列(固件里最好带一个自增序号)")
        else:
            k, n, mx, gaps, lead, base = res
            print("--check-seq: 字段 %s, %d 个样本, 正常步长 %d, 稳态最大跳变 %d, 超步长 %d 次 -> %s"
                  % (k, n, base, mx, gaps, "无丢帧" if gaps == 0 else "有丢帧/混入旧数据"))
            jset(seq_check={"field": k, "samples": n, "base_delta": base,
                            "max_delta": mx, "gaps": gaps, "ok": gaps == 0})
            if lead > 1:
                print("            开头追赶区最大跳变 %d (缓冲区旧内容->实时数据, 不计入丢帧)" % lead)
            if gaps != 0:
                print("!! --check-seq 判定失败: 检测到丢帧, 退出码非零。")
                return 1
            print("--check-seq 判定通过: 序号严格递增, 无丢帧。")
    print("--- 开头 ---")
    for l in lines[:5]:
        print("  " + l)
    if len(lines) > 10:
        print("--- 结尾 ---")
        for l in lines[-5:]:
            print("  " + l)
    print("提示: 验收看序号/时间戳是否严格递增且无跳变(丢帧), 以及 attach 后是否从上次数值继续(未复位/未 halt)。")
    return 0


# RTT 地址自愈用的默认 RAM 搜索范围(控制块一定在 RAM 里)。
_RTT_SEARCH_DEFAULT = "0x20000000 0x20000"


def _rtt_empty(path):
    """抓包文件里有没有真实数据(不存在 / 只有 BOM 与空白 都算空)。"""
    try:
        with io.open(path, "rb") as fh:
            return len(fh.read().strip(b"\xef\xbb\xbf \r\n\t")) == 0
    except OSError:
        return True


def _rtt_capture_jlink(args, dev, logger, addr_args, tag=""):
    """跑一次 JLinkRTTLogger, 并把收尾清理做干净; 返回 (outfile, logfile)。

    每次抓包前先停掉常驻 GDB server: RTT 客户端与 GDB server 独占同一台 J-Link。
    每次用唯一文件名: JLinkRTTLogger 对已存在的文件是"追加"行为,
    复用固定路径会把上次的数据混进来, 造成"数据跨了几百秒"的假象。
    """
    srv = JLinkServer(dev, GDB_PORT, getattr(args, "serial", None) or _SERIAL)
    if srv.is_up():
        print("检测到常驻 GDB server 占用 J-Link -> 先 stop(它与 RTT 客户端互斥)")
        srv.stop()
        time.sleep(0.5)
    outfile = args.out or os.path.join(
        tempfile.gettempdir(), "stm32-dev-rtt-%s%s.log" % (time.strftime("%Y%m%d-%H%M%S"), tag))
    logfile = os.path.join(tempfile.gettempdir(), "stm32-dev-rtt-logger.log")
    for f in (outfile, logfile):
        try:
            os.remove(f)
        except OSError:
            pass
    argv = [logger, "-Device", dev, "-If", "SWD", "-Speed", "4000",
            "-RTTChannel", str(args.channel)] + list(addr_args)
    argv.append(outfile)
    print("== RTT 抓包: device=%s channel=%d %.1fs -> %s ==" % (dev, args.channel, args.seconds, outfile))
    _warn_stale_jlink()              # 起客户端前先看有没有别的进程占着设备
    gui_before = _jlink_gui_pids()   # JLinkRTTLogger 会连带拉起 JLinkGUIServer, 收尾要一起清
    proc = subprocess.Popen(argv, stdout=open(logfile, "w"), stderr=subprocess.STDOUT)
    deadline = time.time() + max(1.0, args.seconds)
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.2)
    try:
        proc.terminate()
        proc.wait(timeout=2)     # 客户端收到终止后可能还写几秒文件, 不宜久等
    except Exception:
        pass
    if proc.poll() is None:      # 没死透就强杀, 否则它会继续占着 J-Link/继续写文件
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    # 清理本次新拉起的 JLinkGUIServer: 否则它会一直占着 J-Link, 后续 gdb/抓包全部卡死。
    for pid in (_jlink_gui_pids() - gui_before):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
        except Exception:
            pass
    return outfile, logfile


def cmd_rtt(args):
    """抓取 RTT 上行通道(只读)。通用: 只要目标固件已集成 SEGGER RTT 即可。

    走哪条路由探针决定: J-Link 用 JLinkRTTLogger(要独占探针, 抓包前先 stop),
    ST-Link/DAPLink 用 OpenOCD 的 rtt server(见 PROBES.md)。
    """
    probe = probe_for_work()
    if probe in ("stlink", "daplink"):
        got = _rtt_capture_ocd(args, probe)
        if not got:
            print("提示: 也可以先用别的通道观测(serial 串口探针帧 / .noinit 黑匣子), 它们和探针无关。")
            return 1
        return _rtt_report(args, got[0], got[1],
                           hints=["OpenOCD 侧常见原因: 日志里出现 rtt: control block not found(固件没集成 RTT 或记错地址);",
                                  "                        核一直停在 halt(没 resume 成功);",
                                  "                        探针被别的进程占着(两个 stlinkserver / CubeProgrammer GUI)。"])
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    # 地址优先: --address > ELF 符号 _SEGGER_RTT > 自动搜索(慢, 实测 10s+)
    if not args.address and not args.search:
        sym = symbol_addr_from_elf(elf, "_SEGGER_RTT")
        if sym:
            args.address = "0x%08X" % sym
            print("RTT 控制块地址: %s (ELF 符号 _SEGGER_RTT)" % args.address)
        else:
            print("提示: 未从 ELF 解析到 _SEGGER_RTT, 退回自动搜索(可能 10s+); 建议 --elf 或 --address")
    # 抓包前确保 CPU 在跑: gdb 读变量后 detach 不会恢复运行(坑#16), 此时 RTT 只有缓冲区快照。
    if not args.no_resume:
        with_server(dev, elf, ["monitor go"])
    # RTT 客户端与 GDB server 独占同一台 J-Link: 有常驻 server 就先停掉, 否则 logger 打不开设备。
    srv = JLinkServer(dev, GDB_PORT, getattr(args, "serial", None) or _SERIAL)
    if srv.is_up():
        print("检测到常驻 GDB server 占用 J-Link -> 先 stop(它与 RTT 客户端互斥)")
        srv.stop()
        time.sleep(0.5)
    logger = find_rtt_logger()
    if not logger:
        print("ERROR: 找不到 JLinkRTTLogger。请装 SEGGER J-Link 驱动, 或设 JLINK_RTT_LOGGER 环境变量。")
        return 1
    addr_args = ()
    if args.address:
        addr_args = ("-RTTAddress", str(args.address))
    elif args.search:
        addr_args = ("-RTTSearchRanges", "%s %s" % (args.search[0], args.search[1]))
    outfile, logfile = _rtt_capture_jlink(args, dev, logger, addr_args)
    # 地址自愈: 用 ELF 符号地址一条数据都没抓到 -> 板子跑的多半不是这份 ELF(坑#9),
    # 符号地址属于别的构建; 这时改用 RAM 搜索再抓一次, 而不是直接报"没抓到数据"。
    if not (addr_args[:1] == ("-RTTSearchRanges",)) and _rtt_empty(outfile):
        why = ("地址 %s(来自 ELF 符号 _SEGGER_RTT)" % args.address) if args.address else "自动搜索"
        print("!! %s 没抓到任何数据:" % why)
        print("   最常见原因: 板子跑的不是这份 ELF(坑#9, 符号地址是旧构建的布局); 或固件没写 RTT。")
        print("   自动改用 RAM 搜索再抓一次(%s)..." % _RTT_SEARCH_DEFAULT)
        if not args.no_resume:
            with_server(dev, elf, ["monitor go"])
        outfile, logfile = _rtt_capture_jlink(
            args, dev, logger, ("-RTTSearchRanges", _RTT_SEARCH_DEFAULT), tag="-rescan")
    return _rtt_report(args, outfile, logfile)


def _warn_stale_jlink():
    """开设备前检查会**独占** J-Link 的残留进程并告警。

    实测: JLinkGUIServer 不独占设备(它在场时 gdb read / RTT 抓包均正常), 因此不计入告警;
    真正独占的是 JLinkRTTLogger / JLinkGDBServer 这类正在用设备的进程。
    """
    stale = []
    for n in ("JLinkRTTLogger", "JLinkGDBServerCL", "JLinkGDBServer", "JLinkRemoteServer", "openocd"):
        stale += [(n, p) for p in _pids_of(n)]
    if stale:
        print("!! 检测到残留调试进程(J-Link / OpenOCD, 会独占探针, 后续命令会卡死):")
        for n, pid in stale:
            print("     %-22s pid=%d" % (n, pid))
        print("   -> 先执行: python stm32-dev.py cleanup")
    return stale


def cmd_cleanup(args):
    """清理残留的 J-Link 进程, 释放被独占的 J-Link。

    症状: 后续 gdb/抓包全部卡死(不是板子死机)。常见残留:
      JLinkRTTLogger / JLinkGUIServer(RTT 客户端连带拉起) / JLinkGDBServerCL。

    默认只清理由本工具启动、并记录在 pid 文件里的那个 server(进程名+PID 双重校验),
    其它 J-Link 进程只报告不动手(可能是 IDE / 别的板卡 / 别人的会话)。
    要全局清场必须显式加 --all --force。
    """
    names = ("JLinkRTTLogger", "JLinkGUIServer", "JLinkGDBServerCL", "JLinkGDBServer",
             "JLinkRemoteServer", "JLinkRemoteServerCL", "openocd")
    own_pid = JLinkServer._read_pid() or OpenOCDServer._read_pid()
    own = None
    if own_pid:
        for n in names:
            if own_pid in _pids_of(n):
                own = (n, own_pid)
                break

    if args.all:
        if not args.force:
            print("!! --all 会强杀所有 J-Link 相关进程(包括 IDE、别的板卡的会话)。")
            print("   确认要这么做, 请加 --force:  <skill>/scripts/stm32-dev.py cleanup --all --force")
            return 1
        targets = [(n, pid) for n in names for pid in _pids_of(n)]
    else:
        targets = [own] if own else []
        others = sorted({(n, pid) for n in names for pid in _pids_of(n) if (n, pid) != own})
        if others:
            print("检测到其它 J-Link 进程(默认不动它们, 可能是 IDE 或别的会话):")
            for n, pid in others:
                print("     %-22s pid=%d" % (n, pid))
            print("   -> 确认都是残留、且没有别的会话在用, 才执行: cleanup --all --force")

    killed = []
    for n, pid in targets:
        if args.dry_run:
            killed.append((n, pid, "(dry-run)"))
        else:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
                killed.append((n, pid, "killed"))
            except Exception as e:
                killed.append((n, pid, "kill failed: %s" % e))
    # 常驻 server 的 pid 文件也清掉, 避免下次误判"已运行"
    if not args.dry_run and own:
        try:
            os.remove(JLinkServer._pid_file())
        except OSError:
            pass
    if not killed:
        print("没有需要清理的进程(本工具自己启的 server 没在跑)。")
        return 0
    for n, pid, st in killed:
        print("  %-22s pid=%-7d %s" % (n, pid, st))
    print("共 %d 个。" % len(killed))
    jset(killed=[{"name": n, "pid": p, "result": s} for n, p, s in killed])
    return 0


def _port_owner_pid(port):
    """谁在监听这个 TCP 端口(Windows 用 netstat -ano)。返回 PID 或 None。"""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=15).stdout or ""
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":" + str(port)):
                try:
                    return int(parts[4])
                except ValueError:
                    return None
    return None


def _pids_of(imagename):
    """按进程名取 PID(Windows 用 tasklist, 其它平台用 pgrep)。"""
    pids = []
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % imagename,
                                  "/NH", "/FO", "CSV"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout or ""
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower().startswith(imagename.lower()):
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
        else:
            out = subprocess.run(["pgrep", "-f", imagename], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout or ""
            pids = [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        pass
    return pids


# ---------------------------------------------------------------------------
# 黑匣子 / 故障转储模板
# ---------------------------------------------------------------------------
_BB_MAGIC = 0x424C4B31   # "BLK1"

_BLACKBOX_H = r'''#ifndef BLACKBOX_H
#define BLACKBOX_H

#include <stdint.h>

/* 故障标签 */
#define BB_TAG_HARDFAULT 1u
#define BB_TAG_ERROR     2u

/* 黑匣子记录: 放在 .noinit 段(复位不清零), 可跨复位保留故障现场。
   布局固定 13 个字 —— 技能工具 'blackbox' 命令按此布局解码。 */
typedef struct
{
  uint32_t magic;        /* 有效标记 0x424C4B31 */
  uint32_t boot_count;   /* 本次上电后的启动次数 */
  uint32_t crash_flag;   /* 1 = 上一次运行发生了崩溃(启动时被读取并清零, 想保留历史就先读再清) */
  uint32_t tag;          /* BB_TAG_* */
  uint32_t cfsr, hfsr, bfar, mmar;
  uint32_t pc, lr, psr;
  uint32_t tick;         /* 崩溃时刻 HAL_GetTick */
  uint32_t seq;          /* 崩溃时刻应用侧进度计数 */
} blackbox_t;

void              BlackBox_Init(void);                                  /* 启动时调用(必须在 RTT/串口初始化之后) */
void              BlackBox_Record(uint32_t tag, const uint32_t *frame); /* 故障时调用, frame 可传 0 */
void              BlackBox_SetSeq(uint32_t seq);                        /* 应用侧进度(可选, 极廉价) */
uint32_t          BlackBox_BootCount(void);
const blackbox_t *BlackBox_Get(void);

#endif /* BLACKBOX_H */
'''

_BLACKBOX_C = r'''/* 黑匣子: 故障现场跨复位保存。
   记录区放 .noinit 段 -> 启动代码不清零 -> 复位后仍可读。
   通道默认 RTT, 脱机场景可改成串口探针帧。 */
#include "main.h"
#include "SEGGER_RTT.h"
#include "blackbox.h"

#define BB_MAGIC 0x424C4B31u   /* "BLK1" */

static blackbox_t g_bb __attribute__((section(".noinit")));

void BlackBox_Init(void)
{
  if (g_bb.magic == BB_MAGIC)
  {
    ++g_bb.boot_count;
    if (g_bb.crash_flag != 0u)
    {
      SEGGER_RTT_printf(0,
        "\r\n== BLACKBOX: last crash tag=%u cfsr=%08X hfsr=%08X bfar=%08X mmar=%08X\r\n"
        "   pc=%08X lr=%08X psr=%08X tick=%u seq=%u boot_count=%u\r\n",
        (unsigned)g_bb.tag, (unsigned)g_bb.cfsr, (unsigned)g_bb.hfsr,
        (unsigned)g_bb.bfar, (unsigned)g_bb.mmar,
        (unsigned)g_bb.pc, (unsigned)g_bb.lr, (unsigned)g_bb.psr,
        (unsigned)g_bb.tick, (unsigned)g_bb.seq, (unsigned)g_bb.boot_count);
      g_bb.crash_flag = 0u;          /* 只报一次 */
    }
  }
  else                               /* 冷启动或 RAM 被清: 初始化 */
  {
    g_bb.magic      = BB_MAGIC;
    g_bb.boot_count = 0u;
    g_bb.crash_flag = 0u;
    g_bb.tag        = 0u;
    g_bb.seq        = 0u;
  }
}

void BlackBox_Record(uint32_t tag, const uint32_t *frame)
{
  g_bb.magic      = BB_MAGIC;
  g_bb.crash_flag = 1u;
  g_bb.tag        = tag;
  g_bb.cfsr       = SCB->CFSR;
  g_bb.hfsr       = SCB->HFSR;
  g_bb.bfar       = SCB->BFAR;
  g_bb.mmar       = SCB->MMFAR;
  g_bb.pc         = (frame != 0) ? frame[6] : 0u;
  g_bb.lr         = (frame != 0) ? frame[5] : 0u;
  g_bb.psr        = (frame != 0) ? frame[7] : 0u;
  g_bb.tick       = HAL_GetTick();
}

void BlackBox_SetSeq(uint32_t seq) { g_bb.seq = seq; }

uint32_t BlackBox_BootCount(void) { return g_bb.boot_count; }

const blackbox_t *BlackBox_Get(void) { return &g_bb; }
'''

_FAULT_C = r'''/* 故障现场转储: 把 Cortex-M 故障寄存器 + 异常栈帧打到 RTT。
   异常入栈顺序(ARMv7-M): R0,R1,R2,R3,R12,LR,PC,xPSR -> f[6]=PC, f[5]=LR。 */
#include "main.h"
#include "SEGGER_RTT.h"
#include "fault_dump.h"
#include "blackbox.h"   /* 不用黑匣子可删 */

void FaultDump_Report(const char *tag, const uint32_t *f)
{
  SEGGER_RTT_printf(0, "\r\n!! FAULT %s cfsr=%08X hfsr=%08X bfar=%08X mmar=%08X\r\n",
                    tag, (unsigned)SCB->CFSR, (unsigned)SCB->HFSR,
                    (unsigned)SCB->BFAR, (unsigned)SCB->MMFAR);
  if (f != 0)
  {
    SEGGER_RTT_printf(0, "   pc=%08X lr=%08X psr=%08X r0=%08X r1=%08X r2=%08X r3=%08X r12=%08X\r\n",
                      (unsigned)f[6], (unsigned)f[5], (unsigned)f[7],
                      (unsigned)f[0], (unsigned)f[1], (unsigned)f[2],
                      (unsigned)f[3], (unsigned)f[4]);
  }
}

/* 若工程里已定义 HardFault_Handler, 删掉下面这个, 改为在你自己的 handler 里调 FaultDump_Entry。 */
void FaultDump_Entry(uint32_t *frame);

__attribute__((naked)) void HardFault_Handler(void)
{
  __asm volatile (
    "mov r0, lr        \n"
    "tst r0, #4        \n"
    "ite eq            \n"
    "mrseq r0, msp     \n"
    "mrsne r0, psp     \n"
    "b FaultDump_Entry \n"
  );
}

void FaultDump_Entry(uint32_t *frame)
{
  BlackBox_Record(BB_TAG_HARDFAULT, frame);   /* 先写黑匣子(跨复位保留) */
  FaultDump_Report("HardFault", frame);       /* 再实时打印一份 */

  /* 复位前留 ~300ms 让主机把 RTT 缓冲读走(RTT 是缓冲, 复位即丢)。
     三个坑:
       1) 不能用 HAL_Delay —— 它靠 SysTick 中断, 在 Fault 里中断可能已被屏蔽/优先级不够;
       2) 不能假定 DWT->CYCCNT 可用 —— 只有 DEMCR.TRCENA + DWT_CTRL.CYCCNTENA 都置位它才走;
          没初始化时 CYCCNT 恒 0, 死等 -> 后面的复位永远执行不到(表现为"卡死在故障里");
       3) Cortex-M0/M0+/M23 根本没有 DWT->CYCCNT(这段需要改写成 NOP 空转或 SysTick 计数)。
     所以: 能用 DWT 就用 DWT, 并额外加一个自减 guard 兜底, 保证一定会往下走。 */
  {
    uint32_t guard = 200000000u;
    if (((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0u) &&
        ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0u))
    {
      uint32_t t0 = DWT->CYCCNT;
      uint32_t wait = (SystemCoreClock / 1000u) * 300u;
      while (((DWT->CYCCNT - t0) < wait) && (--guard != 0u)) { }
    }
    else
    {
      volatile uint32_t spin = SystemCoreClock / 4000u * 300u;   /* 粗略 ~300ms */
      while (spin--) { }
    }
  }

  /* 有限次自动复位。注意: 这里用启动次数近似"连续崩溃次数",
     并不精确 —— 正常上电也会让 boot_count 增加(见 blackbox 的注释)。 */
  if (BlackBox_BootCount() < 3u) { NVIC_SystemReset(); }
  while (1) { }
}
'''

_FAULT_H = r'''#ifndef FAULT_DUMP_H
#define FAULT_DUMP_H

#include <stdint.h>

void FaultDump_Report(const char *tag, const uint32_t *frame);  /* frame 可传 0 */
void FaultDump_Entry(uint32_t *frame);                          /* 裸 handler 的落点 */

#endif /* FAULT_DUMP_H */
'''


def cmd_blackbox(args):
    """读取 .noinit 黑匣子记录(布局见模板: 13 个字)。"""
    elf = args.elf or infer_elf()
    if not elf or not os.path.isfile(elf):
        print("ERROR: 未找到 ELF, 请 --elf 指定。")
        return 1
    addr = symbol_addr_from_elf(elf, args.symbol)
    if addr is None:
        print("ERROR: ELF 里找不到符号 %s。请用 --symbol 指定黑匣子变量名。" % args.symbol)
        return 1
    print("黑匣子符号 %s @ 0x%08X" % (args.symbol, addr))
    out = with_server(args.device, elf, ["x/13xw 0x%08X" % addr], resume=not args.keep_halted)
    print(out)
    # 只解析内存 dump 行(形如 "0x20000e60 <g_bb>:	0x..."), 别把 gdb 的 PC 行也算进来
    nums = []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("0x") and ":" in s:
            nums += [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{1,8})", s.split(":", 1)[1])]
    if len(nums) < 13:
        print("!! 没读到 13 个字, 检查符号/地址/是否被优化掉。")
        return
    magic, boot, crash, tag, cfsr, hfsr, bfar, mmar, pc, lr, psr, tick, seq = nums[:13]
    print("--- 解码 ---")
    if magic != _BB_MAGIC:
        print("  magic=0x%08X (不是 0x%08X) -> 记录区无效: 冷启动, 或 .noinit 段没配好/被清零"
              % (magic, _BB_MAGIC))
        return
    print("  boot_count=%d  crash_flag=%d  tag=%d(%s)" % (
        boot, crash, tag,
        {1: "HardFault", 2: "Error_Handler"}.get(tag, "?")))
    if crash == 0:
        print("  上次运行正常结束(无崩溃记录)。")
        return
    print("  cfsr=%08X hfsr=%08X bfar=%08X mmar=%08X" % (cfsr, hfsr, bfar, mmar))
    print("  pc=%08X lr=%08X psr=%08X tick=%u seq=%u" % (pc, lr, psr, tick, seq))
    jset(blackbox={"boot_count": boot, "crash_flag": crash, "tag": tag,
                   "cfsr": "0x%08X" % cfsr, "hfsr": "0x%08X" % hfsr,
                   "pc": "0x%08X" % pc, "lr": "0x%08X" % lr,
                   "tick": tick, "seq": seq})
    print("  定位: arm-none-eabi-addr2line -e %s -f -p 0x%08X" % (os.path.basename(elf), pc))


def cmd_init_fault(args):
    """把故障现场转储 + 黑匣子的模板代码生成到工程里。"""
    d = args.dir or "."
    src = os.path.join(d, "Core", "Src")
    inc = os.path.join(d, "Core", "Inc")
    if not os.path.isdir(src) or not os.path.isdir(inc):
        src = inc = d          # 非 CubeMX 布局: 直接放目标目录
    files = [
        (os.path.join(inc, "blackbox.h"), _BLACKBOX_H),
        (os.path.join(src, "blackbox.c"), _BLACKBOX_C),
        (os.path.join(inc, "fault_dump.h"), _FAULT_H),
        (os.path.join(src, "fault_dump.c"), _FAULT_C),
    ]
    skipped = 0     # 被跳过(=没写成)的文件数, 决定退出码
    for path, content in files:
        if os.path.exists(path) and not args.force:
            print("跳过(已存在): %s   [--force 覆盖]" % path)
            skipped += 1
            continue
        with open(path, "w", newline="\n") as f:
            f.write(content)
        print("写入: %s" % path)
    print("""
--- 接线步骤 ---
1) 把 blackbox.c / fault_dump.c 加进构建(如 Makefile 的 C_SOURCES)。
2) 链接脚本加 .noinit 段(必须不加载、不初始化, 且不能落在 .bss 范围里):
     .noinit (NOLOAD) : { . = ALIGN(4); *(.noinit) *(.noinit*) . = ALIGN(4); } >RAM
3) 启动时调用(顺序很重要, 必须在 RTT/串口初始化之后):
     SEGGER_RTT_Init();
     BlackBox_Init();          /* 打印上次崩溃现场 */
4) 主循环里可选: BlackBox_SetSeq(<进度计数>);
5) Error_Handler 里加: BlackBox_Record(BB_TAG_ERROR, 0);
6) 崩溃后读: python stm32-dev.py blackbox --elf build/xxx.elf
""")
    if skipped:
        print("!! 有 %d 个文件因已存在被跳过(未覆盖)。确认要用新模板请加 --force。" % skipped)
        return 1
    return 0


def find_objcopy():
    """arm-none-eabi-objcopy(用于生成 flat image 做 flash 校验)。"""
    for name in ("arm-none-eabi-objcopy",):
        p = shutil.which(name)
        if p:
            return p
    gdb = find_gdb()
    if gdb:
        base = os.path.dirname(gdb)
        cand = os.path.join(base, "arm-none-eabi-objcopy" + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(cand):
            return cand
    return None


def run_jlink_script(dev, lines, timeout=120):
    """用 JLink.exe 执行一段 Commander 脚本, 返回输出(自动清理它拉起的 GUI server)。"""
    jlink = find_jlink_cmd()
    if not jlink:
        return "ERROR: 找不到 JLink.exe。"
    _dry_stop([jlink, "-device", dev, "-if", "SWD", "-speed", "4000", "-autoconnect", "1",
               "-CommanderScript", "<临时脚本: %s>" % "; ".join(lines)])
    fd, path = tempfile.mkstemp(suffix=".jlink")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    gui_before = _jlink_gui_pids()
    try:
        r = subprocess.run([jlink, "-device", dev, "-if", "SWD", "-speed", "4000",
                            "-autoconnect", "1", "-CommanderScript", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "ERROR: %s" % e
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        for pid in (_jlink_gui_pids() - gui_before):
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
            except Exception:
                pass


def _elf_load_info(elf):
    """返回 (base_addr, size) —— ELF 里最小 LOAD 物理地址和总长度。"""
    readelf = find_readelf()
    if not readelf:
        return None
    try:
        out = subprocess.run([readelf, "-lW", elf], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30).stdout or ""
    except Exception:
        return None
    segs = []
    for line in out.splitlines():
        p = line.split()
        # LOAD 0x000000 0x08000000 0x08000000 0x05f40 0x05f40 R E 0x10000
        if p and p[0] == "LOAD" and len(p) >= 6:
            try:
                segs.append((int(p[3], 16), int(p[4], 16)))
            except ValueError:
                continue
    if not segs:
        return None
    base = min(a for a, _ in segs)
    end = max(a + s for a, s in segs)
    return base, end - base


def _verify_prepare(args, elf=None):
    """校验的公共准备: 找 ELF -> objcopy 出二进制 -> 取 LOAD 段范围。
    返回 (elf, base, size, data); 出错打印原因并返回 None。"""
    elf = elf or getattr(args, "elf", None) or infer_elf()
    if not elf or not os.path.isfile(elf):
        print("ERROR: 未找到 ELF。请 --elf 指定。")
        return None
    objcopy = find_objcopy()
    if not objcopy:
        print("ERROR: 找不到 arm-none-eabi-objcopy。")
        return None
    info = _elf_load_info(elf)
    if not info:
        print("ERROR: 解析 ELF LOAD 段失败(需要 arm-none-eabi-readelf)。")
        return None
    base, size = info
    tmp_bin = os.path.join(tempfile.gettempdir(), "stm32-dev-verify.bin")
    try:
        subprocess.run([objcopy, "-O", "binary", elf, tmp_bin], capture_output=True, timeout=60, check=True)
    except Exception as e:
        print("ERROR: objcopy 失败: %s" % e)
        return None
    try:
        data = open(tmp_bin, "rb").read()[:size]
    except OSError as e:
        print("ERROR: 读 %s 失败: %s" % (tmp_bin, e))
        return None
    return (elf, base, len(data), data)


def _verify_compare(args, elf, base, data, b, how=""):
    """公共比对: 本地镜像 vs 从板子读回的那串字节。两边读回通道(官方 CLI / OpenOCD)都走这里。"""
    n = min(len(data), len(b))
    diffs = [i for i in range(n) if data[i] != b[i]]
    ok = (not diffs) and len(data) == len(b)
    jset(elf=elf, base=base, size=len(data), diff_bytes=len(diffs), match=ok, via=how)
    if ok:
        print("VERIFY OK: 板子固件与 %s 完全一致。" % os.path.basename(elf))
        return 0
    print("!! VERIFY FAIL: 板子上的固件和 %s 不一样。" % os.path.basename(elf))
    print("   ---- 这就是坑#9(板子在跑旧固件), 先别查代码, 先把烧录搞对 ----")
    if len(data) != len(b):
        print("   长度不一致: ELF %d vs 板子 %d" % (len(data), len(b)))
    print("   不同字节: %d / %d" % (len(diffs), n))
    for i in diffs[:5]:
        print("   0x%08X: ELF=0x%02X 板子=0x%02X" % (base + i, data[i], b[i]))
    if len(diffs) > 5:
        print("   ...")
    print("   -> 重新烧: %s %s flash --elf %s" % (sys.executable or "python", os.path.abspath(__file__), elf))
    return 1


def _ocd_dump_argv(probe, serial, device, base, size, outfile, args=None):
    """OpenOCD 把 Flash 读回文件的命令行: 官方 CLI 用不了这只 ST-Link 时的备份通道, DAPLink 也用它。"""
    ocd = find_openocd()
    if not ocd:
        print("ERROR: 没找到 openocd(读回校验要用它; 见 SETUP.md)。")
        return None
    tgt = _ocd_target(device)
    if not tgt:
        print("ERROR: 认不出 %s 对应的 openocd 目标脚本。" % device)
        return None
    argv = _argv_for(ocd, "-f", _OCD_IFACE.get(probe, "interface/cmsis-dap.cfg"),
                     "-c", "transport select swd")
    argv += _ocd_speed_arg(probe, args)
    if serial:
        argv += ["-c", "adapter serial %s" % serial]
    argv += ["-f", "target/%s.cfg" % tgt, "-c", "init", "-c", "halt",
             "-c", "dump_image \"%s\" 0x%08X 0x%X" % (outfile.replace("\\", "/"), base, size),
             "-c", "catch {resume}", "-c", "shutdown"]
    return argv


def _verify_via_ocd(args, probe, prep=None, why=""):
    """用 OpenOCD 读回板子 Flash 再逐字节比。
    两条路都会走到这里: ST-Link 的官方 CLI 用不了(克隆件/被占), 以及 DAPLink。"""
    if prep is None:
        prep = _verify_prepare(args)
    if not prep:
        return 1
    elf, base, size, data = prep
    fl = os.path.join(tempfile.gettempdir(), "stm32-dev-verify-read.bin")
    try:
        os.remove(fl)      # 旧文件必须删: 不然会把上次读回的结果当成这次的结果
    except OSError:
        pass
    dev = resolve_device(args, elf)
    serial = _stlink_serial(args) if probe == "stlink" else (args.serial if (args.serial or "") != DEFAULT_SERIAL else "")
    argv = _ocd_dump_argv(probe, serial, dev, base, size, fl, args)
    if not argv:
        return 1
    if why:
        print("(%s)" % why)
    print("ELF 镜像: base=0x%08X size=%d -> 用 OpenOCD 从板子读回 Flash 比对..." % (base, size))
    r = _run_openocd(argv, timeout=max(120, size // 256))
    if r is None:
        print("ERROR: 起 openocd 失败。")
        return 1
    out = ((r.stdout or "") + (r.stderr or ""))
    print(out[-800:])
    if not (r.returncode == 0 and os.path.isfile(fl) and os.path.getsize(fl) > 0):
        _ocd_fail_hint(out)
        print("ERROR: OpenOCD 读回 Flash 失败。")
        return 1
    try:
        b = open(fl, "rb").read()
    except OSError as e:
        print("ERROR: 读 %s 失败: %s" % (fl, e))
        return 1
    return _verify_compare(args, elf, base, data, b, how="OpenOCD")


def _verify_stlink(args):
    """ST-Link 的刷后校验: 从板子读回 Flash, 与本地镜像逐字节比(J-Link 路径同一条铁律)。
    官方 CLI 读不回来(克隆件/被占)时自动换 OpenOCD, 不用人管。"""
    if _stlink_channel(args) == "openocd" or _STLINK_CLI_DEAD:
        return _verify_via_ocd(args, "stlink", why="这只 ST-Link 走 OpenOCD 通道")
    elf = args.elf or infer_elf()
    if not elf or not os.path.isfile(elf):
        print("ERROR: 未找到 ELF。请 --elf 指定。")
        return 1
    cli = find_stm32_cli()
    if not cli:
        print("ERROR: 没找到 STM32_Programmer_CLI(ST-Link 的官方命令行)。")
        return 1
    objcopy = find_objcopy()
    if not objcopy:
        print("ERROR: 找不到 arm-none-eabi-objcopy。")
        return 1
    info = _elf_load_info(elf)
    if not info:
        print("ERROR: 解析 ELF LOAD 段失败(需要 arm-none-eabi-readelf)。")
        return 1
    base, size = info
    tmp_bin = os.path.join(tempfile.gettempdir(), "stm32-dev-verify.bin")
    tmp_read = os.path.join(tempfile.gettempdir(), "stm32-dev-verify-read.bin")
    try:
        os.remove(tmp_read)      # 旧文件必须删: CLI 不改写已存在的读回文件时会被当成新结果
    except OSError:
        pass
    try:
        subprocess.run([objcopy, "-O", "binary", elf, tmp_bin], capture_output=True, timeout=60, check=True)
    except Exception as e:
        print("ERROR: objcopy 失败: %s" % e)
        return 1
    data = open(tmp_bin, "rb").read()[:size]
    size = len(data)
    print("ELF 镜像: base=0x%08X size=%d -> 用 ST-Link 从板子读回 Flash 比对..." % (base, size))
    _free_stlink_for_cli(verbose=True)
    conn = _stlink_conn(_stlink_serial(args), freq=_stlink_freq(args, "cli"))
    argv = [cli, "-c", conn, "-u", "0x%08X" % base, "0x%X" % size, tmp_read]
    print("  %s" % " ".join(argv))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(120, size // 512))
    except Exception as e:
        print("ERROR: 跑官方 CLI 失败: %s" % e)
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or not os.path.isfile(tmp_read):
        print(out[-900:])
        if _stlink_cli_blocked(out.lower()):
            print("ST-Link 官方 CLI 用不了这只探针(克隆件 / 探针被别的程序占着)。")
            if _stlink_channel(args) == "cli":
                return 1
            return _verify_via_ocd(args, "stlink", prep=(elf, base, size, data),
                                   why="改用 OpenOCD 读回校验")
        print("ERROR: 读回 Flash 失败(上面 Error 那行是原因; 没插探针也会这样)。")
        return 1
    b = open(tmp_read, "rb").read()
    return _verify_compare(args, elf, base, data, b, how="ST-Link 官方 CLI")
    print("!! VERIFY FAIL: 板子上的固件和 %s 不一样。" % os.path.basename(elf))
    print("   ---- 这就是坑#9(板子在跑旧固件), 先别查代码, 先把烧录搞对 ----")
    if len(data) != len(b):
        print("   长度不一致: ELF %d vs 板子 %d" % (len(data), len(b)))
    print("   不同字节: %d / %d" % (len(diffs), n))
    for i in diffs[:5]:
        print("   0x%08X: ELF=0x%02X 板子=0x%02X" % (base + i, data[i], b[i]))
    if len(diffs) > 5:
        print("   ...")
    print("   -> 重新烧: %s %s flash --elf %s" % (sys.executable or "python", os.path.abspath(__file__), elf))
    return 1


def cmd_verify(args):
    """校验板子上的固件与 ELF 是否一致(坑#9: 板子跑旧固件是最常见的假 bug)。"""
    kind = probe_for_work()
    if kind == "stlink":
        return _verify_stlink(args)
    if kind == "daplink":
        return _verify_via_ocd(args, "daplink", why="DAPLink 的读回校验走 OpenOCD")
    elf = args.elf or infer_elf()
    if not elf or not os.path.isfile(elf):
        print("ERROR: 未找到 ELF。请 --elf 指定。")
        return 1
    objcopy = find_objcopy()
    if not objcopy:
        print("ERROR: 找不到 arm-none-eabi-objcopy。")
        return 1
    info = _elf_load_info(elf)
    if not info:
        print("ERROR: 解析 ELF LOAD 段失败(需要 arm-none-eabi-readelf)。")
        return 1
    base, size = info
    dev = resolve_device(args, elf)
    tmp_bin = os.path.join(tempfile.gettempdir(), "stm32-dev-verify.bin")
    tmp_flash = os.path.join(tempfile.gettempdir(), "stm32-dev-verify-flash.bin")
    try:
        subprocess.run([objcopy, "-O", "binary", elf, tmp_bin], capture_output=True, timeout=60, check=True)
    except Exception as e:
        print("ERROR: objcopy 失败: %s" % e)
        return 1
    print("ELF 镜像: base=0x%08X size=%d -> 读回板子 Flash 比对..." % (base, size))
    out = run_jlink_script(dev, ["savebin %s 0x%08X 0x%X" % (tmp_flash.replace("\\", "/"), base, size),
                                 "q"], timeout=max(60, size // 1024))
    if "O.K." not in out:
        print(out[-800:])
        print("ERROR: 读回 Flash 失败。")
        return 1
    a = open(tmp_bin, "rb").read()
    b = open(tmp_flash, "rb").read()
    if len(a) != len(b):
        print("!! 长度不一致: ELF %d vs 板子 %d (ELF 可能只烧了一部分?)" % (len(a), len(b)))
    n = min(len(a), len(b))
    diffs = [i for i in range(n) if a[i] != b[i]]
    jset(elf=elf, base=base, size=size, diff_bytes=len(diffs), match=(not diffs and len(a) == len(b)))
    match = (not diffs) and (len(a) == len(b))
    if match:
        print("VERIFY OK: 板子固件与 %s 完全一致。" % os.path.basename(elf))
    else:
        print("VERIFY FAIL: 共 %d 字节不同(前 3 处: %s)" %
              (len(diffs), ", ".join("+0x%X" % d for d in diffs[:3])))
        print("  -> 板子跑的很可能是旧固件(坑#9): 重新 make && make flash 后再验。")
    for f in (tmp_bin, tmp_flash):
        try:
            os.remove(f)
        except OSError:
            pass
    return 0 if match else 1


def cmd_reset(args):
    """复位并运行目标。按探针选路: J-Link 走 Commander 脚本, ST-Link 走官方 CLI。"""
    kind = probe_for_work()
    if kind == "stlink":
        return _reset_stlink(args)
    if kind == "daplink":
        return _reset_openocd(args, "daplink")
    dev = resolve_device(args, args.elf or infer_elf())
    # * `g` 与 `q` 之间必须有 Sleep(坑#80): 没 Sleep 时 J-Link 会在 MCU 还没跑起来
    #   就关掉会话, 核停在复位态 —— 现象是"复位完串口/485 一点反应都没有"。
    out = run_jlink_script(dev, ["r", "Sleep 1200", "g", "Sleep 1200", "q"])
    ok = "O.K." in out
    print("RESET+GO %s: %s" % (dev, "O.K." if ok else out[-300:]))
    return 0 if ok else 1


def cmd_selftest(args):
    """无硬件自检: 验证技能自身关键机制(不连板子)。"""
    ok = True
    checks = []

    def chk(name, cond, extra=""):
        nonlocal ok
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, (" -> " + extra) if extra else ""))
        checks.append({"name": name, "pass": bool(cond), "detail": extra})
        if not cond:
            ok = False

    # 1) 递增序列分析(正常/丢帧两种)
    good = "\n".join("seq=%d tick=%d" % (i, i * 100) for i in range(1, 51))
    res = analyze_increasing_seq(good)
    chk("analyze_increasing_seq 正常样本判无丢帧", res is not None and res[3] == 0, str(res))
    bad = good.replace("seq=25", "seq=40")
    res2 = analyze_increasing_seq(bad)
    chk("analyze_increasing_seq 能检出丢帧", res2 is not None and res2[3] >= 1, str(res2))
    # 2) 模板生成
    d = tempfile.mkdtemp(prefix="stm32-dev-selftest-")
    d0 = d      # 后面退出码自检用同一个临时目录(里面没有 ELF)

    class _A:
        pass

    a = _A()
    a.dir = d
    a.force = True
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_init_fault(a)
    chk("init-fault 生成 4 个模板文件", len(glob.glob(os.path.join(d, "*"))) == 4)
    for f in glob.glob(os.path.join(d, "*")):
        os.remove(f)
    os.rmdir(d)
    # 3) ELF 符号解析
    elf = args.elf or infer_elf()
    if elf and os.path.isfile(elf):
        chk("symbol_addr_from_elf(_SEGGER_RTT)", symbol_addr_from_elf(elf, "_SEGGER_RTT") is not None)
        chk("_elf_load_info", _elf_load_info(elf) is not None, str(_elf_load_info(elf)))
    else:
        print("  [skip] 未找到 ELF, 跳过符号/段解析自检(--elf 可指定)")
    # 4) 工具定位
    for name, fn in (("find_gdb", find_gdb), ("find_jlink_server", find_jlink_server),
                     ("find_readelf", find_readelf), ("find_objcopy", find_objcopy),
                     ("find_rtt_logger", find_rtt_logger)):
        chk(name, fn() is not None)
    # 5) 退出码契约: 失败必须非零(以前 main 里的 or 0 会把失败吞成 0)
    me = os.path.abspath(__file__)
    for name, argv in (("verify 缺 ELF", ["verify", "--elf", os.path.join(d0, "nope.elf")]),
                       ("cleanup --all 缺 --force", ["cleanup", "--all"])):
        try:
            # 只要退出码, 不抓输出(抓管道会在 Python 3.14 退出时冒出 reader 线程的噪声)
            r = subprocess.run([sys.executable, me] + argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120)
            chk("退出码: %s -> 非零" % name, r.returncode != 0, "rc=%d" % r.returncode)
        except Exception as e:
            chk("退出码: %s -> 非零" % name, False, str(e))

    # 6) 串号参数构造(以前指定 --serial 必然 IndexError)
    a = _server_args("JLinkGDBServerCL", "STM32H743VI", 3333, "602711039")
    chk("_server_args 带串号", "USB=602711039" in a and a[a.index("-select") + 1] == "USB=602711039", str(a[-2:]))
    a2 = _server_args("JLinkGDBServerCL", "STM32H743VI", 3333, "")
    chk("_server_args 不带串号", a2[a2.index("-select") + 1] == "USB", str(a2[-2:]))

    # 7) 闭环断言解析
    chk("_parse_bool(true)", _parse_bool("$1 = true") is True)
    chk("_parse_bool(false)", _parse_bool("$1 = false") is False)
    chk("_parse_bool(0/7)", _parse_bool("$1 = 0") is False and _parse_bool("$1 = 7") is True)
    chk("_parse_bool(乱码) -> None", _parse_bool("error") is None)

    # 7b) preflight: 烧录脚本 r/g/exit 缺 Sleep 必须被检出(坑 #80)
    bad_script = 'loadfile a.hex\nr\ng\nexit\n'
    chk("preflight 检出 r/g/exit 全缺 Sleep",
        len(_pf_flash_script_issues(bad_script)) == 2,
        str(_pf_flash_script_issues(bad_script)))
    good_script = 'loadfile a.hex\nr\nSleep 1200\ng\nSleep 1200\nexit\n'
    chk("preflight 放过 r Sleep g Sleep exit",
        len(_pf_flash_script_issues(good_script)) == 0,
        str(_pf_flash_script_issues(good_script)))
    # Makefile 形态: 命令全塞在一个 printf 的字面 \n 里, 也必须拆得开
    mk = '@printf "loadfile $(H)\\nr\\ng\\nexit\\n" > flash.jlink\n'
    chk("preflight 能拆开 Makefile 里的字面 \\n",
        len(_pf_flash_script_issues(mk)) == 2, str(_pf_flash_script_issues(mk)))
    chk("preflight 放过 r 与 g 之间有 Sleep 的 Makefile",
        len(_pf_flash_script_issues(
            mk.replace("\\nr\\ng\\nexit", "\\nr\\nSleep 1200\\ng\\nSleep 1200\\nexit"))) == 0)
    chk("preflight r->g 与 g->exit 各报一条(无 loadfile 时由调用方过滤)",
        len(_pf_flash_script_issues("r\ng\nexit\n")) == 2,
        str(_pf_flash_script_issues("r\ng\nexit\n")))

    # 7c) preflight: 只死循环的故障处理器必须被检出(坑 #32)
    hf_dead = "void HardFault_Handler(void) {\n  while (1) {}\n}\n"
    chk("preflight 检出死循环 HardFault",
        [n for n, _ in _pf_iter_fault_handlers(hf_dead)] == ["HardFault_Handler"],
        str([n for n, _ in _pf_iter_fault_handlers(hf_dead)]))
    hf_log = "void HardFault_Handler(void) {\n  g_bb.cfsr = SCB->CFSR;\n  while (1) {}\n}\n"
    chk("preflight 放过有现场记录的 HardFault",
        list(_pf_iter_fault_handlers(hf_log)) == [])
    chk("preflight 不管 CubeMX 的 Error_Handler",
        list(_pf_iter_fault_handlers("void Error_Handler(void) {\n while (1) {}\n}\n")) == [])

    # 6b) 故障处理器: 同一文件 #if/#else 两版 + 委派给记录函数 + 没使能的异常, 都不能误报
    hf_naked = "void HardFault_Handler(void) { __asm volatile ( \"b hardfault_entry\" ); }\n"
    hf_dead2 = "void HardFault_Handler(void) { while (1) { } }\n"
    hf_rec = "void hardfault_entry(uint32_t *f) { fault_report(\"HF\", f); }\n"
    d_hf = [("it.c", n, b, _pf_body_has_log(b)) for n, b in _pf_fault_handler_defs(hf_naked + hf_dead2)]
    lf = set(n for n, v in _pf_func_logged(hf_rec).items() if v)
    hard1, soft1 = _pf_fault_verdicts(d_hf, lf, False)
    chk("preflight: 同文件 #if/#else 两版 HardFault + 委派 -> 只提醒不报警",
        len(d_hf) == 2 and not hard1 and len(soft1) == 1)
    hard2, soft2 = _pf_fault_verdicts(
        [("it.c", "HardFault_Handler", " { while (1) { } }", False),
         ("it.c", "UsageFault_Handler", " { while (1) { } }", False)], set(), False)
    chk("preflight: 裸 HardFault 报警 / 没使能的 UsageFault 只提醒",
        [x[1] for x in hard2] == ["HardFault_Handler"] and [x[1] for x in soft2] == ["UsageFault_Handler"])
    hard3, soft3 = _pf_fault_verdicts([("it.c", "UsageFault_Handler", " { while (1) { } }", False)], set(), True)
    chk("preflight: SHCSR 使能了的 UsageFault 也报警",
        [x[1] for x in hard3] == ["UsageFault_Handler"] and not soft3)

    # 8) 型号识别失败必须报错, 不能猜
    class _N:
        device = None
        elf = None
    try:
        resolve_device(_N())
        chk("resolve_device 不猜型号", False, "居然没报错")
    except SkillError:
        chk("resolve_device 不猜型号", True)

    # 9) SVD 解析(有 SVD 才测)
    if elf and os.path.isfile(elf):
        dev0 = infer_device_generic(elf)
        svd0 = find_svd(dev0) if dev0 else None
        if svd0:
            ph = load_svd(svd0)
            chk("load_svd 解析出外设", bool(ph) and len(ph) > 5, "%d 个外设" % (len(ph) if ph else 0))
        else:
            print("  [skip] 本机没有 %s 的 SVD, 跳过解析自检" % dev0)

    # 10) 探针层(换 J-Link / ST-Link / DAPLink 的地基): 归一化 / 能力表 / 配置 / 各后端命令
    chk("_norm_probe 认得出三种调试器的各种叫法",
        _norm_probe("ST-Link V3") == "stlink" and _norm_probe("stlinkv3set") == "stlink"
        and _norm_probe("cmsis-dap") == "daplink" and _norm_probe("CMSIS DAP") == "daplink"
        and _norm_probe("J-Link") == "jlink" and _norm_probe("") == "" and _norm_probe("乱写的") == "",
        "ST-Link V3 -> %s" % _norm_probe("ST-Link V3"))
    chk("三种调试器都有能力表(name/caps/best/notes/tools)",
        sorted(PROBE_DEFS.keys()) == ["daplink", "jlink", "stlink"]
        and all(k in PROBE_DEFS[p] and PROBE_DEFS[p][k] for p in PROBE_DEFS
                for k in ("name", "caps", "best", "notes", "tools", "degrade")))
    chk("能力表 >= 12 项且 caps 是键值表",
        len(CAP_LABELS) >= 12 and all(isinstance(PROBE_DEFS[p]["caps"], dict) and PROBE_DEFS[p]["caps"]
                                      for p in PROBE_DEFS),
        "%d 项能力" % len(CAP_LABELS))
    chk("_ocd_target 认系列, 认不出给 None(不猜)",
        _ocd_target("STM32G431CBT6") == "stm32g4x" and _ocd_target("STM32F103C8T6") == "stm32f1x"
        and _ocd_target("STM32H743VIT6") == "stm32h7x" and _ocd_target("ATMEGA328P") is None)
    chk("_stlink_conn 拼官方 CLI 连接串",
        _stlink_conn() == "port=SWD freq=8000" and "sn=602711039" in _stlink_conn("602711039")
        and "mode=UR" in _stlink_conn("", 8000, "UR"), _stlink_conn("602711039", 8000, "UR"))
    chk("_flash_from_code 按容量码(第 11 位)取容量",
        _flash_from_code("STM32G431CBT6") == "128K" and _flash_from_code("STM32H743VIT6") == "2048K"
        and _flash_from_code("STM32F103C8") == "64K",
        "G431CBT6 -> %s" % _flash_from_code("STM32G431CBT6"))
    dcfg = tempfile.mkdtemp(prefix="stm32-dev-cfg-")
    save_config({"probe": "stlink", "serial": "0421"}, dcfg)
    cfg = load_config(dcfg)
    chk("工程配置 probe/serial 能存能读",
        cfg.get("probe") == "stlink" and cfg.get("serial") == "0421" and os.path.isfile(config_file(dcfg)),
        str(cfg))
    if os.path.isfile(config_file(dcfg)):
        os.remove(config_file(dcfg))
    try:
        os.rmdir(dcfg)
    except OSError:
        pass
    pa = _A()
    pa.probe = "stlink"
    pa.serial = ""
    chk("resolve_probe: --probe 优先", resolve_probe(pa)[0] == "stlink")
    pa.probe = "瞎写的"
    try:
        resolve_probe(pa)
        chk("resolve_probe: 认不出的探针必须报错", False, "居然没报错")
    except SkillError:
        chk("resolve_probe: 认不出的探针必须报错", True)
    ocd = find_openocd()
    if ocd:
        s1 = " ".join(_ocd_prog_argv("daplink", "ABC123", "STM32G431CBT6") or [])
        chk("DAPLink: cmsis-dap 接口 + adapter serial + 系列脚本",
            "interface/cmsis-dap.cfg" in s1 and "adapter serial ABC123" in s1
            and "target/stm32g4x.cfg" in s1, s1)
        s2 = " ".join(_ocd_prog_argv("stlink", "", "STM32G431CBT6") or [])
        chk("ST-Link: stlink 接口 + 不塞串号",
            "interface/stlink.cfg" in s2 and "adapter serial" not in s2, s2)
    else:
        print("  [skip] 没装 openocd, 跳过 OpenOCD 命令行自检")
    chk("本机 ST 官方 CLI / Cube 包 / 探针工具盘点不崩",
        isinstance(find_stm32_cli(), (str, type(None))) and isinstance(find_cube_pack("STM32G431CB") , (str, type(None))))

    if ocd:
        rc = " ".join(_ocd_rtt_argv(ocd, "stlink", "", "STM32G431CBT6", 0x20000000, 0x400, 9090, 0) or [])
        chk("OpenOCD RTT: setup/start/server 三段齐全, 且 resume 用 catch 兜住",
            'rtt setup 0x20000000 0x400 "SEGGER RTT"' in rc and "rtt start" in rc
            and "rtt server start 9090 0" in rc and "catch {resume}" in rc, rc)
        rd = " ".join(_ocd_rtt_argv(ocd, "daplink", "ABC123", "STM32F103C8T6", 0x20000000, 0x20000, 9091, 1) or [])
        chk("OpenOCD RTT: DAPLink 用 cmsis-dap + 带串号 + F1 系列脚本",
            "interface/cmsis-dap.cfg" in rd and "adapter serial ABC123" in rd
            and "target/stm32f1x.cfg" in rd, rd)
        chk("OpenOCD RTT: 认不出的芯片必须返回 None(不许瞎编 target 脚本)",
            _ocd_rtt_argv(ocd, "stlink", "", "ATMEGA328P", 0, 0x400, 9090, 0) is None)

    class _A:
        pass
    a = _A()
    a.address = None
    a.search = None
    a.rtt_port = 0
    chk("RTT 地址窗口: 没 ELF 符号时退回全 RAM 搜索窗口",
        _rtt_addr_span(a, "") == (0x20000000, 0x20000), str(_rtt_addr_span(a, "")))
    a.address = "0x20000BA4"
    chk("RTT 地址窗口: --address 优先且窗口开小",
        _rtt_addr_span(a, "") == (0x20000BA4, 0x400), str(_rtt_addr_span(a, "")))
    a.address = "乱写"
    chk("RTT 地址窗口: 地址写错返回 None(不许崩)", _rtt_addr_span(a, "") is None)
    a.address = None
    chk("RTT TCP 端口: 默认 9090", _rtt_tcp_port(a) == 9090)
    a.rtt_port = 9500
    chk("RTT TCP 端口: --rtt-port 生效", _rtt_tcp_port(a) == 9500)
    # 地址自愈靠"抓包文件是不是空的"来判定, 这个判定错了会白白重抓或者漏重抓。
    _empt = os.path.join(tempfile.gettempdir(), "stm32-dev-selftest-empty.log")
    with io.open(_empt, "wb") as fh:
        fh.write(b"\xef\xbb\xbf \r\n\t")
    chk("RTT 自愈: 只有空白/BOM 算没抓到", _rtt_empty(_empt) is True)
    with io.open(_empt, "w", encoding="utf-8") as fh:
        fh.write("RTT seq=1\r\n")
    chk("RTT 自愈: 有数据就不算空", _rtt_empty(_empt) is False)
    os.remove(_empt)
    chk("RTT 自愈: 文件根本不存在也算空(不许崩)",
        _rtt_empty(os.path.join(tempfile.gettempdir(), "stm32-dev-no-such-file.log")) is True)
    chk("RTT 自愈: 默认搜索范围是 RAM 段",
        _RTT_SEARCH_DEFAULT.startswith("0x2") and " " in _RTT_SEARCH_DEFAULT, _RTT_SEARCH_DEFAULT)

    # 工具链自动配置: 安装命令必须对应实测存在的包 ID, 编一个不存在的 ID 会白等一场。
    _oc = _sys_install_cmds("openocd")
    chk("自动安装: openocd 有安装命令(包 ID 实测存在)",
        bool(_oc) and any(("xpack-dev-tools.openocd-xpack" in x) or (x == "openocd") for x in _oc), str(_oc))
    _gcc = _sys_install_cmds("gcc")
    chk("自动安装: 编译器有安装命令",
        bool(_gcc) and any(("Arm.GnuArmEmbeddedToolchain" in x) or (x == "gcc-arm-none-eabi") for x in _gcc), str(_gcc))
    chk("自动安装: 没把握的东西不乱装(返回空)", _sys_install_cmds("something-else") == [])
    # 主动建议: 每种探针都要说清"还能做什么", 探针无关的三条永远在。
    _pj = _proactive_lines("jlink")
    _ps = _proactive_lines("stlink")
    _pd = _proactive_lines("daplink")
    _pu = _proactive_lines("")
    chk("主动建议: J-Link 提到 RTT 与下行回灌",
        any(("rtt --elf" in x) for x in _pj) and any(("rtt-send" in x) for x in _pj))
    chk("主动建议: ST-Link 提到选项字节与 -hf",
        any(("mode=UR" in x) for x in _ps) and any(("-hf" in x) for x in _ps))
    chk("主动建议: DAPLink 提到 pyOCD", any(("pyocd" in x.lower()) for x in _pd))
    chk("主动建议: 没定探针时先提示去探测", any(("probe detect" in x) for x in _pu), str(_pu))
    chk("主动建议: 四类都给探针无关的三条(串口/黑匣子/DWT)",
        all(any(("serial --port" in x) for x in v) and any(("blackbox" in x) for x in v)
            and any(("DWT" in x) for x in v) for v in (_pj, _ps, _pd, _pu)))
    # setup 的开关: --install 必须是真开关, 否则"缺啥自动装"是句空话。
    _ap = build_parser().parse_args(["setup", "--install"])
    chk("setup --install 开关存在(隐含 --fix)", _ap.install is True and _ap.fix is False)

    # --dry-run: 第一次烧接电机的板子时必须能"只看不跑"。这条错了会真的动板子。
    chk("dry-run: 默认是关的(不许默默变成不执行)", _DRY_RUN is False)
    globals()["_DRY_RUN"] = True        # 强制打开, 只测这个开关本身
    _blocked = False
    try:
        _dry_stop(["echo", "hi"])
    except _DryRun:
        _blocked = True
    globals()["_DRY_RUN"] = False
    chk("dry-run: 打开后 _dry_stop 会拦住执行(且抛出可被 main 接住)", _blocked is True)
    _pdf = build_parser().parse_args(["flash", "--dry-run"])
    _pdr = build_parser().parse_args(["reset", "--dry-run"])
    chk("dry-run: flash / reset 都有开关", _pdf.dry_run is True and _pdr.dry_run is True)

    # 裸地址读写: 没有 SVD 的东西(DWT / ITM / SCB / TPIU)也必须能读能写
    chk("裸地址: 默认按 4 字节读", _read_expr("0xE000ED00") == "*(volatile unsigned int *)0xE000ED00")
    chk("裸地址: --size 1/2/8 换宽度",
        _read_expr("0x20000000", 1).startswith("*(volatile unsigned char *)")
        and _read_expr("0x20000000", 2).startswith("*(volatile unsigned short *)")
        and _read_expr("0x20000000", 8).startswith("*(volatile unsigned long long *)"))
    chk("裸地址: 符号名原样透传", _read_expr("g_bb") == "g_bb" and _write_expr("uwTick") == "uwTick")
    chk("裸地址: 写用同一种左值形式", _write_expr("0xE000EDF0") == "*(volatile unsigned int *)0xE000EDF0")
    chk("裸地址: read/write 都有 --size",
        build_parser().parse_args(["read", "--size", "2", "0xE000ED00"]).size == 2
        and build_parser().parse_args(["write", "x=1"]).size == 4)

    # 恢复运行: 换探针后命令不一样; 写死 monitor go 会让 ST-Link/DAPLink 的 continue 直接失败
    _oldp = _PROBE
    globals()["_PROBE"] = "stlink"
    _rs = _resume_cmd()
    globals()["_PROBE"] = "daplink"
    _rd = _resume_cmd()
    globals()["_PROBE"] = "jlink"
    _rj = _resume_cmd()
    globals()["_PROBE"] = _oldp
    chk("恢复运行: ST-Link/DAPLink 用 monitor resume, J-Link 用 monitor go",
        _rs == "monitor resume" and _rd == "monitor resume" and _rj == "monitor go")

    # --- RTT 下行(rtt-send)也通用了: J-Link 走 pylink, ST-Link/DAPLink 走 OpenOCD 的双向 rtt server ---
    chk("--hex 能吃逗号与 0x 前缀", _rtt_hex_bytes("70,0A") == b"\x70\x0a" and _rtt_hex_bytes("0x70 0x0a") == b"\x70\x0a")
    chk("--hex 乱写返回 None(不崩)", _rtt_hex_bytes("zz") is None and _rtt_hex_bytes("") is None)
    _rsend = build_parser().parse_args(["rtt-send", "hi", "--probe", "stlink"])
    chk("rtt-send 有 --channel / --rtt-port", _rsend.channel == 0 and _rsend.rtt_port == 9090)
    chk("rtt-send 三件套在位",
        all(callable(globals().get(n)) for n in ("_rtt_send_ocd", "_ocd_start_rtt", "_ocd_finish", "_tail_text", "_fetch_rtt_sources")))
    chk("init-rtt 有 --offline", build_parser().parse_args(["init-rtt"]).offline is False)
    _rtt_paths = dict((n, rel) for rel, n in _RTT_REPO_FILES)
    chk("RTT 官方仓库路径与文件数对",
        len(_RTT_REPO_FILES) == 6
        and _rtt_paths.get("SEGGER_RTT.c", "").startswith("RTT/")
        and _rtt_paths.get("SEGGER_RTT_ASM_ARMv7M.S", "").startswith("RTT/")
        and _rtt_paths.get("SEGGER_RTT_Conf.h") == "Config/SEGGER_RTT_Conf.h")
    chk("init-rtt --offline 时不去联网", _fetch_rtt_sources(tempfile.gettempdir(), offline=True) == [])

    # --- ST-Link: 世代识别 / 通道自动切换 / 工程配置健壮性(都在真机 ST-Link V2 上踩出来) ---
    chk("ST-Link 世代: USB PID 优先",
        _stlink_variant("", "", "0483:3748")[0] == "v2"
        and _stlink_variant("", "", "0483:374b")[0] == "v2-1"
        and _stlink_variant("", "", "0483:374e")[0] == "v3")
    chk("ST-Link 世代: 没 PID 时看固件串, 板载板名分 V2-1",
        _stlink_variant("V2J46S7", "")[0] == "v2"
        and _stlink_variant("V2J28M18", "NUCLEO-G431RB")[0] == "v2-1"
        and _stlink_variant("V3J15M7", "")[0] == "v3"
        and _stlink_variant("", "")[0] == "")
    chk("ST-Link 世代: V2 与 V3 的最优 SWD 时钟不同",
        _stlink_profile("v2")["freq_ocd"] == 1800 and _stlink_profile("v2")["freq_cli"] == 4000
        and _stlink_profile("v3")["freq_ocd"] == 4000 and _stlink_profile("v3")["freq_cli"] == 8000)
    chk("ST-Link 世代: 能力表按世代覆盖(独立 V2 没有虚拟串口)",
        _caps_of("stlink", "v2").get("vcp", "").startswith("没有")
        and _caps_of("stlink", "v3").get("vcp", "").startswith("有(")
        and _caps_of("stlink", "v2").get("swd_clock", "").startswith("1.8")
        and _caps_of("stlink", "v3").get("swd_clock", "").startswith("8 "))
    chk("ST-Link 通道: CLI 报\"这只探针用不了\"才换道(真机原文)",
        _stlink_cli_blocked("st-link error (dev_connect_err)")
        and _stlink_cli_blocked("error: not a genuine st device")
        and _stlink_cli_blocked("no debug probe detected")
        and not _stlink_cli_blocked("download verified successfully")
        and not _stlink_cli_blocked(""))
    chk("OpenOCD 时钟参数: 只给 ST-Link 加, DAPLink 不加",
        _ocd_speed_arg("stlink")[0] == "-c" and _ocd_speed_arg("daplink") == [])
    _dch = tempfile.mkdtemp(prefix="stm32ch")
    _cwd0 = os.getcwd()
    try:
        os.chdir(_dch)
        save_config({"stlink_channel": "openocd", "stlink_freq": 1234})
        _a_auto = build_parser().parse_args(["flash"])
        _a_cli = build_parser().parse_args(["flash", "--via", "cli"])
        chk("ST-Link 通道: 没给 --via 时按工程配置走", _stlink_channel(_a_auto) == "openocd")
        chk("ST-Link 通道: --via 压过工程配置", _stlink_channel(_a_cli) == "cli")
        os.environ["STM32_DEV_STLINK_CHANNEL"] = "cli"
        chk("ST-Link 通道: 环境变量也压过工程配置", _stlink_channel(_a_auto) == "cli")
        del os.environ["STM32_DEV_STLINK_CHANNEL"]
        chk("SWD 时钟: 工程配置压过世代默认", _stlink_freq(_a_auto, "ocd") == 1234)
        chk("SWD 时钟: --freq 压过工程配置",
            _stlink_freq(build_parser().parse_args(["flash", "--freq", "6000"]), "ocd") == 6000)
        os.remove(config_file())
        chk("ST-Link 通道: 没有配置时是 auto", _stlink_channel(_a_auto) == "auto")
        # 记事本 / PowerShell 存的 json 带 BOM: 用 utf-8 读会抛异常 -> 整份配置被静默忽略(真机踩到)
        with open(config_file(), "wb") as _fh:
            _fh.write(b"\xef\xbb\xbf" + b'{"stlink_channel": "openocd"}')
        chk("工程配置: 带 BOM 也读得出来", _stlink_channel(_a_auto) == "openocd")
        merge_config({"probe": "stlink"})
        _cf = load_config()
        chk("工程配置: merge_config 是补写, 不冲掉原有设置",
            _cf.get("stlink_channel") == "openocd" and _cf.get("probe") == "stlink")
    finally:
        os.chdir(_cwd0)
        shutil.rmtree(_dch, ignore_errors=True)

    print("SELFTEST: %s" % ("PASS" if ok else "FAIL"))
    jset(checks=checks, result=("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _do_verify(elf, dev):
    """比对板子 Flash 与 ELF。返回 (ok, 消息)。"""
    objcopy = find_objcopy()
    info = _elf_load_info(elf) if elf and os.path.isfile(elf) else None
    if not objcopy or not info:
        return None, "无法校验(缺 arm-none-eabi-objcopy / readelf, 或 ELF 不可解析)"
    base, size = info
    tmp_bin = os.path.join(tempfile.gettempdir(), "stm32-dev-verify.bin")
    tmp_flash = os.path.join(tempfile.gettempdir(), "stm32-dev-verify-flash.bin")
    try:
        subprocess.run([objcopy, "-O", "binary", elf, tmp_bin], capture_output=True, timeout=60, check=True)
    except Exception as e:
        return None, "无法校验(objcopy 失败: %s)" % e
    out = run_jlink_script(dev, ["savebin %s 0x%08X 0x%X" % (tmp_flash.replace("\\", "/"), base, size), "q"],
                           timeout=max(60, size // 1024))
    if "O.K." not in out:
        return None, "无法校验(读回板子 Flash 失败)"
    a = open(tmp_bin, "rb").read()
    b = open(tmp_flash, "rb").read()
    n = min(len(a), len(b))
    diffs = sum(1 for i in range(n) if a[i] != b[i]) + abs(len(a) - len(b))
    for f in (tmp_bin, tmp_flash):
        try:
            os.remove(f)
        except OSError:
            pass
    return (diffs == 0), ("板子固件与 ELF 一致(%d 字节)" % size if diffs == 0
                          else "板子固件与 ELF 不一致(%d 字节不同)" % diffs)


_RTT_REPO_RAW = "https://raw.githubusercontent.com/SEGGERMicro/RTT/main/"

_RTT_REPO_FILES = [
    ("RTT/SEGGER_RTT.c", "SEGGER_RTT.c"),
    ("RTT/SEGGER_RTT.h", "SEGGER_RTT.h"),
    ("RTT/SEGGER_RTT_printf.c", "SEGGER_RTT_printf.c"),
    ("RTT/SEGGER_RTT_ASM_ARMv7M.S", "SEGGER_RTT_ASM_ARMv7M.S"),
    ("RTT/SEGGER_RTT_ConfDefaults.h", "SEGGER_RTT_ConfDefaults.h"),
    ("Config/SEGGER_RTT_Conf.h", "SEGGER_RTT_Conf.h"),
]


def _fetch_rtt_sources(dst, verbose=False, offline=False):
    """从 SEGGER 官方仓库(BSD 许可)取 RTT 源码。返回已写入的文件名; 关键文件没拿到就返回 []。

    路径按仓库实际布局: RTT/ 下是 .c/.h 与汇编加速件, Config/ 下是 SEGGER_RTT_Conf.h。
    """
    if offline:
        print("   --offline: 跳过联网下载。")
        return []
    import urllib.request
    got = []
    for rel, name in _RTT_REPO_FILES:
        url = _RTT_REPO_RAW + rel
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read()
            if not data:
                raise ValueError("空文件")
            with open(os.path.join(dst, name), "wb") as fh:
                fh.write(data)
            got.append(name)
            if verbose:
                print("   %-28s <- %s (%d 字节)" % (name, rel, len(data)))
        except Exception as e:
            print("   [没取到] %s: %s" % (name, e))
    need = ("SEGGER_RTT.c", "SEGGER_RTT.h")
    if not all(n in got for n in need):
        return []
    print("从官方仓库取到 %d 个文件: %s" % (len(got), _RTT_REPO_RAW))
    return got

def cmd_init_rtt(args):
    """把 SEGGER RTT 源码放进工程: 先看 J-Link 安装目录自带没, 没有再联网从官方仓库取。

    与探针无关 —— RTT 是固件侧的事, 三种调试器都能用(抓包通道不同: J-Link 原生 / 其余走 OpenOCD)。
    """
    d = args.dir or "."
    dst = os.path.join(d, args.subdir)
    os.makedirs(dst, exist_ok=True)
    want = ["SEGGER_RTT.c", "SEGGER_RTT.h", "SEGGER_RTT_Conf.h",
            "SEGGER_RTT_ConfDefaults.h", "SEGGER_RTT_printf.c",
            "SEGGER_RTT_ASM_ARMv7M.S"]
    src_dir = None
    for base in WINDOWS_JLINK_DIRS + UNIX_JLINK_DIRS:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            if "SEGGER_RTT.c" in files:
                src_dir = root
                break
        if src_dir:
            break
    copied = []
    if src_dir:
        for name in want:
            s = os.path.join(src_dir, name)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(dst, name))
                copied.append(name)
        print("从 J-Link 安装目录复制 RTT 源码: %s" % src_dir)
    if not copied:
        print("!! J-Link 安装目录里没找到 RTT 源码(新版安装包常不含) -> 改从 SEGGER 官方仓库取。")
        copied = _fetch_rtt_sources(dst, verbose=args.verbose, offline=args.offline)
    if not copied:
        print("   手动取法(BSD 许可): https://github.com/SEGGERMicro/RTT")
        print("   需要: " + ", ".join(want))
        print("         .c/.h 与汇编件在 RTT/ 下, SEGGER_RTT_Conf.h 在 Config/ 下")
        return 1
    print("已复制到 %s:" % dst)
    for n in copied:
        print("   " + n)
    print("""
--- 接线步骤 ---
1) 编译加入: SEGGER_RTT.c 和 SEGGER_RTT_printf.c; 头文件路径加 %s
2) Cortex-M3/M4/M7 默认会启用汇编加速(RTT_USE_ASM=1), 但需要把
   SEGGER_RTT_ASM_ARMv7M.S 也加进构建; 不想加就在 SEGGER_RTT_Conf.h 里写:
     #define RTT_USE_ASM (0)
3) 代码里: #include "SEGGER_RTT.h"; 启动时 SEGGER_RTT_Init();
   之后用 SEGGER_RTT_printf(0, "seq=%%u tick=%%u\r\n", ...) 打日志(%%u 是 RTT 自带格式化)。
4) 抓包: python stm32-dev.py rtt --elf <elf> --check-seq
5) 默认缓冲 上行 1KB/下行 16B, 模式 NO_BLOCK_SKIP(满则丢, 不阻塞固件)。""" % dst)


def load_svd(svd_path):
    """解析 SVD XML -> {外设: (base, {寄存器: (offset, [(字段, bitOffset, bitWidth, {值:名})])})}。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(svd_path).getroot()
    except Exception as e:
        return None

    def txt(node, tag):
        e = node.find(tag)
        return e.text.strip() if (e is not None and e.text) else None

    periphs = {}
    for p in root.iter("peripheral"):
        name = txt(p, "name")
        base_s = txt(p, "baseAddress")
        if not name or not base_s:
            continue
        try:
            base = int(base_s, 0)
        except ValueError:
            continue
        regs = {}
        for r in p.iter("register"):
            rname = txt(r, "name")
            off_s = txt(r, "addressOffset")
            if not rname or not off_s:
                continue
            try:
                off = int(off_s, 0)
            except ValueError:
                continue
            fields = []
            for f in r.iter("field"):
                fname = txt(f, "name")
                if not fname:
                    continue
                bo, bw = txt(f, "bitOffset"), txt(f, "bitWidth")
                if bo is None:
                    br = txt(f, "bitRange")          # [msb:lsb]
                    if br and ":" in br:
                        msb, lsb = br.strip("[]").split(":")
                        bo, bw = lsb, str(int(msb) - int(lsb) + 1)
                if bo is None or bw is None:
                    continue
                enum = {}
                for ev in f.iter("enumeratedValue"):
                    en, ev_v = txt(ev, "name"), txt(ev, "value")
                    if en and ev_v:
                        try:
                            enum[int(ev_v, 0)] = en
                        except ValueError:
                            pass
                try:
                    fields.append((fname, int(bo, 0), int(bw, 0), enum))
                except ValueError:
                    continue
            regs[rname] = (off, fields)
        periphs[name] = (base, regs)
    return periphs


def cmd_svd(args):
    """查 SVD / 读寄存器并用 SVD 解码字段。

    svd --elf build/x.elf                 # 只定位 SVD 文件
    svd --elf build/x.elf GPIOA.MODER     # 读该寄存器并解码位域
    svd --elf build/x.elf 0x40020000      # 按裸地址读
    """
    dev = args.device or (infer_device_generic(args.elf) if args.elf else None)
    if not dev:
        print("无法确定芯片。请用 --device 指定, 或 --elf 编译产物。")
        return 1
    svd = find_svd(dev)
    if not svd:
        print("未找到 %s 的 SVD。获取方式:" % dev)
        print("  - STM32CubeCLT 里有: C:\\ST\\STM32CubeCLT_*\\STMicroelectronics_CMSIS_SVD\\<chip>.svd")
        print("  - ST 官网下载对应器件 pack / 数据手册 SVD")
        print("  - 或从 github cmsis-svd 镜像拉取")
        print("  然后设环境变量 SVD_SEARCH_DIR 或放入本技能目录")
        return 1
    print("SVD: %s" % svd)
    if not args.expr:
        print("用法: svd --elf <elf> GPIOA.MODER   # 读寄存器并解码; 或 0x<裸地址>")
        return
    periphs = load_svd(svd)
    if not periphs:
        print("!! SVD 解析失败。")
        return 1
    expr = args.expr
    addr = None
    fields = []
    label = expr
    if expr.lower().startswith("0x"):
        try:
            addr = int(expr, 16)
        except ValueError:
            print("地址格式不对: %s" % expr)
            return 1
    else:
        if "." not in expr:
            print("格式应为 <外设>.<寄存器>, 如 GPIOA.MODER (或裸地址 0x...)")
            return 1
        pname, rname = expr.split(".", 1)
        hit = None
        for k in periphs:
            if k.upper() == pname.upper():
                hit = k
                break
        if hit is None:
            print("SVD 里没有外设 %s。可用示例: %s" % (pname, ", ".join(sorted(periphs)[:12])))
            return 1
        base, regs = periphs[hit]
        rhit = None
        for k in regs:
            if k.upper() == rname.upper():
                rhit = k
                break
        if rhit is None:
            # ST 的 SVD 常用扁平名(外设_寄存器, 如 RCC 下的 RCC_CFGR), 允许只写短名。
            alt = "%s_%s" % (hit, rname)
            for k in regs:
                if k.upper() == alt.upper():
                    rhit = k
                    break
        if rhit is None:
            print("SVD 里 %s 没有寄存器 %s。可用示例: %s"
                  % (hit, rname, ", ".join(sorted(regs)[:15])))
            return 1
        off, fields = regs[rhit]
        addr = base + off
        label = "%s.%s" % (hit, rhit)
    out = with_server(args.device, args.elf or infer_elf(), ["x/1xw 0x%08X" % addr],
                      resume=not args.keep_halted)
    m = re.search(r"0x([0-9a-fA-F]{8})", out.split(":")[-1] if ":" in out else out)
    if not m:
        print(out)
        print("!! 读取失败。")
        return 1
    val = int(m.group(1), 16)
    print("%s @0x%08X = 0x%08X" % (label, addr, val))
    if not fields:
        return
    shown = 0
    for fname, bo, bw, enum in sorted(fields, key=lambda x: x[1]):
        v = (val >> bo) & ((1 << bw) - 1)
        if v == 0 and not enum and not args.all:
            continue
        name = enum.get(v, "")
        print("  %-18s bit[%2d:%2d] = 0x%X%s" % (fname, bo + bw - 1, bo, v,
                                                 ("  (" + name + ")") if name else ""))
        shown += 1
    if shown == 0:
        print("  (所有字段为 0; 加 --all 显示全部)")
    return 0


def _unescape(s):
    """把 \\n \\r \\t \\xNN 转成真实字节。"""
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "n":
                out.append(10); i += 2; continue
            if n == "r":
                out.append(13); i += 2; continue
            if n == "t":
                out.append(9); i += 2; continue
            if n == "0":
                out.append(0); i += 2; continue
            if n == "x" and i + 3 < len(s) + 1:
                try:
                    out.append(int(s[i + 2:i + 4], 16)); i += 4; continue
                except ValueError:
                    pass
            out.append(ord(n)); i += 2; continue
        out.extend(c.encode("utf-8"))
        i += 1
    return bytes(out)


def _rtt_hex_bytes(s):
    """把 "70,0A" / "70 0a" / "0x70 0x0a" 都吃成字节; 解析不了返回 None。"""
    if not s:
        return None
    try:
        return bytes.fromhex(re.sub(r"0[xX]|,", " ", s))
    except ValueError:
        return None


def _tail_text(path, n=6):
    """读文件末尾 n 行(读不到就返回空串)。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        return " | ".join(lines[-n:])
    except Exception:
        return ""


def _ocd_start_rtt(argv, port, logfile):
    """起一个 openocd, 等它的 RTT server 把 TCP 端口挂上。

    返回 (proc, sk, before_pids); 失败返回 (proc, None, before) —— 调用方负责收场。
    注意: Windows 下 openocd 常是 .cmd 包装件(cmd /c), 只杀 cmd 会留下 openocd.exe 占着探针,
    所以同时记下启动前已存在的 openocd pid, 收场时杀差集。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    before = set(_pids_of("openocd"))
    lf = open(logfile, "wb")
    proc = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, creationflags=flags)
    sk = None
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            sk = socket.create_connection(("127.0.0.1", port), 0.5)
            break
        except OSError:
            time.sleep(0.3)
    proc._sd_log = lf          # 挂在对象上, 收场时一起关
    return (proc, sk, before)


def _ocd_finish(proc, sk, before):
    """收场: 关 socket、杀 openocd 及其子进程、关日志句柄。"""
    try:
        if sk is not None:
            sk.close()
    except OSError:
        pass
    if proc is not None:
        _kill_pid(proc.pid)
        for pid in (set(_pids_of("openocd")) - before):
            _kill_pid(pid)
        lf = getattr(proc, "_sd_log", None)
        if lf is not None:
            try:
                lf.close()
            except Exception:
                pass


def _rtt_send_ocd(args, probe):
    """ST-Link / DAPLink 发 RTT 下行(往目标写): 用 OpenOCD 的 rtt server。

    OpenOCD 的 rtt server 是双向的(源码 src/server/rtt_server.c): 目标上行 -> socket,
    我们写 socket -> 目标的下行缓冲(rtt_write_channel)。所以 RTT 下行不是 J-Link 独有。
    固件侧仍然要 SEGGER_RTT_HasKey()/SEGGER_RTT_GetKey() 把数据取走。
    """
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    span = _rtt_addr_span(args, elf)
    if span is None:
        print("ERROR: 地址解析失败(要 0x 开头的十六进制): %s" % (args.address or ""))
        return 1
    addr, size = span
    ocd = find_openocd()
    if not ocd:
        print("ERROR: 找不到 OpenOCD —— ST-Link/DAPLink 的 RTT 下行靠它。装法见 SETUP.md, 或设 OPENOCD 环境变量。")
        return 1
    sn = args.serial or _SERIAL or ""
    port = _rtt_tcp_port(args)
    channel = getattr(args, "channel", 0)
    argv = _ocd_rtt_argv(ocd, probe, sn, dev, addr, size, port, channel)
    if not argv:
        print("ERROR: 认不出 %s 属于哪个 STM32 系列, OpenOCD 需要 -f target/<系列>x.cfg。" % dev)
        print("       -> 用 --device STM32G431CBT6 这样的完整订货号, 或给 --elf。")
        return 1
    payload = _rtt_hex_bytes(args.hex) if args.hex else _unescape(args.data or "")
    if not payload:
        print("ERROR: 没有要发送的数据(--data 或 --hex)。")
        return 1
    for srv in (JLinkServer(dev, GDB_PORT, sn), OpenOCDServer(dev, GDB_PORT, sn, probe)):
        try:
            if srv.is_up():
                print("检测到常驻 GDB server -> 先 stop(它和 RTT 抢同一台探针)")
                srv.stop()
                time.sleep(0.5)
        except Exception:
            pass
    logfile = os.path.join(tempfile.gettempdir(), "stm32-dev-openocd-rtt.log")
    try:
        os.remove(logfile)
    except OSError:
        pass
    print("== RTT 下行(OpenOCD + %s): device=%s channel=%d, 发 %d 字节 x%d =="
          % (PROBE_DEFS.get(probe, {}).get("name", probe), dev, channel, len(payload), max(1, args.repeat)))
    print("   " + " ".join(argv))
    proc, sk, before = _ocd_start_rtt(argv, port, logfile)
    rc = 1
    try:
        if sk is None:
            print("!! OpenOCD 没能把 RTT 服务挂到 127.0.0.1:%d(进程%s)。"
                  % (port, "已退出" if proc.poll() is not None else "还在跑"))
            tail = _tail_text(logfile)
            if tail:
                print("   后台日志: %s" % tail)
            else:
                print("   后台日志是空的(openocd 被强杀时缓冲没落盘)。手动跑一遍看现场:")
                print("     " + " ".join(argv))
            print("   常见原因: 控制块地址不对(板子跑的不是这份 ELF, 坑#9) / 核停在 halt / 探针被别的程序占着。")
        else:
            print("已连上 RTT 通道 %d; 固件里要用 SEGGER_RTT_HasKey()/SEGGER_RTT_GetKey() 取, 没取就被丢弃。" % channel)
            sk.settimeout(0.2)
            drain_end = time.time() + 0.4       # 先排掉环形缓冲里的旧数据, 免得 --expect 匹到开机日志
            while time.time() < drain_end:
                try:
                    if not sk.recv(4096):
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
            expect = re.compile(args.expect) if args.expect else None
            hits = 0
            for i in range(max(1, args.repeat)):
                t0 = time.perf_counter()
                sk.sendall(payload)
                buf = b""
                deadline = time.time() + max(0.2, args.timeout)
                while expect and time.time() < deadline:
                    try:
                        chunk = sk.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    if expect.search(buf.decode("utf-8", "replace")):
                        break
                ms = (time.perf_counter() - t0) * 1000
                txt = buf.decode("utf-8", "replace").strip()
                if expect:
                    if expect.search(txt):
                        hits += 1
                        print("[%d/%d] %.1f ms 命中: %s" % (i + 1, args.repeat, ms, txt[-200:]))
                    else:
                        print("[%d/%d] %.1f ms 未命中: %s" % (i + 1, args.repeat, ms, txt[-120:] or "<无数据>"))
                else:
                    print("[%d/%d] 已发 %d 字节%s"
                          % (i + 1, args.repeat, len(payload), ("" if not txt else "; 回包: " + txt[-200:])))
                if args.interval > 0 and i + 1 < args.repeat:
                    time.sleep(args.interval / 1000.0)
            if expect:
                print("命中 %d/%d" % (hits, args.repeat))
                jset(sent=args.repeat, hits=hits)
                rc = 0 if hits == args.repeat else 1
            else:
                jset(sent=args.repeat, bytes=len(payload))
                rc = 0
    finally:
        _ocd_finish(proc, sk, before)
    return rc

def cmd_rtt_send(args):
    """向 RTT 下行通道发数据(往目标写)。

    J-Link: pylink-square 直连 DLL(可选依赖: pip install pylink-square)。
    ST-Link / DAPLink: 走 OpenOCD 的 rtt server(TCP 双向, 见 _rtt_send_ocd)。
    固件侧用 SEGGER_RTT_HasKey()/SEGGER_RTT_GetKey() 取; 与常驻 GDB server 抢同一台探针(自动先停)。
    """
    probe = probe_for_work()
    if probe in ("stlink", "daplink"):
        return _rtt_send_ocd(args, probe)
    try:
        import pylink
    except ImportError:
        print("ERROR: J-Link 的 RTT 下行要 pylink-square -> pip install pylink-square")
        print("       替代一: ST-Link/DAPLink 不用 pylink, 直接 rtt-send(走 OpenOCD)")
        print("       替代二: 起 RTT server 后用 JLinkRTTClient(默认 localhost:19021) 手动交互")
        return 1
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    addr = args.address
    if not addr:
        sym = symbol_addr_from_elf(elf, "_SEGGER_RTT")
        if sym:
            addr = "0x%08X" % sym
    _warn_stale_jlink()
    payload = _rtt_hex_bytes(args.hex) if args.hex else _unescape(args.data or "")
    if not payload:
        print("ERROR: 没有要发送的数据(--data 或 --hex)。")
        return 1
    jl = pylink.JLink()
    try:
        jl.open()
        jl.set_tif(pylink.enums.JLinkInterfaces.SWD)
        jl.connect(dev, speed=args.speed)
        jl._dll.JLINKARM_Go()          # connect 会 halt, 先让 CPU 跑
        ok = False
        for _ in range(30):
            try:
                jl.rtt_start(block_address=int(addr, 16)) if addr else jl.rtt_start()
                ok = True
                break
            except Exception:
                try:
                    jl.rtt_start()
                    ok = True
                    break
                except Exception:
                    time.sleep(0.2)
        if not ok:
            print("ERROR: 找不到 RTT 控制块。用 --address 指定, 或确认固件已集成 RTT。")
            return 1
        time.sleep(0.3)
        jl.rtt_read(0, 65536)                       # 排空
        expect = re.compile(args.expect) if args.expect else None
        hits = 0
        for i in range(max(1, args.repeat)):
            t0 = time.perf_counter()
            jl.rtt_write(0, payload)
            if expect is None and args.repeat == 1:
                time.sleep(args.timeout)
            deadline = time.time() + args.timeout
            buf = b""
            while time.time() < deadline:
                d = jl.rtt_read(0, 65536)
                if d:
                    buf += bytes(d)
                    if expect and expect.search(buf.decode("utf-8", "replace")):
                        break
            ms = (time.perf_counter() - t0) * 1000
            txt = buf.decode("utf-8", "replace").strip()
            if expect:
                if expect.search(txt):
                    hits += 1
                    print("[%d/%d] %.1f ms 命中: %s" % (i + 1, args.repeat, ms, txt[-200:]))
                else:
                    print("[%d/%d] %.1f ms 未命中: %s" % (i + 1, args.repeat, ms, txt[-120:] or "<无数据>"))
            elif txt:
                print(txt[-500:])
            if args.interval > 0 and i + 1 < args.repeat:
                time.sleep(args.interval / 1000.0)
        if expect:
            print("命中 %d/%d" % (hits, args.repeat))
            jset(sent=args.repeat, hits=hits)
            return 0 if hits == args.repeat else 1
        jset(sent=args.repeat)
        return 0
    except Exception as e:
        print("ERROR: %s" % e)
        return 1
    finally:
        try:
            jl.rtt_stop()
        except Exception:
            pass
        try:
            jl.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 调试器(探针)选型层: 命令层不写死型号, 只问"这个探针能不能做 X"
#   优先级: --probe > 环境变量 STM32_DEV_PROBE > 工程根 .stm32-dev.json > 自动探测
# ---------------------------------------------------------------------------
def _which_or_dirs(names, dirs, env=None):
    """env 变量 > PATH > 常见安装目录; 找不到返回 None(不抛异常)。"""
    if env:
        v = os.environ.get(env)
        if v:
            if os.path.isfile(v):
                return v
            p = shutil.which(v)
            if p:
                return p
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for d in dirs:
        for n in names:
            cand = os.path.join(d, n)
            if os.path.isfile(cand):
                return cand
    return None


def _glob_first(patterns):
    """按 glob 找第一个存在的文件; 版本号大的优先。"""
    hits = []
    for pat in patterns:
        hits.extend(glob.glob(os.path.expanduser(pat)))
    for h in sorted(set(hits), reverse=True):
        if os.path.isfile(h):
            return h
    return None


def find_stm32_cli():
    """STM32_Programmer_CLI: 烧录+逐字节校验+选项字节+HardFault 分析(ST 官方)。"""
    env = os.environ.get("STM32_PROGRAMMER_CLI")
    if env and os.path.isfile(env):
        return env
    for n in ("STM32_Programmer_CLI", "STM32_Programmer_CLI.exe", "STM32_Programmer.sh"):
        p = shutil.which(n)
        if p:
            return p
    return _glob_first([
        "C:/ST/STM32CubeCLT_*/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe",
        "C:/Program Files/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe",
        "C:/Program Files (x86)/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe",
        "/opt/st/stm32cubeclt_*/STM32CubeProgrammer/bin/STM32_Programmer_CLI",
        "/usr/local/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI",
    ])


def find_openocd():
    """OpenOCD: 多探针通用的 gdb server, 也是非 J-Link 探针跑 RTT/SWO 的唯一通道。"""
    env = os.environ.get("OPENOCD")
    if env and os.path.isfile(env):
        return _unwrap_cmd(env)
    for n in ("openocd", "openocd.exe", "openocd.cmd", "openocd.bat"):
        p = shutil.which(n)
        if p:
            return _unwrap_cmd(p)
    return _unwrap_cmd(_glob_first([
        "~/.local/bin/openocd*",
        "~/.local/share/xpack*/bin/openocd*",
        "~/AppData/Local/Microsoft/WinGet/Links/openocd*",
        "/usr/bin/openocd", "/usr/local/bin/openocd", "/opt/homebrew/bin/openocd",
    ]))


def find_stlink_gdbserver():
    """ST-LINK_gdbserver: ST 官方 gdb server(半主机 / SWO 时钟分频)。"""
    env = os.environ.get("STLINK_GDB_SERVER")
    if env and os.path.isfile(env):
        return env
    for n in ("ST-LINK_gdbserver", "ST-LINK_gdbserver.exe"):
        p = shutil.which(n)
        if p:
            return p
    return _glob_first([
        "C:/ST/STM32CubeCLT_*/STLink-gdb-server/bin/ST-LINK_gdbserver.exe",
        "C:/Program Files/STMicroelectronics/STLink-gdb-server/bin/ST-LINK_gdbserver.exe",
        "/opt/st/stm32cubeclt_*/STLink-gdb-server/bin/ST-LINK_gdbserver",
    ])


def find_pyocd():
    """pyOCD: 可选, 列 CMSIS-DAP 探针 / 烧录更省事。"""
    env = os.environ.get("PYOCD")
    if env and os.path.isfile(env):
        return env
    for n in ("pyocd", "pyocd.exe"):
        p = shutil.which(n)
        if p:
            return p
    return None


PROBE_ALIASES = [
    ("jlink", ["jlink", "j-link", "segger", "jlinkv9", "jlinkv11", "jl"]),
    ("stlink", ["stlink", "st-link", "stlinkv2", "stlinkv3", "stlinkv3set", "stlinkv3mini",
                "stlinkv3minie", "v3set", "v3mini", "v3minie", "st"]),
    ("daplink", ["daplink", "dap", "cmsisdap", "cmsis-dap", "pyocd", "mbed", "picoprobe",
                 "link", "microbit", "c251"]),
]

CAP_LABELS = [
    ("flash", "烧录"),
    ("flash_verify", "烧后逐字节校验"),
    ("rtt_up", "RTT 实时日志(探针拉)"),
    ("rtt_down", "RTT 回灌(主机->目标)"),
    ("swo", "SWO/ITM trace"),
    ("vcp", "虚拟串口"),
    ("option_bytes", "选项字节/读保护"),
    ("recover", "救砖(UR/HOTPLUG)"),
    ("fault_analysis", "官方 HardFault 分析"),
    ("semihosting", "半主机"),
    ("multi_probe", "多探针选号"),
    ("swd_clock", "SWD 时钟上限"),
]

PROBE_DEFS = {
    "jlink": {
        "name": "SEGGER J-Link",
        "matches": ["J-Link", "J-Link V9/V10/V11", "J-Link EDU/PLUS"],
        "caps": {
            "flash": "JLink.exe Commander(loadfile + 刷后校验)",
            "flash_verify": "可选(先 readback 再比)",
            "rtt_up": "native: 探针直接读 RAM 环缓冲, MB/s 级",
            "rtt_down": "native: JLinkRTTClient 端口 19021",
            "swo": "完整(SWO + 并行 trace)",
            "vcp": "无(J-Link 不带串口)",
            "option_bytes": "要自己拼 Commander 脚本",
            "recover": "一般(靠复位策略)",
            "fault_analysis": "无(自己读 CFSR/HFSR)",
            "semihosting": "GDB + SEGGER 半主机",
            "multi_probe": "USB=<序列号>",
            "swd_clock": "最高(默认给 4000kHz, 可上万)",
        },
        "best": "RTT 实时双向日志(唯一能边跑边高速读日志的), 跨厂商芯片全支持, 多探针选号稳, 量产/多核生态最全。",
        "notes": [
            "J-Link 驱动用 SEGGER 私有栈, openocd 用 libusb, 二者同时抢同一根 J-Link 会 LIBUSB_ERROR_NOT_FOUND; 用哪个探针就只开哪条通道。",
            "JLinkGUIServer/JLinkRemoteServer 会残留(技能里要 taskkill, 否则下次连不上)。",
            "RTT Logger 与 JLinkGDBServerCL 互斥(同一台 J-Link 只能被一个进程独占); 抓 RTT 前必须先 stop 掉常驻 server。",
            "RTT 要固件侧集成 SEGGER RTT 源码(技能 init-rtt 就是干这个), 探针自带 RTT 不等于目标能用。",
            "J-Link 自动搜索 RTT 控制块在本板会失败(报 RTT Control Block not found), 必须给 --address(ELF 里 _SEGGER_RTT 的地址)。",
            "EDU 版本授权禁止商用; 商业产品要 Base/Plus 级。",
        ],
        "degrade": "没有 J-Link 时: 烧录/复位走 ST-Link 或 DAPLink(OpenOCD); RTT 换 OpenOCD 的 rtt(吞吐降 1~2 个数量级, 丢帧判定要打折)。",
        "tools": [
            ("jlink_gdbserver", "JLinkGDBServerCL(gdb server)", find_jlink_server, "装 SEGGER J-Link 驱动, 或设 JLINK_GDB_SERVER"),
            ("jlink_rtt", "JLinkRTTLogger(RTT 抓包)", find_rtt_logger, "同上; 新安装包缺它就换 OpenOCD rtt"),
            ("jlink_exe", "JLink.exe(烧录/复位/ShowEmuList)", find_jlink_cmd, "装 SEGGER J-Link 驱动"),
        ],
    },
    "stlink": {
        "name": "ST-Link V2 / V2-1 / V3(世代不同, 能力差得远, 技能按世代自动选参数)",
        "matches": ["ST-Link V2(独立小 U 盘/克隆件最多)", "ST-Link V2-1(Nucleo/Discovery 板载)",
                    "ST-Link V3SET", "ST-Link V3MINI", "ST-Link V3MINIE"],
        "caps": {
            "flash": "STM32_Programmer_CLI -w(有返回码) 或 OpenOCD program",
            "flash_verify": "STM32_Programmer_CLI -w -v(官方逐字节校验, 退出码可信)",
            "rtt_up": "OpenOCD rtt(轮询 RAM, 吞吐低 1~2 个数量级)",
            "rtt_down": "OpenOCD rtt server 是双向的(rtt-send 走它, 不用额外库)",
            "swo": "V3 的 SWO 可用, 但 OpenOCD hla 驱动要手配 swo/tpiu(单 AP 限制)",
            "vcp": "V3 与板载 V2-1 有虚拟串口(一根 USB 同时给 SWD + 串口); 独立 V2 没有",
            "option_bytes": "STM32_Programmer_CLI -ob(读保护/BOR/WRP/boot 全支持)",
            "recover": "最好: -c mode=UR/HOTPLUG + -rdu 读保护救援",
            "fault_analysis": "自带: STM32_Programmer_CLI -hf 分析 HardFault",
            "semihosting": "ST-LINK_gdbserver --semihosting 支持",
            "multi_probe": "-i <序列号> 或 OpenOCD adapter serial",
            "swd_clock": "按世代: V2 约 1.8MHz(实测), V3 默认 8MHz(最高 24MHz)",
        },
        "best": "调 STM32 的官方一等公民: 烧录带逐字节校验和真退出码, 选项字节/读保护/救砖最顺手, 还自带 HardFault 分析器和虚拟串口。",
        "notes": [
            "STM32_Programmer_CLI 和 ST-LINK_gdbserver 会抢同一根 ST-Link; 烧录前先停 gdb server。",
            "本机可能同时存在 ST 的 stlinkserver.exe 和 CubeCLT 里那个, 两个都开就抢设备(现象是时好时坏)。",
            "克隆/山寨 ST-Link 会被官方 CLI 直接拒(报 DEV_CONNECT_ERR / not a genuine ST device): 技能会自动改走 OpenOCD, 烧录/校验/调试/RTT 一样能做。",
            "官方 CLI 与 OpenOCD 不能同时占同一只 ST-Link: 技能跑 CLI 前会先收掉自己留下的 OpenOCD; 是别人(IDE/ST-LINK_gdbserver)占着就得你自己关。",
            "OpenOCD 的 hla(ST-Link)驱动有单 AP 限制: SWO/RTT 要手动给 -ap-num/-baseaddr; SWO 时钟配错会让芯片调试口锁死到重新上电。",
            "OpenOCD RTT 是轮询内存, 抓包丢的行里混着工具侧丢帧, 不能再当固件丢帧证据。",
            "V3MINI/V3MINIE 是精简版, 是否引出 SWO 引脚要先看板子(只有 SWD 四针时用 RTT/虚拟串口, 别指望 SWO)。",
            "STM32_Programmer_CLI 只保 ST 自家芯片。",
        ],
        "degrade": "没有 ST-Link 时: 烧录/选项字节换 OpenOCD(DAPLink 或 J-Link); 官方 -hf 和 -rdu 这类 ST 专属功能就用不上了。",
        "tools": [
            ("stm32_cli", "STM32_Programmer_CLI(烧录/校验/选项字节/-hf)", find_stm32_cli, "装 STM32CubeCLT 或 STM32CubeProgrammer, 或设 STM32_PROGRAMMER_CLI"),
            ("openocd", "OpenOCD(gdb server + RTT + SWO)", find_openocd, "装 OpenOCD(winget install xpack-openocd), 或设 OPENOCD"),
            ("stlink_gdbserver", "ST-LINK_gdbserver(可选: 半主机/SWO 分频)", find_stlink_gdbserver, "随 STM32CubeCLT 安装"),
        ],
    },
    "daplink": {
        "name": "DAPLink / CMSIS-DAP",
        "matches": ["DAPLink", "CMSIS-DAP", "mbed", "Picoprobe", "Keil ULINK 的 DAP 模式"],
        "caps": {
            "flash": "OpenOCD program 或 pyocd flash",
            "flash_verify": "OpenOCD verify / pyocd 自带校验",
            "rtt_up": "OpenOCD rtt(同 ST-Link: 轮询, 吞吐低)",
            "rtt_down": "同 ST-Link: 理论上能回写, 技能没接",
            "swo": "看固件: 老版 DAPLink 没引出 SWO, v2 固件才有",
            "vcp": "多数带虚拟串口(固件决定)",
            "option_bytes": "无 ST 官方支持(要读手册手写 FLASH 寄存器)",
            "recover": "一般(靠 OpenOCD reset 配置)",
            "fault_analysis": "无(自己读 CFSR/HFSR 或走 OpenOCD)",
            "semihosting": "GDB + OpenOCD 半主机",
            "multi_probe": "OpenOCD cmsis_dap_serial",
            "swd_clock": "最高到 4~10MHz(看实现)",
        },
        "best": "便宜、开源、通吃各家芯片; pyOCD 列探针/烧录最省事, 没有授权限制。",
        "notes": [
            "STM32_Programmer_CLI 不认 DAPLink: ST 官方那套(选项字节/-hf/救砖)全都用不了, 只能走 OpenOCD/pyOCD。",
            "OpenOCD 要显式给 adapter driver cmsis-dap(或 cmsis-dap v2)和 transport swd。",
            "中文克隆 CMSIS-DAP 的 VID/PID 五花八门, 认型号别只看名字, 要看 USB 描述符。",
            "SWO 是否能用取决于 DAPLink 固件版本, 不确定就先按没有 SWO 规划(用 RTT 或串口)。",
        ],
        "degrade": "DAPLink 只适合当烧录/断点通道; 要 RTT 就上 OpenOCD rtt, 要选项字节/救砖就换 ST-Link。",
        "tools": [
            ("openocd", "OpenOCD(烧录 + gdb + RTT)", find_openocd, "装 OpenOCD, 或设 OPENOCD"),
            ("pyocd", "pyOCD(可选: 列探针/烧录更省事)", find_pyocd, "pip install pyocd"),
        ],
    },
}

CONFIG_NAME = ".stm32-dev.json"


def _norm_probe(name):
    """探针名归一化: J-Link / stlinkv3 / cmsis-dap -> jlink/stlink/daplink; 认不出返回 ''。"""
    n = (name or "").strip().lower().replace("_", "-").replace(" ", "")
    if not n:
        return ""
    for pid, aliases in PROBE_ALIASES:
        if n == pid:
            return pid
    for pid, aliases in PROBE_ALIASES:
        for a in aliases:
            if a and (n == a or a in n):
                return pid
    return ""


def config_file(root=None):
    return os.path.join(root or os.getcwd(), CONFIG_NAME)


def load_config(root=None):
    """读工程根配置; 读不到就给空 dict(不是错误)。"""
    path = config_file(root)
    try:
        # utf-8-sig: 记事本/PowerShell 存过的配置带 BOM, 用 utf-8 读会直接抛异常 -> 整份配置被静默忽略。
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def merge_config(patch, root=None):
    """往工程配置里补几条, 不碰已经有的。

    踩到过: 回退到 OpenOCD 时 save_config({"stlink_channel": "openocd"}) 会把工程配置里
    原有的 probe/serial 整份冲掉 —— 配置是"工程级设置", 只能补写, 不能整份覆盖。"""
    cfg = load_config(root)
    cfg.update(patch or {})
    return save_config(cfg, root)


def save_config(cfg, root=None):
    path = config_file(root)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception as e:
        raise SkillError("写配置失败 %s: %s" % (path, e))
    return path


# ---------------------------------------------------------------------------
# ST-Link 的"世代"(版本)识别: V2 / V2-1 / V3 的能力差得很远, 选路和参数都跟着变
#   真机踩到: ST-Link/V2(克隆件) 官方 CLI 报 DEV_CONNECT_ERR, OpenOCD 却能连,
#             而且 OpenOCD 要 2000kHz 只给 1800kHz -> 时钟要按这一代给, 不能一律 4000/8000。
# ---------------------------------------------------------------------------
_STLINK_PID_VARIANTS = {
    "3744": "v2", "3748": "v2", "3752": "v2",            # ST-Link/V2(独立小U盘)与克隆件
    "374B": "v2-1", "374D": "v2-1",                      # 板载 V2-1(Nucleo/Discovery)
    "374E": "v3", "374F": "v3", "3753": "v3", "3754": "v3",
}

_STLINK_PROFILE = {
    "v2": {
        "label": "ST-Link/V2(独立小 U 盘那种; 克隆件最多)",
        "freq_cli": 4000, "freq_ocd": 1800,
        "caps": {
            # 键名必须和基础能力表一致, 否则是"多出一行"而不是"覆盖这一行"
            "swd_clock": "1.8 MHz(OpenOCD 实测; 官方 CLI 走 4000)",
            "vcp": "没有(独立 V2 不带虚拟串口)",
            "swo": "引脚上有, 克隆件常没引出; 通道慢(约 1~2 Mbit/s)",
        },
        "notes": [
            "克隆件会被 ST 官方 CLI 拒(DEV_CONNECT_ERR / not a genuine ST device) -> 技能自动改走 OpenOCD, 烧录/校验/调试/RTT 一样能做",
            "官方 CLI 与 OpenOCD 不能同时占同一只 ST-Link: 跑 CLI 前技能会先把残留的 OpenOCD 收掉(收不干净 CLI 就报 DEV_CONNECT_ERR)",
            "STM32CubeIDE / ST-LINK_gdbserver / 另一个 openocd 也会抢它, 报连不上先想这个",
        ],
    },
    "v2-1": {
        "label": "ST-Link/V2-1(Nucleo / Discovery 板载)",
        "freq_cli": 4000, "freq_ocd": 1800,
        "caps": {
            "swd_clock": "1.8~4 MHz(和 V2 同档)",
            "vcp": "有(板载, 和 SWD 共用同一根 USB)",
            "swo": "有(要引线)",
        },
        "notes": [
            "板载版跟着开发板一起上电/复位, 调试时别把整板 USB 拔了",
            "虚拟串口和 SWD 共用一根 USB: 串口观测与调试可以同时开, 不互相抢",
        ],
    },
    "v3": {
        "label": "ST-Link/V3(V3SET / V3MINI / V3MINIE)",
        "freq_cli": 8000, "freq_ocd": 4000,
        "caps": {
            "swd_clock": "8 MHz(最高可到 24 MHz)",
            "vcp": "有(V3SET 有 3 个)",
            "swo": "有, 而且快得多(高速 trace 通道)",
        },
        "notes": [
            "V3 的 SWD 时钟和虚拟串口都更多, 大工程烧录/单步明显更顺",
            "V3MINI / V3MINIE 是精简版: 没 V3SET 那些额外接口, 但高速 SWD/SWO 都在",
        ],
    },
}

_STLINK_VAR_CACHE = None


def _stlink_variant(fw="", board="", vidpid=""):
    """这只 ST-Link 是哪一代: v2 / v2-1 / v3(判不出来返回空串, 不猜)。
    依据优先级: USB PID(硬事实) > 固件串 V2J../V3J..(再用板载板名区分 V2 还是 V2-1)。"""
    m = re.search(r"([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})", vidpid or "")
    if m:
        v = _STLINK_PID_VARIANTS.get(m.group(2).upper())
        if v:
            return v, "USB PID %s" % m.group(2).upper()
    f = (fw or "").strip().upper()
    m = re.match(r"V(\d)", f)
    if m:
        if m.group(1) == "3":
            return "v3", "固件 %s" % f
        if m.group(1) == "2":
            if (board or "").strip():
                return "v2-1", "固件 %s + 板载板名" % f
            return "v2", "固件 %s" % f
    return "", ""


def _openocd_probe_line(verbose=False):
    """跑一次 openocd 看它怎么描述探针, 例: STLINK V2J46S7 (API v2) VID:PID 0483:3748。
    官方 CLI 认不出克隆件时, 这行是唯一能拿到探针型号/版本的地方。"""
    if OpenOCDServer("", GDB_PORT, "", "stlink").is_up():
        return ""      # 已经有 openocd 占着探针, 别再起一个去抢
    ocd = find_openocd()
    if not ocd:
        return ""
    tgt = _ocd_target("STM32G431CB") or "stm32g4x"
    argv = _argv_for(ocd, "-f", "interface/stlink.cfg", "-c", "transport select swd",
                     "-f", "target/%s.cfg" % tgt, "-c", "init", "-c", "shutdown")
    out = _run_quiet(argv, timeout=25)
    for ln in out.splitlines():
        up = ln.upper()
        if "STLINK" in up:
            # 去掉 openocd 的日志前缀(例 "Info : STLINK V2J46S7 (API v2) VID:PID 0483:3748")
            return ln[up.index("STLINK"):].strip()
    return ""


def _stlink_variant_probe(deep=False, verbose=False):
    """现在插着的 ST-Link 是哪一代 -> (variant, 依据)。deep=True 时允许跑一次 openocd 兜底。"""
    global _STLINK_VAR_CACHE
    if _STLINK_VAR_CACHE:
        return _STLINK_VAR_CACHE
    res = ("", "")
    for h in detect_stlink(verbose=verbose):
        v, why = _stlink_variant(h.get("fw", ""), h.get("board", ""), "")
        if v:
            res = (v, "官方 CLI: " + why)
            break
    if not res[0] and deep:
        v, why = _stlink_variant("", "", _openocd_probe_line(verbose=verbose))
        if v:
            res = (v, "OpenOCD: " + why)
    if res[0]:
        _STLINK_VAR_CACHE = res
    return res


def _stlink_profile(variant=""):
    return _STLINK_PROFILE.get(variant) or {}


def _caps_of(pid, variant=""):
    """这个探针(含具体世代)实际能做到什么: 基础能力表 + 世代覆盖。"""
    caps = dict(PROBE_DEFS.get(pid, {}).get("caps", {}))
    if pid == "stlink":
        caps.update(_stlink_profile(variant).get("caps", {}))
    return caps


def _stlink_freq(args=None, channel="ocd"):
    """SWD 时钟给多少: --freq > 工程配置 > 按这一代 ST-Link 的最优值(V2 只有 1.8MHz)。"""
    f = getattr(args, "freq", None) if args else None
    if f:
        return int(f)
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    if cfg.get("stlink_freq"):
        return int(cfg["stlink_freq"])
    v, _why = _stlink_variant_probe()
    p = _stlink_profile(v)
    if p:
        return int(p["freq_cli"] if channel == "cli" else p["freq_ocd"])
    return 4000 if channel == "cli" else 1800


def _ocd_speed_arg(probe="stlink", args=None):
    """OpenOCD 的 adapter speed: 按这一代 ST-Link 给最优值(给 V2 要 2000 它也只能 1800)。"""
    if probe != "stlink":
        return []
    return ["-c", "adapter speed %d" % _stlink_freq(args, "ocd")]


def _unwrap_cmd(path):
    """openocd / pyocd 这类工具可能是 .cmd/.bat 包装件: 把里面真正的 exe 抠出来。
    直接起包装件的话我们拿到的 pid 是 cmd.exe, 杀它会留下真进程当孤儿 —— 孤儿占着探针,
    ST 官方 CLI 就报 DEV_CONNECT_ERR(真机踩过)。"""
    if not path or os.name != "nt" or not path.lower().endswith((".cmd", ".bat")):
        return path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return path
    for m in re.finditer(r'([A-Za-z]:\\[^"\r\n]*?\.exe)', txt):
        cand = m.group(1).strip()
        if os.path.isfile(cand):
            return cand
    return path


def _argv_for(tool, *extra):
    """Windows 上 .cmd/.bat 不能直接 CreateProcess, 要用 cmd /c 起(openocd.cmd 就是这种)。"""
    if os.name == "nt" and tool.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", tool] + list(extra)
    return [tool] + list(extra)


def _run_quiet(argv, timeout=20):
    """跑一个命令拿 stdout; 任何失败都返回空串(探测/体检不许把技能搞崩)。"""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def detect_jlink(verbose=False):
    """用 JLink.exe 的 ShowEmuList 列 J-Link(顺带拿型号, 用来分辨 V9/V11)。"""
    exe = find_jlink_cmd()
    if not exe:
        if verbose:
            print("    [jlink] 没找到 JLink.exe(装 SEGGER 驱动, 或设 JLINK_CMD)")
        return []
    scr = os.path.join(tempfile.gettempdir(), "stm32-dev-showemu.jlink")
    try:
        with open(scr, "w", encoding="ascii", errors="replace") as fh:
            fh.write("ShowEmuList\nexit\n")
    except Exception:
        return []
    out = _run_quiet(_argv_for(exe, "-CommanderScript", scr), timeout=25)
    found = []
    for m in re.finditer(r"Serial number:\s*(\d+)(?:,\s*ProductName:\s*([^\r\n]+))?", out):
        found.append({"probe": "jlink", "serial": m.group(1),
                      "model": (m.group(2) or "").strip(), "raw": out})
    if not found and verbose:
        print("    [jlink] JLink.exe 在, 但没列出探针(没插好 / 被别的进程占用)")
    return found


def detect_stlink(verbose=False):
    """用 STM32_Programmer_CLI -l 列 ST-Link(官方工具只在真 ST-Link 上才认)。"""
    cli = find_stm32_cli()
    out = ""
    if not cli:
        if verbose:
            print("    [stlink] 没找到 STM32_Programmer_CLI(装 STM32CubeCLT/CubeProgrammer, 或设 STM32_PROGRAMMER_CLI)")
    else:
        out = _run_quiet(_argv_for(cli, "-l"), timeout=30)
    blocks = re.split(r"(?=ST-Link Probe \d+:)", out)
    found = []
    for b in blocks:
        if "ST-Link Probe" not in b:
            continue
        sn = re.search(r"ST-LINK SN\s*:\s*(\S+)", b)
        fw = re.search(r"ST-LINK FW\s*:\s*(\S+)", b)
        bd = re.search(r"Board\s*:\s*([^\r\n]+)", b)
        fwv = (fw.group(1).strip() if fw else "")
        bdv = (bd.group(1).strip() if bd else "")
        snv = (sn.group(1).strip() if sn else "")
        # 真机踩到: 探针被别的程序占着时, CLI 的 -l 会吐 "5&1C422B18&0&7" 这种 USB 实例 ID 尾巴当串号,
        # 照单全收会把垃圾串号塞进 -c 连接串, 之后一律连不上。
        bad = bool(snv) and not re.fullmatch(r"[0-9A-Fa-f]{12,32}", snv)
        if bad:
            snv = ""
        var, why = _stlink_variant(fwv, bdv, "")
        found.append({"probe": "stlink", "serial": snv,
                      "model": (fwv + " " + bdv).strip(),
                      "fw": fwv, "board": bdv, "variant": var, "variant_why": why,
                      "cli_bad": bad, "raw": b})
    if not found:
        # 官方 CLI 认不出来时(被别的程序占着 / 克隆件 / 没装), 用 OpenOCD 真握一次手: 连得上就说明
        # 探针确实在, 这是"山寨 ST-Link 也能自动用上"的关键一步。只有真出现"看得见探针"的信号
        # 才跑 openocd, 免得压根没插探针时白等一秒多。
        blob = (out or "").lower()
        signal = (not cli) or ("dev_connect_err" in blob) or ("not a genuine st device" in blob) \
            or ("unable to connect to st-link" in blob) or ("st-link is not" in blob) \
            or ("error in initializing st-link" in blob)
        if signal:
            ln = _openocd_probe_line(verbose=verbose)
            if ln:
                fwm = re.search(r"V(\dJ[A-Za-z0-9]+)", ln)
                vpm = re.search(r"([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})", ln)
                fwv = ("V" + fwm.group(1)) if fwm else ""
                var, why = _stlink_variant(fwv, "", (vpm.group(1) if vpm else ""))
                found.append({"probe": "stlink", "serial": "", "model": ln, "fw": fwv,
                              "board": "", "variant": var, "variant_why": why,
                              "cli_bad": bool(cli), "via": "openocd", "raw": ln})
    if not found and verbose:
        m = re.search(r"Error[^\r\n]*", out or "")
        print("    [stlink] 官方 CLI 没认出 ST-Link" + ("(" + m.group(0).strip() + ")" if m else "(可能没插 / 克隆件)"))
    return found


_PNP_VIDS = {
    "1366": ("jlink", "SEGGER J-Link"),
    "0483": ("stlink", "ST-Link / STM32(VID 0483, 也可能是 VCP)"),
    "0D28": ("daplink", "DAPLink / mbed (VID 0D28)"),
    "1209": ("daplink", "DAPLink / Picoprobe (VID 1209)"),
    "2E8A": ("daplink", "Raspberry Pi Pico (VID 2E8A)"),
    "C251": ("daplink", "DAPLink (VID C251)"),
}


def _pnp_probes(verbose=False):
    """Windows 兜底: 官方工具都认不出来时, 按 USB VID 看插了什么。"""
    if os.name != "nt":
        return []
    ps = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
          "Where-Object { $_.InstanceId -match 'VID_(1366|0483|0D28|1209|2E8A|C251)' } | "
          "ForEach-Object { $_.InstanceId }")
    out = _run_quiet(["powershell", "-NoProfile", "-Command", ps], timeout=25)
    saw = {}
    for line in out.splitlines():
        m = re.search(r"VID_([0-9A-Fa-f]{4})", line)
        if not m:
            continue
        pid, label = _PNP_VIDS.get(m.group(1).upper(), ("", ""))
        if pid:
            saw[pid] = label
    return [{"probe": p, "serial": "", "model": saw[p], "raw": ""} for p in sorted(saw)]


def detect_daplink(verbose=False):
    """pyocd 优先(能拿序列号); 没装 pyocd 就靠 USB VID 兜底认插没插。"""
    exe = find_pyocd()
    if exe:
        out = _run_quiet(_argv_for(exe, "list", "--probes"), timeout=25)
        if out.strip():
            return [{"probe": "daplink", "serial": "", "model": "", "raw": out}]
        if verbose:
            print("    [daplink] pyocd 在, 但没列出探针")
    elif verbose:
        print("    [daplink] 没装 pyocd(只能看 USB 有没有插, 认不出型号): pip install pyocd")
    return []


def detect_probes(only="", verbose=False):
    out = {}
    ids = [only] if only else ["jlink", "stlink", "daplink"]
    if "jlink" in ids:
        out["jlink"] = detect_jlink(verbose)
    if "stlink" in ids:
        out["stlink"] = detect_stlink(verbose)
    if "daplink" in ids:
        out["daplink"] = detect_daplink(verbose)
    return out


def resolve_probe(args=None, cfg=None, verbose=False):
    """定这次用哪个探针。返回 (probe_id, serial, 依据文字)。"""
    explicit = getattr(args, "probe", None) if args else None
    if explicit:
        pid = _norm_probe(explicit)
        if not pid:
            raise SkillError("认不出这个探针: %s(可用: jlink / stlink / daplink)" % explicit)
        serial = (getattr(args, "serial", "") or "").strip()
        if serial == DEFAULT_SERIAL:
            serial = ""
        return pid, serial, "--probe"
    env = os.environ.get("STM32_DEV_PROBE", "")
    if env:
        pid = _norm_probe(env)
        if pid:
            return pid, "", "环境变量 STM32_DEV_PROBE"
    cfg = cfg if cfg is not None else load_config()
    if cfg.get("probe"):
        pid = _norm_probe(cfg["probe"])
        if pid:
            return pid, cfg.get("serial", "") or "", CONFIG_NAME
    found = detect_probes(verbose=verbose)
    hit = [k for k in ("jlink", "stlink", "daplink") if found.get(k)]
    if len(hit) == 1:
        sn = found[hit[0]][0]["serial"] if found[hit[0]] else ""
        return hit[0], sn, "自动探测"
    if not hit:
        raise SkillError("没探测到任何调试器。插好 USB 后跑: stm32-dev.py probe detect --verbose")
    raise SkillError("同时探测到多个调试器(%s)。请指定: --probe <名字>, 或 probe use <名字>" % "/".join(hit))


def _probe_tool_status(pid):
    """这个探针要用的本机工具在不在。返回 [(label, 路径 或 None, 装法)]。"""
    out = []
    for _key, label, finder, hint in PROBE_DEFS[pid]["tools"]:
        try:
            path = finder()
        except Exception:
            path = None
        out.append((label, path, hint))
    return out


def _describe_effective(pid, serial, why):
    d = PROBE_DEFS[pid]
    jset(probe=pid, probe_name=d["name"], serial=serial, probe_source=why)
    print("调试器: %s(%s)" % (d["name"], pid))
    if pid == "stlink":
        _v, _vwhy = _stlink_variant_probe()
        if _v:
            _p = _stlink_profile(_v)
            jset(stlink_variant=_v)
            print("  这一代: %s   (%s)" % (_p["label"], _vwhy))
            print("  按这代最优: SWD %d kHz(OpenOCD) / %d kHz(官方 CLI)" % (_p["freq_ocd"], _p["freq_cli"]))
        else:
            print("  这一代: 没认出来(不猜); 插好探针跑 probe detect 看细节")
    if serial:
        print("  序列号: %s" % serial)
    print("  来源: %s" % why)
    return d


def probe_for_work(verbose=False):
    """这条命令该用哪个探针: 已定(参数/环境变量/工程配置)的直接用, 没定就快速探测一次。
    探测顺序 jlink -> stlink -> daplink, 找到第一个就停; 全无返回 ''(交给旧路径报错)。"""
    global _PROBE, _SERIAL, _PROBE_WARNED
    if _PROBE:
        return _PROBE
    for pid in ("jlink", "stlink", "daplink"):
        hits = (detect_probes(only=pid) or {}).get(pid) or []
        if hits:
            _PROBE = pid
            if not _SERIAL and hits[0].get("serial"):
                _SERIAL = hits[0]["serial"]
            if verbose:
                print("自动选中调试器: %s(%s)" % (PROBE_DEFS[pid]["name"], pid))
            return pid
    # 一个都没认出来: 说清楚, 别让后面按老路试 J-Link 的报错把方向带偏
    # (真机踩到: 假 CLI/克隆件下 ST-Link 认不出来 -> 一路走到 J-Link, 报"检查 J-Link 连接/供电")
    if not _PROBE_WARNED:
        _PROBE_WARNED = True
        print("提示: 没探测到任何调试器 -> 下面按老路试 J-Link; 插着探针却没认出来就看 probe detect, 或 probe use <jlink|stlink|daplink> 指定。")
    return ""


def _tool_line(label, path, hint):
    if path:
        print("  [有] %s" % label)
        print("       %s" % path)
    else:
        print("  [缺] %s" % label)
        print("       装法: %s" % hint)
    return bool(path)


def _find_dirs(patterns):
    out = []
    for pat in patterns:
        p = os.path.expanduser(pat)
        if any(c in p for c in "*?["):
            out.extend(glob.glob(p))
        elif os.path.isdir(p):
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# 空项目脚手架(new): 生成一个能直接 make / 烧录 / 调试的最小工程
# ---------------------------------------------------------------------------
_FLASH_CODE = {"6": "32K", "8": "64K", "B": "128K", "Z": "192K", "C": "256K",
               "D": "384K", "E": "512K", "F": "768K", "G": "1024K", "H": "1536K",
               "I": "2048K"}


def _stem_define(stem):
    """CMSIS 的器件宏大小写是固定的: stm32g431xx -> STM32G431xx, stm32f103x8 -> STM32F103x8。

    写错(全大写)的后果: stm32g4xx.h 认不出器件, RCC_* 这些寄存器位定义全没有 -> 编译报 undeclared。
    """
    return stem[:-2].upper() + stem[-2:]


def _dev_key(device):
    """订货号 -> CMSIS 家族键(前 9 位): STM32G431CBT6 -> STM32G431。"""
    return (device or "").upper()[:9]


def _cube_roots():
    return [os.environ.get("STM32_CUBE_REPO", ""),
            os.path.join(os.path.expanduser("~"), "STM32Cube", "Repository"),
            "C:/ST/STM32Cube/Repository"]


def find_cube_pack(device, verbose=False):
    """找本地 STM32Cube 固件包(里面才有 CMSIS 头文件 + ST 官方启动文件/链接脚本)。"""
    m = re.match(r"STM32([A-Z]{1,2})(\d)", (device or "").upper())
    if not m:
        return None
    letters = m.group(1)
    fam = letters if len(letters) == 2 else letters + m.group(2)
    best, best_path = "", ""
    for root in _cube_roots():
        if not root or not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.upper().startswith("STM32CUBE_FW_%s_" % fam.upper()):
                p = os.path.join(root, name)
                if os.path.isdir(p) and name > best:
                    best, best_path = name, p
    if verbose and best_path:
        print("    [cube] %s" % best_path)
    return best_path or None


def _cube_layout(pack):
    """从 Cube 包定位: 设备头目录 / CMSIS 内核头目录 / 启动文件目录 / system_*.c。"""
    dev_root = os.path.join(pack, "Drivers", "CMSIS", "Device", "ST")
    core_inc = os.path.join(pack, "Drivers", "CMSIS", "Include")
    if not os.path.isdir(dev_root) or not os.path.isdir(core_inc):
        return None
    for d in sorted(os.listdir(dev_root)):
        inc = os.path.join(dev_root, d, "Include")
        tpl = os.path.join(dev_root, d, "Source", "Templates")
        gcc = os.path.join(tpl, "gcc")
        if not (os.path.isdir(inc) and os.path.isdir(gcc)):
            continue
        try:
            sysc = [x for x in sorted(os.listdir(tpl)) if x.startswith("system_") and x.endswith(".c")]
        except OSError:
            sysc = []
        if not sysc:
            continue
        return {"dev_inc": inc, "core_inc": core_inc, "gcc": gcc,
                "system": os.path.join(tpl, sysc[0])}
    return None


def _pick_startup(gcc_dir, device):
    """选 ST 官方启动文件: 优先 <家族>xx.s, 否则按容量码选(STM32F103C8 -> startup_stm32f103x8.s)。"""
    key = _dev_key(device).lower()
    cands = sorted(glob.glob(os.path.join(gcc_dir, "startup_%s*.s" % key)))
    if not cands:
        cands = sorted(glob.glob(os.path.join(gcc_dir, "startup_%s*.s" % key[:8])))
    if not cands:
        return None, []
    names = [os.path.basename(c) for c in cands]

    def stem_of(p):
        b = os.path.basename(p)
        return b[len("startup_"):-2]

    exact = [c for c in cands if os.path.basename(c).lower() == "startup_%sxx.s" % key]
    if exact:
        return exact[0], stem_of(exact[0])
    if len(cands) == 1:
        return cands[0], stem_of(cands[0])
    code = (device or "").upper()[-1].lower()
    pick = [c for c in cands if os.path.basename(c).lower().startswith("startup_%sx%s.s" % (key[:8], code))]
    if len(pick) == 1:
        return pick[0], stem_of(pick[0])
    return None, names


_CORE_TABLE = {
    "core_cm0.h": ("-mcpu=cortex-m0", "", ""),
    "core_cm0plus.h": ("-mcpu=cortex-m0plus", "", ""),
    "core_cm3.h": ("-mcpu=cortex-m3", "", ""),
    "core_cm4.h": ("-mcpu=cortex-m4", "-mfpu=fpv4-sp-d16", "-mfloat-abi=hard"),
    "core_cm7.h": ("-mcpu=cortex-m7", "-mfpu=fpv5-d16", "-mfloat-abi=hard"),
    "core_cm23.h": ("-mcpu=cortex-m23", "", ""),
    "core_cm33.h": ("-mcpu=cortex-m33", "-mfpu=fpv5-sp-d16", "-mfloat-abi=hard"),
    "core_cm35p.h": ("-mcpu=cortex-m35p", "-mfpu=fpv5-sp-d16", "-mfloat-abi=hard"),
    "core_cm55.h": ("-mcpu=cortex-m55", "-mfpu=fpv5-d16", "-mfloat-abi=hard"),
}


def _core_flags(dev_inc, stem):
    """从 CMSIS 设备头读出内核与有没有 FPU -> gcc 的 -mcpu/-mfpu(不靠猜)。"""
    core, fpu = "core_cm4.h", 1
    try:
        with open(os.path.join(dev_inc, stem + ".h"), errors="replace") as fh:
            txt = fh.read()
        m = re.search(r'#include\s+"(core_cm[0-9a-z]+)\.h"', txt)
        if m:
            core = m.group(1) + ".h"
        if not re.search(r"#define\s+__FPU_PRESENT\s+1", txt):
            fpu = 0
    except OSError:
        pass
    cpu, mfpu, abi = _CORE_TABLE.get(core, ("-mcpu=cortex-m4", "", ""))
    if not fpu:
        mfpu, abi = "", ""
    return cpu, mfpu, abi, core


def _mem_from_cube(pack, device):
    """直接抄 ST 自己示例工程的链接脚本内存参数 —— 比查表可靠。"""
    key = _dev_key(device).upper()
    hits = []
    proj = os.path.join(pack, "Projects")
    if os.path.isdir(proj):
        for root, dirs, files in os.walk(proj):
            for fn in files:
                u = fn.upper()
                if u.startswith(key) and u.endswith("_FLASH.LD"):
                    hits.append(os.path.join(root, fn))
            if len(hits) >= 5:
                break
    for p in hits:
        try:
            with open(p, errors="replace") as fh:
                txt = fh.read()
        except OSError:
            continue
        fl = re.search(r"FLASH\s*\([^)]*\)\s*:\s*ORIGIN\s*=\s*[^,]+,\s*LENGTH\s*=\s*([0-9]+[KMG]?)", txt, re.I)
        rm = re.search(r"RAM\s*\([^)]*\)\s*:\s*ORIGIN\s*=\s*[^,]+,\s*LENGTH\s*=\s*([0-9]+[KMG]?)", txt, re.I)
        if fl and rm:
            return fl.group(1).upper(), rm.group(1).upper(), p
    return None, None, ""


def _flash_from_code(device):
    """按 ST 的容量编码推 Flash 大小: STM32G431CB(T6) -> 第 11 位 B -> 128K。

    订货号结构: STM32 | G431 | C(封装) | B(容量) | T(温度) | 6(其它)
    所以容量码固定在第 11 位(索引 10); 只给 10 位型号时就是最后一位('8' 这种也认得)。
    """
    d = (device or "").upper().replace(" ", "")
    code = d[10] if len(d) >= 11 else (d[-1] if d else "")
    return _FLASH_CODE.get(code)


def _make_exe():
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in ("make.exe", "mingw32-make.exe", "make"):
            p = os.path.join(d, n)
            if d and os.path.isfile(p):
                return p
    for pat in ("C:/ST/STM32CubeCLT_*/Make/bin/make.exe",
                "C:/ST/STM32CubeCLT_*/Make/bin/mingw32-make.exe"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return "make"


def _gcc_prefix():
    """arm-none-eabi- 的前缀: PATH 里有就用命令名, 否则用 CubeCLT 自带的绝对路径。"""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.isfile(os.path.join(d, "arm-none-eabi-gcc.exe")):
            return "arm-none-eabi-"
    hits = sorted(glob.glob("C:/ST/STM32CubeCLT_*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc.exe"))
    if hits:
        return hits[-1][:-len("gcc.exe")]
    return "arm-none-eabi-"


_MAKEFILE_TPL = """# @@TARGET@@ -- 由 stm32-dev 技能生成的最小可烧录工程(裸机 + CMSIS, 不依赖 HAL)
# 芯片: @@DEVICE@@   调试器配置: .stm32-dev.json(改调试器: stm32-dev.py probe use)
# 内核参数: @@CPU@@ @@FPU@@ @@ABI@@  (按 CMSIS 设备头自动判定; 单精度核如 F722 把 fpv5-d16 改成 fpv5-sp-d16)

TARGET   := @@TARGET@@
DEVICE   := @@DEVICE@@
DEFS     := -D@@STEM_UPPER@@
CUBE     := @@CUBE@@
CUBE_INC := @@CUBE_INC@@
CORE_INC := @@CORE_INC@@
STARTUP  := @@STARTUP@@
SYS_SRC  := @@SYS_SRC@@
RUN      := @@RUN@@

BUILD    := build
PREFIX   := @@PREFIX@@
CC       := $(PREFIX)gcc
AS       := $(PREFIX)gcc -x assembler-with-cpp
OBJCOPY  := $(PREFIX)objcopy
SIZE     := $(PREFIX)size

COREFLAGS := @@CPU@@ @@FPU@@ @@ABI@@
CFLAGS    := $(COREFLAGS) -O2 -g3 -Wall -ffunction-sections -fdata-sections $(DEFS) -I$(CUBE_INC) -I$(CORE_INC) -I.
LDFLAGS   := $(COREFLAGS) -Tlinker.ld -Wl,-Map=$(BUILD)/$(TARGET).map,--cref -Wl,--gc-sections -specs=nano.specs -specs=nosys.specs

OBJS := $(BUILD)/main.o $(BUILD)/@@SYS_OBJ@@ $(BUILD)/@@STARTUP_OBJ@@

.PHONY: all clean flash verify reset size doctor probe
all: $(BUILD)/$(TARGET).elf $(BUILD)/$(TARGET).hex $(BUILD)/$(TARGET).bin

$(BUILD):
	-mkdir $(BUILD)

$(BUILD)/main.o: main.c | $(BUILD)
	$(CC) -c $(CFLAGS) $< -o $@

$(BUILD)/@@SYS_OBJ@@: $(SYS_SRC) | $(BUILD)
	$(CC) -c $(CFLAGS) $< -o $@

$(BUILD)/@@STARTUP_OBJ@@: $(STARTUP) | $(BUILD)
	$(AS) -c $(CFLAGS) $< -o $@

$(BUILD)/$(TARGET).elf: $(OBJS)
	$(CC) $(OBJS) $(LDFLAGS) -o $@
	$(SIZE) $@

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O binary -S $< $@

flash: all
	$(RUN) flash --elf $(BUILD)/$(TARGET).elf

verify: all
	$(RUN) verify --elf $(BUILD)/$(TARGET).elf

reset:
	$(RUN) reset

size: all
	$(SIZE) $(BUILD)/$(TARGET).elf

probe:
	$(RUN) probe show

doctor:
	$(RUN) doctor

clean:
	-rmdir /s /q $(BUILD)
	-rm -rf $(BUILD)
"""

_LD_TPL = """/* @@TARGET@@ -- 由 stm32-dev 技能生成
 * 内存参数: FLASH=@@FLASH@@ RAM=@@RAM@@ (@@MEM_SRC@@)
 * 必须对照芯片数据手册确认这两项 —— 填错会烧不进/跑飞。
 */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);
_Min_Heap_Size = 0x200;
_Min_Stack_Size = 0x400;

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = @@FLASH@@
  RAM   (xrw) : ORIGIN = 0x20000000, LENGTH = @@RAM@@
}

SECTIONS
{
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } >FLASH

  .text :
  {
    . = ALIGN(4);
    *(.text) *(.text*)
    *(.glue_7) *(.glue_7t) *(.eh_frame)
    KEEP (*(.init)) KEEP (*(.fini))
    . = ALIGN(4);
    _etext = .;
  } >FLASH

  .rodata :
  {
    . = ALIGN(4);
    *(.rodata) *(.rodata*)
    . = ALIGN(4);
  } >FLASH

  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } >FLASH
  .ARM : {
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  } >FLASH

  .preinit_array :
  {
    PROVIDE_HIDDEN (__preinit_array_start = .);
    KEEP (*(.preinit_array*))
    PROVIDE_HIDDEN (__preinit_array_end = .);
  } >FLASH

  .init_array :
  {
    PROVIDE_HIDDEN (__init_array_start = .);
    KEEP (*(SORT(.init_array.*)))
    KEEP (*(.init_array*))
    PROVIDE_HIDDEN (__init_array_end = .);
  } >FLASH

  .fini_array :
  {
    PROVIDE_HIDDEN (__fini_array_start = .);
    KEEP (*(SORT(.fini_array.*)))
    KEEP (*(.fini_array*))
    PROVIDE_HIDDEN (__fini_array_end = .);
  } >FLASH

  _sidata = LOADADDR(.data);

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data) *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >RAM AT> FLASH

  . = ALIGN(4);
  .bss :
  {
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss) *(.bss*) *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  } >RAM

  ._user_heap_stack :
  {
    . = ALIGN(8);
    PROVIDE ( end = . );
    PROVIDE ( _end = . );
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  } >RAM

  /* 黑匣子/故障转储放这里: 复位后还能读 (见技能的 blackbox 命令) */
  .noinit (NOLOAD) :
  {
    . = ALIGN(4);
    _snoinit = .;
    *(.noinit) *(.noinit*)
    . = ALIGN(4);
    _enoinit = .;
  } >RAM

  /DISCARD/ : { libc.a ( * ) libm.a ( * ) libgcc.a ( * ) }
  .ARM.attributes 0 : { *(.ARM.attributes) }
}
"""

_MAIN_TPL = """/* @@TARGET@@ -- 由 stm32-dev 技能生成的最小 main.c
 *
 * 这里只有内核级代码(任何 STM32 都能编过); 点灯/串口/CAN 等外设按你的板子自己加。
 * 调试建议: 全局计数器就是最便宜的"探针", 读它就知道程序在不在跑:
 *     stm32-dev.py read g_tick
 */
#include "@@DEV_HEADER@@"

volatile uint32_t g_tick  = 0;              /* 每轮循环 +1 */
volatile uint32_t g_magic = 0x5A5A0000u;

int main(void)
{
    g_magic = 0x5A5A0001u;
    while (1) {
        g_tick++;
        for (volatile uint32_t i = 0; i < 100000u; i++) { }   /* 粗延时 */
    }
}
"""

_README_TPL = """# @@TARGET@@

由 stm32-dev 技能生成的最小工程(裸机 + CMSIS 官方启动文件, 不依赖 HAL)。

- 编译: make
- 烧录并校验: make flash
- 复位运行: make reset
- 看芯片/探针现状: make doctor
- 换调试器: stm32-dev.py probe use <jlink|stlink|daplink>

芯片: @@DEVICE@@; 调试器写在 .stm32-dev.json(技能每次命令都会自动读它)。
内存参数: FLASH=@@FLASH@@ RAM=@@RAM@@ (@@MEM_SRC@@) —— 请对照数据手册确认。
编译用的是本地 Cube 包: @@CUBE@@
"""

_RUNCMD_TPL = """@echo off
rem 由 stm32-dev 技能生成: 让 Makefile 能直接调技能(路径含空格也用引号包好了)
"@@PYTHON@@" "@@SKILL@@" %*
"""

_RUNSH_TPL = """#!/bin/sh
# 由 stm32-dev 技能生成
exec "@@PYTHON@@" "@@SKILL@@" "$@"
"""


def cmd_new(args):
    """空项目脚手架: 生成能直接 make / 烧录 / 调试的最小工程。

    芯片必须给对(--device); 调试器自动探测, 写进工程配置后以后不用再管。
    """
    root = os.path.abspath(args.dir or ".")
    name = args.name or re.sub(r"[^0-9A-Za-z_]", "_", os.path.basename(root) or "firmware")
    device = resolve_device(args, None)
    print("=== 生成工程: %s (芯片 %s) ===" % (root, device))
    pack = find_cube_pack(device, args.verbose)
    if not pack:
        key5 = _dev_key(device)[5:]
        print("ERROR: 没找到本地 STM32Cube 固件包(CMSIS 头文件 + ST 官方启动文件都在里面)。")
        print("  装法: STM32CubeMX -> Help -> Manage embedded software packages -> 勾选 STM32Cube MCU Package for %s" % key5)
        print("        或手动下载 STM32Cube_FW_%s 解压到 %s" % (key5, os.path.join(os.path.expanduser("~"), "STM32Cube", "Repository")))
        print("  也可以设环境变量 STM32_CUBE_REPO 指向包所在的上级目录。")
        return 1
    print("  Cube 包: %s" % pack)
    layout = _cube_layout(pack)
    if not layout:
        print("ERROR: 这个 Cube 包里没有 CMSIS 设备文件: %s" % pack)
        return 1
    startup, stem = _pick_startup(layout["gcc"], device)
    if not startup:
        print("ERROR: 找不到 %s 对应的启动文件。候选: %s" % (device, ", ".join(stem)))
        print("  -> 用 --device 给准确型号(例 --device STM32G431CB)")
        return 1
    cpu, mfpu, abi, core = _core_flags(layout["dev_inc"], stem)
    print("  启动文件: %s   内核: %s (%s %s %s)" % (os.path.basename(startup), core, cpu, mfpu, abi))
    mem_src = "内置容量表"
    flash, ram = None, None
    st_flash, st_ram, st_ld = _mem_from_cube(pack, device)
    if st_flash and st_ram:
        guess = _flash_from_code(device)
        if guess and guess != st_flash:
            print("  [warn] ST 示例链接脚本写的是 %s, 型号编码指向 %s -> 按型号编码; 要用 ST 的值就显式给 --flash" % (st_flash, guess))
        else:
            flash, ram = st_flash, st_ram
            mem_src = "抄自 ST 示例 %s" % os.path.basename(st_ld)
    if not flash:
        flash = _flash_from_code(device)
    if args.flash:
        flash = args.flash
        mem_src = "--flash 指定"
    if args.ram:
        ram = args.ram
        mem_src = "--ram 指定"
    if not flash or not ram:
        print("ERROR: 内存参数没定下来(Flash=%s RAM=%s)。" % (flash or "?", ram or "?"))
        print("  -> 查数据手册后显式给: --flash 128K --ram 32K")
        return 1
    skill = os.path.abspath(__file__)
    python = sys.executable or "python"
    prefix = _gcc_prefix()
    files = {}
    files["Makefile"] = (_MAKEFILE_TPL
                         .replace("@@TARGET@@", name).replace("@@DEVICE@@", device)
                         .replace("@@STEM_UPPER@@", _stem_define(stem))
                         .replace("@@CUBE@@", pack.replace("\\", "/"))
                         .replace("@@CUBE_INC@@", layout["dev_inc"].replace("\\", "/"))
                         .replace("@@CORE_INC@@", layout["core_inc"].replace("\\", "/"))
                         .replace("@@STARTUP@@", startup.replace("\\", "/"))
                         .replace("@@SYS_SRC@@", layout["system"].replace("\\", "/"))
                         .replace("@@SYS_OBJ@@", os.path.basename(layout["system"])[:-2] + ".o")
                         .replace("@@STARTUP_OBJ@@", os.path.basename(startup)[:-2] + ".o")
                         .replace("@@PREFIX@@", prefix)
                         .replace("@@RUN@@", "tools/stm32-dev-run" + (".cmd" if os.name == "nt" else ".sh"))
                         .replace("@@CPU@@", cpu).replace("@@FPU@@", mfpu).replace("@@ABI@@", abi))
    files["linker.ld"] = (_LD_TPL.replace("@@TARGET@@", name).replace("@@FLASH@@", flash)
                          .replace("@@RAM@@", ram).replace("@@MEM_SRC@@", mem_src))
    files["main.c"] = _MAIN_TPL.replace("@@TARGET@@", name).replace("@@DEV_HEADER@@", stem + ".h")
    files["README.md"] = (_README_TPL.replace("@@TARGET@@", name).replace("@@DEVICE@@", device)
                          .replace("@@FLASH@@", flash).replace("@@RAM@@", ram)
                          .replace("@@MEM_SRC@@", mem_src).replace("@@CUBE@@", pack))
    files[".gitignore"] = "build/\n"
    tools_dir = os.path.join(root, "tools")
    runner = "stm32-dev-run" + (".cmd" if os.name == "nt" else ".sh")
    files[os.path.join("tools", runner)] = (
        (_RUNCMD_TPL if os.name == "nt" else _RUNSH_TPL)
        .replace("@@PYTHON@@", python).replace("@@SKILL@@", skill))
    if not args.force:
        clash = [fn for fn in files if os.path.isfile(os.path.join(root, fn))]
        if clash:
            print("ERROR: 这些文件已存在, 不覆盖: %s" % ", ".join(clash))
            print("  -> 确认要重写就加 --force")
            return 1
    os.makedirs(tools_dir, exist_ok=True)
    for fn, txt in files.items():
        p = os.path.join(root, fn)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", newline="\n") as fh:
            fh.write(txt)
        print("  + %s" % fn)
    kind = probe_for_work(verbose=False)
    if kind:
        cfg = load_config(root)
        cfg["probe"] = kind
        if _SERIAL and _SERIAL != DEFAULT_SERIAL:
            cfg["serial"] = _SERIAL
        save_config(cfg, root)
        print("  + .stm32-dev.json (调试器: %s)" % kind)
        print("    以后所有命令都会自动用这个调试器; 换: stm32-dev.py probe use <jlink|stlink|daplink>")
    else:
        print("  [提醒] 现在没探测到调试器 -> 插好后跑: stm32-dev.py probe use <jlink|stlink|daplink>")
    if args.no_build:
        print("跳过编译(--no-build)。下一步: cd %s && make" % root)
        return 0
    mk = _make_exe()
    print("=== 编译验证: %s -j4 ===" % mk)
    try:
        r = subprocess.run([mk, "-j4"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except Exception as e:
        print("ERROR: 跑 make 失败: %s" % e)
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    print(out[-1500:])
    if r.returncode != 0:
        print("!! 编译失败。生成的 Makefile 里内核/FPU/内存都是自动填的, 把上面这段发我或用 --flash/--ram 覆盖。")
        return 1
    elf = os.path.join(root, "build", name + ".elf")
    print("OK: 工程生成 + 编译通过 -> %s" % elf)
    print("下一步: cd %s && make flash" % root)
    jset(project=root, device=device, elf=elf, probe=kind or None, flash=flash, ram=ram)
    return 0


# ---------------------------------------------------------------------------
# 串口观测(串口探针帧): 不占用调试器, 换任何探针都能用
# ---------------------------------------------------------------------------
_SERIAL_HINTS = (
    (("stlink", "st-link", "stmicro"), 90, "ST-Link 自带的虚拟串口"),
    (("ch340", "cp210", "ftdi", "silicon labs", "prolific", "wch", "usb-serial", "usb serial"), 80, "USB 转串口"),
)
_NOT_TARGET = ("bth", "bluetooth", "\u84dd\u7259", "acpi")


def _serial_ports(verbose=False):
    """列本机串口, 按"最可能是目标板"排序。没装 pyserial 返回 None。"""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    out = []
    for p in list_ports.comports():
        desc = (p.description or "")
        low = (desc + " " + (p.hwid or "")).lower()
        score, why = 10, "未知设备"
        if any(b in low for b in _NOT_TARGET):
            score, why = -10, "蓝牙/主板自带, 一般不是目标板"
        else:
            for keys, sc, w in _SERIAL_HINTS:
                if any(k in low for k in keys):
                    score, why = sc, w
                    break
            else:
                if "usb" in low:
                    score, why = 70, "USB 串口设备"
        out.append({"port": p.device, "desc": desc, "why": why, "score": score})
    out.sort(key=lambda d: (-d["score"], d["port"]))
    if verbose:
        for d in out:
            print("    [%s] %s  (%s)" % (d["port"], d["desc"], d["why"]))
    return out


def cmd_serial(args):
    """读串口里固件自己吐的观测帧。

    这条通道不碰调试器: 换 J-Link / ST-Link / DAPLink 都一样用,
    而且可以和"调试/烧录"同时进行(调试器独占的是探针, 不是串口)。
    """
    import time
    ports = _serial_ports(args.verbose)
    if ports is None:
        print("ERROR: 没装 pyserial(读串口的库)。")
        print("  装法: %s -m pip install pyserial" % (sys.executable or "python"))
        return 1
    if args.list:
        if not ports:
            print("本机没看到任何串口。")
            return 1
        print("本机串口(按最可能是目标板排序):")
        for d in ports:
            print("  %-8s %s  <- %s" % (d["port"], d["desc"], d["why"]))
        print("读串口: %s %s serial --port <串口> --seconds 5" % (sys.executable or "python", os.path.abspath(__file__)))
        return 0
    port = args.port or ""
    if not port:
        cands = [d for d in ports if d["score"] >= 70]
        if len(cands) == 1:
            port = cands[0]["port"]
            print("自动选中串口 %s (%s)" % (port, cands[0]["desc"]))
        elif not cands:
            print("ERROR: 没找到像目标板的串口。本机看到:")
            for d in ports:
                print("  %s: %s" % (d["port"], d["desc"]))
            print("  -> 显式指定: --port COM21")
            return 1
        else:
            print("ERROR: 有多个串口候选, 请用 --port 指定:")
            for d in cands:
                print("  %s: %s (%s)" % (d["port"], d["desc"], d["why"]))
            return 1
    try:
        import serial
    except ImportError:
        print("ERROR: 没装 pyserial。装法: %s -m pip install pyserial" % (sys.executable or "python"))
        return 1
    try:
        ser = serial.Serial(port, args.baud, timeout=0.2)
    except Exception as e:
        print("ERROR: 打不开 %s: %s" % (port, e))
        print("  -> 常见原因: ① 被别的程序占着(串口助手/上位机/另一个脚本)")
        print("               ② 串口号不对(拔插一次会变) -> --list 看现在的")
        print("               ③ 板子没插")
        return 1
    flags = 0 if args.case_sensitive else re.I
    print("=== 读串口 %s @ %d baud, 最多 %s 秒 (Ctrl+C 可提前结束) ===" % (port, args.baud, args.seconds))
    lines, buf, t0 = [], "", time.time()
    try:
        while time.time() - t0 < args.seconds:
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="replace")
            while True:
                m = re.search(r"\r\n|\n|\r", buf)
                if not m:
                    break
                line, buf = buf[:m.start()], buf[m.end():]
                lines.append(line)
                if not args.grep or re.search(args.grep, line, flags):
                    print(line)
    except KeyboardInterrupt:
        print("(手动结束)")
    finally:
        try:
            ser.close()
        except Exception:
            pass
    if buf.strip():
        lines.append(buf)
        if not args.grep or re.search(args.grep, buf, flags):
            print(buf)
    data = "\n".join(lines)
    took = round(time.time() - t0, 1)
    print("=== 结束: %d 行 / %s 秒 ===" % (len(lines), took))
    if args.save:
        try:
            with open(args.save, "w", encoding="utf-8") as fh:
                fh.write(data)
            print("已存原文: %s" % args.save)
        except OSError as e:
            print("!! 存文件失败: %s" % e)
    jset(port=port, baud=args.baud, lines=len(lines), seconds=took, saved=args.save or None)
    if not lines:
        print("!! 一行都没收到。按顺序查:")
        print("   1) 波特率对不对(常见 115200; 问固件里怎么配的)")
        print("   2) 板子到底有没有在发(固件里加一句开机就打印的问候语最省事)")
        print("   3) 接线: TXD->RXD 交叉 + 必须共地")
        print("   4) RS485 半双工要等方向控制切到发送, 或先只看能不能收到任何字节")
        return 1
    rc = 0
    if args.grep:
        hits = [l for l in lines if re.search(args.grep, l, flags)]
        print("--grep %s: %d/%d 行命中" % (args.grep, len(hits), len(lines)))
        if not hits:
            rc = 1
    if args.check_seq:
        res = analyze_increasing_seq(data)
        if res is None:
            print("--check-seq: 没找到递增计数列。建议固件每帧带 seq=<自增数>, 这样丢帧可判定。")
        else:
            k, n, mx, gaps, lead, base = res
            print("--check-seq: 字段 %s, %d 个样本, 正常步长 %d, 稳态最大跳变 %d, 超步长 %d 次 -> %s"
                  % (k, n, base, mx, gaps, "无丢帧" if gaps == 0 else "有丢帧"))
            jset(seq_check={"field": k, "samples": n, "base_delta": base,
                            "max_delta": mx, "gaps": gaps, "ok": gaps == 0})
            if lead > 1:
                print("            开头追赶区最大跳变 %d (缓冲区旧内容->实时数据, 不计入丢帧)" % lead)
            if gaps != 0:
                print("!! 有丢帧: 串口这一侧丢帧多半是上位机读太慢/波特率不匹配, 不能直接判固件丢帧。")
                rc = 1
            else:
                print("--check-seq 判定通过: 序号严格递增。")
    return rc


def _pip_install(pkg, verbose=False):
    """装一个 Python 包(用户级, 不需要管理员)。返回 True/False。"""
    argv = [sys.executable or "python", "-m", "pip", "install", "--disable-pip-version-check", pkg]
    print("  [pip] %s" % " ".join(argv))
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600,
                           text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        print("        失败: %s" % e)
        print("        手动装: %s" % " ".join(argv))
        return False
    if r.returncode == 0:
        print("        装好了: %s" % pkg)
        return True
    print("        没装上(可能是网络或权限)。手动装: %s" % " ".join(argv))
    if verbose:
        print(((r.stdout or ""))[-600:])
    return False


def _sys_install_cmds(what):
    """该缺件在本机的系统级安装命令(已实测存在)。给不出可靠命令就返回 []。"""
    win = (os.name == "nt")
    if what == "openocd":
        return (["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
                 "--id", "xpack-dev-tools.openocd-xpack"] if win
                else ["sudo", "apt", "install", "-y", "openocd"])
    if what == "gcc":
        return (["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
                 "--id", "Arm.GnuArmEmbeddedToolchain"] if win
                else ["sudo", "apt", "install", "-y",
                      "gcc-arm-none-eabi", "gdb-multiarch", "make"])
    if what == "make":
        return (["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
                 "--id", "ezwinports.make"] if win
                else ["sudo", "apt", "install", "-y", "make"])
    return []


def _auto_install(argv, label, verbose=False):
    """跑一条系统级安装命令(只有用户显式 --install 时才调用)。"""
    print("  [装] %s <- %s" % (label, " ".join(argv)))
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
                           text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        print("        失败: %s" % e)
        return False
    if r.returncode == 0:
        print("        装好了: %s" % label)
        return True
    print("        没成功(可能要管理员权限或网络不通)。手动跑: %s" % " ".join(argv))
    if verbose:
        print(((r.stdout or ""))[-600:])
    return False


def _proactive_lines(pid, cfg=None):
    """按当前调试器列出「还能做什么」: 没用上的能力 + 现成命令。探针无关的三条永远给。"""
    root = os.getcwd()
    elfs = glob.glob(os.path.join(root, "build", "*.elf")) + glob.glob(os.path.join(root, "*.elf"))
    elf = elfs[0] if elfs else "build/你的工程.elf"
    common = [
        "串口自报帧(不占调试器, 换任何探针都一样): serial --port COM<n> --seconds 5 --check-seq",
        "跨复位黑匣子(.noinit 里留现场, 事后读一次): blackbox --elf %s" % elf,
        "DWT 打点(计时/计数不占带宽): 读 DWT_CYCCNT -> read 0xE0001004(先看 0xE0001000 的 bit0); 固件侧见 OBSERVE.md, 探针无关",
    ]
    per = {
        "jlink": [
            "实时日志(RTT): rtt --elf %s --check-seq   (原生 MB/s 双向, 这是 J-Link 唯一不可替代的强项)" % elf,
            "往板子回灌数据(下行通道, 别的探针基本做不了): rtt-send --channel 0 文本",
            "选项字节/读保护: J-Link 侧没有现成 CLI, 得手写 Commander 脚本; 换 ST-Link 才有一行命令",
        ],
        "stlink": [
            "选项字节/读保护/救砖(本技能原来没有的能力): STM32_Programmer_CLI -c port=SWD mode=UR -ob RDP=0xAA  (擦全片解保护, 会清空固件)",
            "板上 HardFault 现场直读(不用自己写转储代码): STM32_Programmer_CLI -hf",
            "V3/V2 自带虚拟串口: 一根 USB 同时给 SWD + 串口, 插上后 serial --list 会多一个 COM",
            "SWD 时钟默认已用 8MHz(V3 上限); 线长或干扰大时在 -c 里加 freq=4000 降速",
            "SWO/ITM 也能走 OpenOCD(swo create + tpiu create), 前提是板子把 SWO 引脚引出来了",
            "实时日志走 OpenOCD rtt server: 能用, 但比 J-Link 慢 1~2 个数量级",
        ],
        "daplink": [
            "免驱 CMSIS-DAP: 插上就能用, 不用装厂商驱动",
            "装 pyOCD 会更省心(配置更少, 烧录/复位一条命令): python -m pip install pyocd",
            "SWO/ITM: 只有 v2 版 DAPLink 带 SWO 端点, v1 没有",
            "选项字节/救砖: 要走 OpenOCD 的 option 命令, 比 ST 官方 CLI 麻烦",
            "实时日志走 OpenOCD rtt server(和 ST-Link 同一条路)",
        ],
    }
    if pid in per:
        return per[pid] + common
    return ["还没定调试器: 先 probe detect(看插着哪个) 或 probe list(看支持哪些)"] + common


def cmd_setup(args):
    """一键体检: 调试器 + 工具链 + 芯片资料 + 工程, 每项缺什么就说怎么装。"""
    verbose = bool(getattr(args, "verbose", False))
    fix = bool(getattr(args, "fix", False))
    install = bool(getattr(args, "install", False))
    if install:
        fix = True        # --install 隐含 --fix: 先自动补能补的, 再装系统件
    cfg = load_config()
    problems = []
    need = []             # 缺的东西, 交给 --fix / --install 自动处理

    print("=== 1/4 调试器(探针) ===")
    pid, serial, why = "", "", ""
    try:
        pid, serial, why = resolve_probe(args, cfg, verbose)
    except SkillError as e:
        print("  还没定: %s" % e)
    if pid:
        _describe_effective(pid, serial, why)
        for label, path, hint in _probe_tool_status(pid):
            if not _tool_line(label, path, hint):
                problems.append("%s 的 %s 没装" % (PROBE_DEFS[pid]["name"], label))
        print("  最强用法: %s" % PROBE_DEFS[pid]["best"])
        if fix and not cfg.get("probe"):
            p = merge_config({"probe": pid, **({"serial": serial} if serial else {})})
            print("  已写进工程配置: %s(以后不用再指定)" % p)

    print("")
    print("=== 2/4 编译工具链 ===")
    gd = find_gdb()
    gcc_dirs = [os.path.dirname(gd)] if gd else []
    gcc = _which_or_dirs(["arm-none-eabi-gcc", "arm-none-eabi-gcc.exe"], gcc_dirs, "ARM_GCC")
    if not gcc:
        gcc = _glob_first(["C:/ST/STM32CubeCLT_*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc.exe",
                           "/opt/st/stm32cubeclt_*/GNU-tools-for-STM32/bin/arm-none-eabi-gcc"])
    _tool_line("arm-none-eabi-gcc(编译器)", gcc,
               "装 STM32CubeCLT(自带编译器+gdb+make/cmake/ninja); Linux: apt install gcc-arm-none-eabi")
    if not gcc:
        problems.append("编译器 arm-none-eabi-gcc 没装")
        need.append("gcc")
    _tool_line("arm-none-eabi-gdb(调试器前端)", gd,
               "装 STM32CubeCLT, 或把 gdb 路径写进环境变量 STM32_DEBUG_GDB")
    if not gd:
        problems.append("gdb 没装")
    _tool_line("arm-none-eabi-objcopy(生成 hex/bin)", find_objcopy(),
               "随编译器一起装(同 arm-none-eabi-gcc)")
    make = _which_or_dirs(["make", "make.exe", "mingw32-make", "mingw32-make.exe"],
                          _find_dirs(["C:/ST/STM32CubeCLT_*/GNU-tools-for-STM32/bin",
                                      "C:/ST/STM32CubeCLT_*/Make/bin"]), "MAKE")
    cmake = _which_or_dirs(["cmake", "cmake.exe"],
                           _find_dirs(["C:/ST/STM32CubeCLT_*/CMake/bin"]), "CMAKE")
    ninja = _which_or_dirs(["ninja", "ninja.exe"],
                           _find_dirs(["C:/ST/STM32CubeCLT_*/Ninja/bin"]), "NINJA")
    _tool_line("make(或 cmake+ninja 二选一)", make or cmake,
               "随 STM32CubeCLT 装; 或 winget install ezwinports.make / Kitware.CMake")
    if not (make or cmake):
        problems.append("make/cmake 都没有(空工程脚手架生成的 Makefile 跑不起来)")
        need.append("make")
    if cmake and not ninja:
        print("  [提示] cmake 有但 ninja 没装: 用 make 生成器即可, 或 winget install Ninja-build.Ninja")
    try:
        import serial as _serial_probe      # noqa: F401  只探测有没有装, 不真用
        _pyserial_ok = True
    except Exception:
        _pyserial_ok = False
    _tool_line("pyserial(读串口自报帧, 不占调试器)", "yes" if _pyserial_ok else "",
               "装法: %s -m pip install pyserial   (setup --fix 会自动装)" % (sys.executable or "python"))
    if not _pyserial_ok:
        problems.append("pyserial 没装(serial 串口观测用不了)")
        need.append("pyserial")
    if pid == "daplink" and not find_pyocd():
        print("  [提示] DAPLink 装上 pyOCD 会更省心(可选): %s -m pip install pyocd  (--fix 也会装)" % (sys.executable or "python"))
        need.append("pyocd")

    print("")
    print("=== 3/4 芯片资料(SVD 寄存器解码 / OpenOCD 目标脚本) ===")
    svd_dirs = _find_dirs(SVD_SEARCH_DIRS)
    n_svd = 0
    for d in svd_dirs:
        try:
            n_svd += len([x for x in os.listdir(d) if x.lower().endswith(".svd")])
        except Exception:
            pass
    if svd_dirs:
        print("  [有] 找到 %d 个 SVD(寄存器位域解码可用), 例: %s" % (n_svd, svd_dirs[0]))
    else:
        print("  [缺] 没有 SVD: 寄存器位域解码用不了")
        print("       装法: 装 STM32CubeCLT/CubeMX(winget 里没有 CubeCLT 包, 只能从 ST 官网下安装器),")
        print("             或把 .svd 放到工程里, 或设 STM32_SVD_DIR")
    ocd = find_openocd()
    if ocd:
        base = os.path.dirname(os.path.abspath(ocd))
        cands = [os.path.join(base, "openocd", "scripts"), os.path.join(base, os.pardir, "openocd", "scripts"),
                 os.path.join(base, os.pardir, "share", "openocd", "scripts")]
        hit = [c for c in cands if os.path.isdir(os.path.join(c, "target"))]
        if hit:
            print("  [有] OpenOCD 目标脚本: %s" % os.path.join(hit[0], "target"))
        else:
            # 找不到 scripts 目录时, 真跑一次"只加载配置、不碰探针"来判: 配置能加载就说明自带搜索路径够用
            argv = _argv_for(ocd, "-f", "interface/stlink.cfg", "-f", "target/stm32g4x.cfg",
                             "-c", "echo SCRIPT_OK; shutdown")
            try:
                p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=25)
                txt = (p.stdout or b"").decode("utf-8", "replace")
            except Exception:
                txt = ""
            if "SCRIPT_OK" in txt:
                print("  [有] OpenOCD: %s(接口/目标配置文件能加载)" % ocd)
            else:
                print("  [有] OpenOCD: %s(接口/目标配置没加载成功, 可能需要 -s 指定 scripts 目录)" % ocd)
    else:
        print("  [缺] OpenOCD: 只在非 J-Link 探针(gdb server / RTT / SWO)时才需要")
        print("       装法: winget install --id xpack-dev-tools.openocd-xpack, 或设 OPENOCD")
        need.append("openocd")

    print("")
    print("=== 4/4 当前工程 ===")
    root = os.getcwd()
    for name, what in (("Makefile", "Make 工程"), ("CMakeLists.txt", "CMake 工程"),
                       ("platformio.ini", "PlatformIO 工程"), (CONFIG_NAME, "本技能的探针配置")):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            print("  [有] %s (%s)" % (name, what))
    elfs = glob.glob(os.path.join(root, "build", "*.elf")) + glob.glob(os.path.join(root, "*.elf"))
    if elfs:
        print("  [有] 固件: %s" % elfs[0])
    else:
        print("  [无] 没找到 .elf(还没编译过, 或输出目录不叫 build/)")

    print("")
    print("=== 主动建议: 你这个调试器还能做什么(不换探针也能上) ===")
    for _ln in _proactive_lines(pid, cfg):
        print("  - %s" % _ln)

    if need and fix:
        print("")
        print("=== --fix 自动补(%d 项) ===" % len(need))
        for w in need:
            if w in ("pyserial", "pyocd"):
                _pip_install(w, verbose)
            elif install:
                cmds = _sys_install_cmds(w)
                if cmds:
                    _auto_install(cmds, w, verbose)
                else:
                    print("  %s: 没有可靠的自动装法, 按上面的提示手动装" % w)
            else:
                cmds = _sys_install_cmds(w)
                if cmds:
                    print("  %s 要动系统, 没自动装。想让我装就加 --install -> %s" % (w, " ".join(cmds)))
                else:
                    print("  %s: 需要手动装(见上面的提示)" % w)

    print("")
    if problems:
        print("结论: 还差 %d 项 -> %s" % (len(problems), "; ".join(problems)))
        print("装完再跑一次 setup。只想现在就能烧录/调试的话, 上面标 [缺] 的最小集合先补上。")
        return 1
    print("结论: 齐了。下一步: make(或 cmake --build) -> flash -> 调试观察。")
    return None


def cmd_probe(args):
    action = getattr(args, "action", "list") or "list"
    raw_target = getattr(args, "target", "") or ""
    target = _norm_probe(raw_target)
    verbose = bool(getattr(args, "verbose", False))
    if raw_target and not target:
        raise SkillError("认不出这个探针: %s(可用: jlink / stlink / daplink)" % raw_target)
    cfg = load_config()

    if action == "list":
        print("支持的调试器(探针) —— 命令层按能力选路, 不写死型号:")
        for pid in ("jlink", "stlink", "daplink"):
            d = PROBE_DEFS[pid]
            print("")
            print("  [%s] %s" % (pid, d["name"]))
            print("    典型: %s" % ", ".join(d["matches"]))
            print("    最强: %s" % d["best"])
            miss = [lbl for lbl, p, _h in _probe_tool_status(pid) if not p]
            if miss:
                print("    本机缺工具: %s(它还能用, 但下面这些功能暂时没有)" % ", ".join(miss))
            else:
                print("    本机工具: 齐")
        print("")
        try:
            pid, serial, why = resolve_probe(args, cfg, verbose)
            _describe_effective(pid, serial, why)
            print("  看单个探针细节: stm32-dev.py probe info %s" % pid)
        except SkillError as e:
            print("当前: 还没定(%s)" % e)
            print("  第一步: stm32-dev.py probe detect  然后 probe use <名字>")
        return None

    if action == "detect":
        # 探测过程的啰嗦输出先收起来: 汇总行先出, 过程放最后,
        # 否则"某个探针没认出来"的中间提示会插在结果前面, 看着乱。
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            found = detect_probes(verbose=True)
            _pnp_extra = _pnp_probes(verbose)
        total = 0
        for pid in ("jlink", "stlink", "daplink"):
            hits = found.get(pid, [])
            total += len(hits)
            if hits:
                for h in hits:
                    extra = (" S/N=%s" % h["serial"]) if h.get("serial") else ""
                    print("  [找到] %s: %s%s" % (pid, h.get("model") or PROBE_DEFS[pid]["name"], extra))
                    if pid == "stlink":
                        if h.get("variant"):
                            _p = _stlink_profile(h["variant"])
                            print("        这一代: %s" % _p["label"])
                            print("        按这代最优: SWD %d kHz(OpenOCD) / %d kHz(官方 CLI)" %
                                  (_p["freq_ocd"], _p["freq_cli"]))
                        if h.get("cli_bad"):
                            print("        注意: 官方 CLI 读不到这只的串号(多半被别的程序占着) -> 烧录/校验会自动改走 OpenOCD")
            else:
                print("  [没找到] %s" % PROBE_DEFS[pid]["name"])
        if not found.get("stlink"):
            _line = _openocd_probe_line(verbose=verbose)
            if _line:
                _v, _vw = _stlink_variant("", "", _line)
                total += 1
                print("  [OpenOCD 看到] %s" % _line)
                if _v:
                    print("        这一代: %s" % _stlink_profile(_v)["label"])
                    print("        按这代最优: SWD %d kHz(OpenOCD) / %d kHz(官方 CLI)" %
                          (_stlink_profile(_v)["freq_ocd"], _stlink_profile(_v)["freq_cli"]))
        for h in _pnp_extra:
            print("  [USB 上看到] %s" % h["model"])
        _detail = _buf.getvalue().strip()
        if _detail:
            print("")
            print("  --- 探测过程 ---")
            for _ln in _detail.splitlines():
                print("  " + _ln)
        if total == 0:
            print("")
            print("一个都没认出来。按顺序查:")
            print("  1) USB 插好了吗, 换个口 / 换根线(只能充电的线不行)")
            print("  2) 驱动装了吗: J-Link->SEGGER 驱动; ST-Link->STM32CubeCLT/CubeProgrammer; DAPLink->免驱")
            print("  3) 别的程序是不是占着它(比如 IDE、另一个 gdb server、ST 的 stlinkserver)")
            print("  4) 克隆 ST-Link 会被官方 CLI 拒(报 not a genuine ST device): 技能会自动改走 OpenOCD, 不用你管")
            return 1
        return None

    if action == "use":
        if not target:
            raise SkillError("用法: probe use <jlink|stlink|daplink> [--serial <S/N>]")
        found = detect_probes(only=target, verbose=verbose)
        hits = found.get(target, [])
        item = {"probe": target}
        serial = (getattr(args, "serial", "") or "").strip()
        if serial == DEFAULT_SERIAL:
            serial = ""
        if not serial and hits:
            serial = hits[0].get("serial", "") or ""
        if serial:
            item["serial"] = serial
        elif getattr(args, "persist", False):
            raise SkillError("要写序列号进配置但没拿到: 先插好探针, 或 --serial <S/N>")
        path = save_config(item)
        _describe_effective(target, serial, "写进 " + os.path.basename(path))
        print("  已写入: %s" % path)
        if hits and hits[0].get("model"):
            print("  认到的型号: %s" % hits[0]["model"])
        print("  最好的用法: %s" % PROBE_DEFS[target]["best"])
        miss = [lbl for lbl, p, _h in _probe_tool_status(target) if not p]
        if miss:
            print("  还缺工具(功能受限): %s -> 跑 setup 看怎么装" % ", ".join(miss))
        return None

    if action == "show":
        try:
            pid, serial, why = resolve_probe(args, cfg, verbose)
        except SkillError as e:
            print("当前没定调试器: %s" % e)
            return 1
        _describe_effective(pid, serial, why)
        return None

    # info
    if not target:
        raise SkillError("用法: probe info <jlink|stlink|daplink>")
    d = PROBE_DEFS[target]
    var = ""
    if target == "stlink":
        var, vwhy = _stlink_variant_probe(deep=verbose)
    caps = _caps_of(target, var)
    jset(probe=target, caps=caps, stlink_variant=var or None)
    print("探针: %s(%s)" % (d["name"], target))
    print("典型型号: %s" % ", ".join(d["matches"]))
    print("最好的能力: %s" % d["best"])
    if var:
        _p = _stlink_profile(var)
        print("")
        print("本机插着的这一代: %s   (%s)" % (_p["label"], vwhy))
        print("  按这代自动选: SWD %d kHz(OpenOCD) / %d kHz(官方 CLI)" % (_p["freq_ocd"], _p["freq_cli"]))
    elif target == "stlink":
        print("")
        print("本机没插着 ST-Link(或官方 CLI 认不出它): 下面按通用能力列, 具体参数等插上后按世代自动选。")
    print("")
    print("能力对照(不同探针各有强弱, 技能按这个选路):")
    for key, label in CAP_LABELS:
        print("  %-18s %s" % (label, caps.get(key, "")))
    print("")
    print("本机工具:")
    for label, path, hint in _probe_tool_status(target):
        if path:
            print("  [有] %s -> %s" % (label, path))
        else:
            print("  [缺] %s -> %s" % (label, hint))
    print("")
    print("注意事项:")
    for n in d["notes"]:
        print("  - %s" % n)
    if var:
        print("这一代(%s)额外注意:" % var)
        for n in _stlink_profile(var).get("notes", []):
            print("  - %s" % n)
    print("")
    print("没有它时怎么替代: %s" % d["degrade"])
    return None


def add_common(parser):
    parser.add_argument("--json", action="store_true", help="输出 JSON(供脚本/CI 调用)")
    parser.add_argument("--elf", default=None, help="ELF 路径(自动推断芯片)")
    parser.add_argument("--device", default=None, help="芯片型号 e.g. STM32H743VI")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="探针序列号(多探针时必须给)")
    parser.add_argument("--probe", default=None,
                        help="调试器: jlink / stlink / daplink (不给则查 .stm32-dev.json 或自动探测)")


def build_parser():
    p = argparse.ArgumentParser(prog="stm32-dev")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("doctor"); add_common(s)
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("probe", help="调试器(探针)选型: list / detect / use / info / show")
    s.add_argument("action", nargs="?", default="list",
                   choices=["list", "detect", "use", "info", "show"],
                   help="list=支持哪些(默认) detect=现在插着哪个 use=选定并写进工程配置 info=单个探针细节 show=当前生效")
    s.add_argument("target", nargs="?", default="", help="use/info 时的探针名(jlink/stlink/daplink)")
    s.add_argument("--persist", action="store_true", help="use 时把序列号也写进配置")
    s.add_argument("--verbose", action="store_true", help="打印每条判定依据")
    add_common(s)
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("setup", help="一键体检: 调试器 + 工具链 + 芯片资料 + 工程, 缺什么怎么装")
    s.add_argument("--fix", action="store_true", help="能自动补的直接补(写探针配置 / pip 装 pyserial·pyocd)")
    s.add_argument("--install", action="store_true", help="缺编译器/openocd 时自动跑 winget(或 apt) 装上(隐含 --fix)")
    s.add_argument("--verbose", action="store_true", help="打印每条判定依据")
    add_common(s)
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("new", help="空项目脚手架: 生成能直接 make/烧录/调试的最小工程")
    add_common(s)
    s.add_argument("--dir", default=".", help="工程目录(默认当前目录)")
    s.add_argument("--name", default=None, help="工程名(默认=目录名)")
    s.add_argument("--flash", default=None, help="Flash 大小(如 128K); 默认抄 ST 官方链接脚本")
    s.add_argument("--ram", default=None, help="RAM 大小(如 32K); 拿不到时必须给")
    s.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    s.add_argument("--no-build", action="store_true", dest="no_build", help="只生成, 不编译验证")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("serial", help="读串口观测帧(不占用调试器, 换任何探针都一样用)")
    add_common(s)
    s.add_argument("--port", help="串口号, 例 COM21; 不给我自己挑(本机只有一个 USB 串口时)")
    s.add_argument("--baud", type=int, default=115200, help="波特率(默认 115200)")
    s.add_argument("--seconds", type=float, default=3.0, help="读多久(默认 3 秒)")
    s.add_argument("--grep", help="只看匹配的行(正则); 一行都没命中则退出码 1")
    s.add_argument("--case-sensitive", action="store_true", dest="case_sensitive", help="--grep 区分大小写")
    s.add_argument("--check-seq", action="store_true", dest="check_seq", help="查固件自增序号有没有跳变(判丢帧)")
    s.add_argument("--save", help="把读到的原文存成文件")
    s.add_argument("--list", action="store_true", help="只列本机串口, 不读")
    s.add_argument("--verbose", action="store_true", help="打印每个串口的判定依据")
    s.set_defaults(func=cmd_serial)
    s = sub.add_parser("preflight", help="*开工第一步: 拿技能里的结论把现有工程扫一遍")
    add_common(s)
    s.add_argument("--root", default=None, help="工程根目录(默认当前目录)")
    s.set_defaults(func=cmd_preflight)

    s = sub.add_parser("read"); add_common(s); s.add_argument("targets", nargs="+")
    s.add_argument("--size", type=int, default=4, choices=[1, 2, 4, 8], help="读写裸地址时的宽度(字节, 1/2/4/8); 读符号时忽略。例: DWT CYCCNT 是 4")
    s.add_argument("--keep-halted", action="store_true", help="读完后不恢复运行(默认会恢复)")
    s.epilog = ("targets 可以是符号名(g_bb / uwTick), 也可以是裸地址(如 0xE000ED00 CPUID、"
                "0xE0001004 DWT 的 CYCCNT); 没有 SVD 的外设与 Cortex-M 内核寄存器就用裸地址。")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("write"); add_common(s); s.add_argument("targets", nargs="+")
    s.add_argument("--size", type=int, default=4, choices=[1, 2, 4, 8], help="读写裸地址时的宽度(字节, 1/2/4/8); 读符号时忽略。例: DWT CYCCNT 是 4")
    s.add_argument("--keep-halted", action="store_true", help="写完后不恢复运行(默认会恢复)")
    s.set_defaults(func=cmd_write)

    s = sub.add_parser("break"); add_common(s); s.add_argument("point", nargs="?", default=None)
    s.add_argument("--keep-halted", action="store_true")
    s.set_defaults(func=cmd_break)

    s = sub.add_parser("continue"); add_common(s); s.set_defaults(func=cmd_contr)
    s = sub.add_parser("step"); add_common(s); s.add_argument("n", type=int, default=1)
    s.set_defaults(func=cmd_step)
    s = sub.add_parser("info"); add_common(s); s.add_argument("--keep-halted", action="store_true")
    s.set_defaults(func=cmd_info)
    s = sub.add_parser("attach"); add_common(s); s.set_defaults(func=cmd_attach)
    s = sub.add_parser("start"); add_common(s); s.set_defaults(func=cmd_start)
    s = sub.add_parser("stop"); add_common(s); s.set_defaults(func=cmd_stop)
    s = sub.add_parser("verify"); add_common(s); add_stlink_args(s)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("reset"); add_common(s); add_stlink_args(s)
    s.add_argument("--dry-run", action="store_true", dest="dry_run", help="只打印将执行的复位命令")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("selftest"); add_common(s)
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("rtt-send"); add_common(s)
    s.add_argument("data", nargs="?", default=None, help="要发送的字符串(支持 \\n \\r \\xNN 转义)")
    s.add_argument("--hex", default=None, help="按十六进制字节发送, 如 70 或 70,0A")
    s.add_argument("--address", default=None, help="RTT 控制块地址(默认从 ELF 解析)")
    s.add_argument("--expect", default=None, help="期望回包的正则; 给出则按命中率判定")
    s.add_argument("--timeout", type=float, default=1.0, help="每次等待回包秒数")
    s.add_argument("--repeat", type=int, default=1, help="重复次数(压测往返延迟)")
    s.add_argument("--interval", type=float, default=0.0, help="重复间隔 ms")
    s.add_argument("--speed", type=int, default=4000, help="J-Link 连接速度(kHz)")
    s.add_argument("--channel", type=int, default=0, help="RTT 通道号(默认 0)")
    s.add_argument("--rtt-port", type=int, default=9090,
                   help="ST-Link/DAPLink 走 OpenOCD 时 RTT server 监听的本地 TCP 端口")
    s.set_defaults(func=cmd_rtt_send)

    s = sub.add_parser("init-rtt"); add_common(s)
    s.add_argument("--dir", default=None, help="工程目录(默认当前目录)")
    s.add_argument("--subdir", default=os.path.join("Middlewares", "SEGGER_RTT"), help="存放子目录")
    s.add_argument("--offline", action="store_true",
                   help="J-Link 安装目录里没有源码时, 也不要联网去官方仓库取")
    s.set_defaults(func=cmd_init_rtt)

    s = sub.add_parser("blackbox"); add_common(s)
    s.add_argument("--symbol", default="g_bb", help="黑匣子变量名(默认 g_bb)")
    s.add_argument("--keep-halted", action="store_true")
    s.set_defaults(func=cmd_blackbox)

    s = sub.add_parser("init-fault"); add_common(s)
    s.add_argument("--dir", default=None, help="工程目录(默认当前目录)")
    s.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    s.set_defaults(func=cmd_init_fault)

    s = sub.add_parser("cleanup"); add_common(s); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--all", action="store_true",
                   help="强杀所有 J-Link 相关进程(含 IDE/别块板子的会话); 必须同时 --force")
    s.add_argument("--force", action="store_true", help="配合 --all 使用, 表示确认风险")
    s.set_defaults(func=cmd_cleanup)
    s = sub.add_parser("svd"); add_common(s)
    s.add_argument("expr", nargs="?", default=None, help="外设.寄存器 或 0x裸地址")
    s.add_argument("--all", action="store_true", help="显示全部位域(默认只显示非零/有枚举的)")
    s.add_argument("--keep-halted", action="store_true")
    s.set_defaults(func=cmd_svd)

    s = sub.add_parser("flash"); add_common(s); add_stlink_args(s); s.add_argument("--hex", default=None)
    s.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="只打印将要执行的烧录命令, 不碰板子(第一次烧接电机的板子时先看这个)")
    s.add_argument("--ur", action="store_true", help="ST-Link 专用: 用 mode=UR(under reset) 连接, 板子跑飞/被保护时用")
    s.add_argument("--no-verify", action="store_true", help="刷完不校验板子 Flash 与 ELF 是否一致")
    s.set_defaults(func=cmd_flash)

    s = sub.add_parser("rtt"); add_common(s)
    s.add_argument("--seconds", type=float, default=8.0, help="抓取时长(秒); 启动搜索控制块要 2~4s")
    s.add_argument("--channel", type=int, default=0, help="RTT 上行通道号")
    s.add_argument("--out", default=None, help="输出文件(默认 temp/stm32-dev-rtt.log)")
    s.add_argument("--search", nargs=2, default=None, metavar=("START", "SIZE"),
                   help="控制块自动搜索失败时显式指定 RAM 范围, 如 --search 0x20000000 0x20000")
    s.add_argument("--address", default=None,
                   help="显式指定 RTT 控制块地址(默认自动从 ELF 符号 _SEGGER_RTT 解析)")
    s.add_argument("--check-seq", action="store_true",
                   help="自动分析抓包里的递增计数列, 报告丢帧")
    s.add_argument("--no-resume", action="store_true",
                   help="抓包前不自动恢复 CPU 运行(默认会 monitor go)")
    s.add_argument("--rtt-port", type=int, default=9090,
                   help="ST-Link/DAPLink 走 OpenOCD 时, RTT server 监听的本地 TCP 端口")
    s.set_defaults(func=cmd_rtt)

    s = sub.add_parser("build-verify"); add_common(s)
    s.add_argument("--build-cmd", default="make")
    s.add_argument("--flash-cmd", default="make flash")
    s.add_argument("--read", nargs="*", default=[])
    s.add_argument("--check", default=None)
    s.add_argument("--max-iter", type=int, default=1)
    s.add_argument("--wait", type=float, default=0.5)
    s.set_defaults(func=cmd_build_verify)
    return p


def main():
    global _JSON, _SERIAL, _PROBE, _DRY_RUN
    # Windows 控制台默认 GBK: 打印 -> / * / != 这类字符会直接 UnicodeEncodeError
    # 把整个命令弄崩(而命令本身其实是对的)。统一降级成 replace —— 宁可显示成
    # '?' 也不要命令失败。(坑 #8)
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args()
    _JSON = bool(getattr(args, "json", False))
    _DRY_RUN = bool(getattr(args, "dry_run", False))
    _CFG = load_config()
    _PROBE = _norm_probe(getattr(args, "probe", None) or os.environ.get("STM32_DEV_PROBE")
                         or _CFG.get("probe", ""))
    _SERIAL = (getattr(args, "serial", "") or _CFG.get("serial", "")
               or DEFAULT_SERIAL)
    if _JSON:
        buf = io.StringIO()
        rc = 0
        with contextlib.redirect_stdout(buf):
            try:
                rc = args.func(args)
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except SkillError as e:
                rc = 1
                _JSON_DATA["error"] = str(e)
            except _DryRun:
                rc = None
            except Exception as e:
                rc = 1
                _JSON_DATA["error"] = "%s: %s" % (type(e).__name__, e)
        # 约定: 命令返回 None = 成功(0); 返回 int = 那个退出码; 其它一律当失败(1)。
        # 这样 "打印 ERROR 后 return" 才真的算失败 —— 以前 None 被 or 0 吞成了成功。
        rc = 0 if rc is None else (rc if isinstance(rc, int) else 1)
        print(json.dumps({"cmd": args.cmd, "ok": rc == 0,
                          "data": _JSON_DATA, "text": buf.getvalue()},
                         ensure_ascii=True, indent=2))
        sys.exit(rc)
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        print("中断(Ctrl+C)")
        sys.exit(130)
    except SkillError as e:
        print("ERROR: %s" % e)
        sys.exit(1)
    except _DryRun:
        print("DRY-RUN: 到这儿停下, 板子没被动过。去掉 --dry-run 才真执行。")
        sys.exit(0)
    except Exception as e:
        print("ERROR: %s: %s" % (type(e).__name__, e))
        sys.exit(1)
    sys.exit(0 if rc is None else (rc if isinstance(rc, int) else 1))


if __name__ == "__main__":
    main()
