#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stm32-dev: 通用 STM32 J-Link + GDB 全流程调试/开发工具。

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

=== 关键坑(踩坑记录, 详见 SKILL.md) ===
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
                             capture_output=True, text=True, timeout=15).stdout
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
        out = subprocess.run([readelf, "-sW", elf], capture_output=True, text=True, timeout=30).stdout or ""
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
        out = subprocess.run([readelf, "-s", elf], capture_output=True, text=True, timeout=30).stdout
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
# gdb
# ---------------------------------------------------------------------------
def run_gdb(elf, commands, interactive=False, resume=True):
    """执行 gdb 命令。

    resume=True: 收尾用 `monitor go` 恢复目标运行。
      * `detach` 不等于恢复运行(坑#16) —— 读完变量后 CPU 一直停着, 后续 RTT/串口看起来像"板子死了";
      * 不能用 gdb 的 `continue`: --batch 下它会阻塞到目标停下(坑#8), 而 `monitor go` 立即返回。
    """
    gdb = find_gdb()
    if not gdb:
        return "ERROR: 找不到 arm-none-eabi-gdb / gdb-multiarch。请装 STM32CubeCLT 或设置 STM32_DEBUG_GDB。"
    cmds = ["target remote :%d" % GDB_PORT, "monitor halt"] + commands
    if not interactive:
        if resume:
            cmds += ["monitor go"]
        cmds += ["detach", "quit"]
    fd, path = tempfile.mkstemp(suffix=".gdb")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(cmds) + "\n")
    argv = [gdb] + ([] if interactive else ["--batch"]) + ["-x", path]
    if elf:
        argv.append(elf)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        return result.stdout + result.stderr
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
    srv = JLinkServer(dev, GDB_PORT, sn)
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
    print("=== STM32 J-Link Debug - 环境自检 ===")
    jlink = find_jlink_server()
    gdb = find_gdb()
    readelf = find_readelf()
    py = find_python()
    rtt = find_rtt_logger()
    probe = jlink   # J-Link GDB server 存在即可判定驱动就绪
    status = {
        "J-Link GDB server": ("OK  " + jlink) if jlink else "缺失 请装 SEGGER J-Link 驱动 (或设 JLINK_GDB_SERVER)",
        "arm-none-eabi-gdb": ("OK  " + gdb) if gdb else "缺失 请装 STM32CubeCLT 或设 STM32_DEBUG_GDB",
        "arm-none-eabi-readelf": ("OK  " + readelf) if readelf else "缺失(可选, 用于自动识别芯片)",
        "python3": ("OK  " + py) if py else "缺失 请装 python3",
        "JLinkRTTLogger(可选)": ("OK  " + rtt) if rtt else "缺失(可选, 仅 RTT 抓包用)",
    }
    for k, v in status.items():
        print("  [%s] %s" % ("OK" if v.startswith("OK") else "!!", v))

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
    if not stale:
        print("  [OK] 无残留 J-Link 进程")
    # 必需工具缺失 -> 退出码非零(doctor 的意义就是"环境是否就绪")
    missing_required = [k for k, v in (("JLinkGDBServer", jlink), ("arm-none-eabi-gdb", gdb),
                                       ("python3", py)) if not v]
    jset(tools={"jlink_gdb_server": jlink, "gdb": gdb, "readelf": readelf,
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
    print("--- J-Link / 板子 ---")
    if probe:
        print("  J-Link 驱动已就绪。连接板子后直接 read 调试即可。")
        print("  若连不上: 检查 SWD(SWDIO/SWCLK/GND/VCC)接线、板子供电、--device 型号。")
    else:
        print("  未找到 J-Link 驱动, 请先装 SEGGER J-Link 驱动。")
    print("--- 下一步 ---")
    print("  1) 编译出 ELF: make")
    print("  2) 读变量:   stm32-dev.py read g_motor --elf build/test.elf")
    print("  3) 若自动识别芯片失败: 加 --device STM32H743VI")
    if missing_required:
        print("!! 缺少必需工具: %s -> 退出码 1" % ", ".join(missing_required))
        return 1
    if not elf or not os.path.isfile(elf or ""):
        print("!! 提示: 没找到 ELF(--elf 可指定), 芯片识别/符号自检被跳过。")
    return 0


def cmd_read(args):
    elf = args.elf or infer_elf()
    if not elf:
        print("ERROR: 未找到 ELF。请用 --elf 指定, 或先 make。")
        return 1
    cmds = ["print %s" % t for t in args.targets]
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
            outs.append(with_server(args.device, elf, ["set %s = %s" % (var, val), "print %s" % var],
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
    """恢复运行。用 `monitor go` 而不是 gdb 的 `continue`:
    --batch 下 `continue` 会一直阻塞到目标停下(坑#8), `monitor go` 立即返回。"""
    print(with_server(args.device, args.elf or infer_elf(), ["monitor go"], resume=False))


def cmd_step(args):
    cmds = ["step"] if args.n <= 1 else ["step %d" % args.n]
    # 单步的意义就是停在每一步, 因此不自动恢复运行
    print(with_server(args.device, args.elf or infer_elf(), cmds, resume=False))


def cmd_info(args):
    print(with_server(args.device, args.elf or infer_elf(),
                      ["info registers", "bt", "info locals"],
                      resume=not args.keep_halted))


def cmd_start(args):
    """启动常驻 J-Link GDB server, 供后续 read/write 复用(延迟更低)。"""
    dev = resolve_device(args)      # 不猜型号: start 也要给对设备名
    srv = JLinkServer(dev, GDB_PORT, args.serial or _SERIAL)
    msg = srv.start(detached=True)
    print(msg)
    if msg.startswith("ERROR"):
        return 1
    print("常驻 J-Link GDB server 已启动。后续 read/write 将复用, 延迟更低")
    print("停止: stm32-dev.py stop")
    return 0


def cmd_attach(args):
    dev = resolve_device(args)
    srv = JLinkServer(dev, GDB_PORT, args.serial or _SERIAL)
    msg = srv.start(detached=True)
    print(msg)
    if msg.startswith("ERROR"):
        return 1
    gdb = find_gdb()
    print("启动交互式 GDB: %s %s  然后输入: target remote :%d (server 已常驻)" % (gdb, args.elf or "", GDB_PORT))


def cmd_stop(args):
    srv = JLinkServer(args.device or "STUB", GDB_PORT, "")
    print(srv.stop())


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


def cmd_flash(args):
    """用 JLink.exe 烧录 hex 到板子。"""
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
    # ★ 两个 Sleep 不能删(坑#80)!
    #   原来写的是 "loadfile ...\nr\ng\nexit\n": `g`(resume) 之后紧跟着 `exit`,
    #   J-Link 会在 MCU 还没真正跑起来时关掉调试会话, 结果是 CPU 停在复位后的状态
    #   —— 程序不跑, 串口/485 一个字都不回, 现象像是"烧完板子就死了"。
    #   实测: `r g exit` = 0/24 应答; `r Sleep 1200 g Sleep 1200 exit` = 24/24。
    #   四种写法 JLink 都不报错, 只能靠"烧完能不能通信"分辨。
    fd, script = tempfile.mkstemp(suffix=".jlink")
    with os.fdopen(fd, "w") as f:
        f.write("loadfile %s\nr\nSleep 1200\ng\nSleep 1200\nexit\n" % hexfile.replace("\\", "/"))
    gui_before = _jlink_gui_pids()   # JLink.exe 退出会留下 JLinkGUIServer, 收尾要清
    rc = 1          # 默认按失败算, 只有确认成功才置 0
    try:
        r = subprocess.run(
            [jlink, "-device", dev, "-if", "SWD", "-speed", "4000",
             "-autoconnect", "1", "-CommanderScript", script],
            capture_output=True, text=True, timeout=60,
        )
        out = r.stdout + r.stderr
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
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
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


def cmd_rtt(args):
    """抓取 RTT 上行通道(只读)。通用: 只要目标固件已集成 SEGGER RTT 即可。

    注意: RTT 与 JLinkGDBServerCL 独占同一台 J-Link, 抓包前先 stop。
    """
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
    # 每次用唯一文件名: JLinkRTTLogger 对已存在的文件是"追加"行为,
    # 复用固定路径会把上次的数据混进来, 造成"数据跨了几百秒"的假象。
    outfile = args.out or os.path.join(
        tempfile.gettempdir(), "stm32-dev-rtt-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    logfile = os.path.join(tempfile.gettempdir(), "stm32-dev-rtt-logger.log")
    for f in (outfile, logfile):
        try:
            os.remove(f)
        except OSError:
            pass
    argv = [logger, "-Device", dev, "-If", "SWD", "-Speed", "4000",
            "-RTTChannel", str(args.channel)]
    if args.address:
        argv += ["-RTTAddress", args.address]
    elif args.search:
        argv += ["-RTTSearchRanges", "%s %s" % (args.search[0], args.search[1])]
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
    data = ""
    if os.path.isfile(outfile):
        try:
            with open(outfile, "r", errors="replace") as f:
                data = f.read()
        except OSError:
            pass
    lines = [l for l in data.splitlines() if l.strip()]
    if not lines:
        print("!! 没抓到任何 RTT 数据。排查顺序(坑#27/#29/#30):")
        print("   1) 固件里是否真的集成了 SEGGER RTT 并调用了写接口(仅 J-Link 支持 RTT 不够);")
        print("   2) 控制块自动搜索失败 -> 加 --search <起始地址> <长度>(如 --search 0x20000000 0x20000);")
        print("   3) J-Link 是否被其它进程占用(先 stop, 关掉 RTT Viewer/IDE);")
        print("   4) M7/M55 开 D-Cache 时缓冲区需在非缓存区;")
        print("   5) 自动搜索命中了 RAM 里残留的旧控制块(换过固件/改过缓冲区位置) -> 用 --address 显式指定。")
        if os.path.isfile(logfile):
            try:
                with open(logfile, "r", errors="replace") as f:
                    print("   --- logger log ---")
                    print("\n".join(f.read().splitlines()[-15:]))
            except OSError:
                pass
        return 1        # 没抓到任何数据 = 失败
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


def _warn_stale_jlink():
    """开设备前检查会**独占** J-Link 的残留进程并告警。

    实测: JLinkGUIServer 不独占设备(它在场时 gdb read / RTT 抓包均正常), 因此不计入告警;
    真正独占的是 JLinkRTTLogger / JLinkGDBServer 这类正在用设备的进程。
    """
    stale = []
    for n in ("JLinkRTTLogger", "JLinkGDBServerCL", "JLinkGDBServer", "JLinkRemoteServer"):
        stale += [(n, p) for p in _pids_of(n)]
    if stale:
        print("!! 检测到残留 J-Link 进程(会独占设备, 后续命令会卡死):")
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
             "JLinkRemoteServer", "JLinkRemoteServerCL")
    own_pid = JLinkServer._read_pid()
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
                             text=True, timeout=15).stdout or ""
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
                                  "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=15).stdout or ""
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower().startswith(imagename.lower()):
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
        else:
            out = subprocess.run(["pgrep", "-f", imagename], capture_output=True, text=True, timeout=15).stdout or ""
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
    fd, path = tempfile.mkstemp(suffix=".jlink")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    gui_before = _jlink_gui_pids()
    try:
        r = subprocess.run([jlink, "-device", dev, "-if", "SWD", "-speed", "4000",
                            "-autoconnect", "1", "-CommanderScript", path],
                           capture_output=True, text=True, timeout=timeout)
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
        out = subprocess.run([readelf, "-lW", elf], capture_output=True, text=True, timeout=30).stdout or ""
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


def cmd_verify(args):
    """校验板子上的固件与 ELF 是否一致(坑#9: 板子跑旧固件是最常见的假 bug)。"""
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
    """复位并运行目标。"""
    dev = resolve_device(args, args.elf or infer_elf())
    # ★ `g` 与 `q` 之间必须有 Sleep(坑#80): 没 Sleep 时 J-Link 会在 MCU 还没跑起来
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


def cmd_init_rtt(args):
    """把 SEGGER RTT 源码放进工程(J-Link 安装目录自带则直接复制, 否则给出获取方式)。"""
    d = args.dir or "."
    dst = os.path.join(d, args.subdir)
    os.makedirs(dst, exist_ok=True)
    want = ["SEGGER_RTT.c", "SEGGER_RTT.h", "SEGGER_RTT_Conf.h",
            "SEGGER_RTT_ConfDefaults.h", "SEGGER_RTT_printf.c"]
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
        print("!! J-Link 安装目录里没找到 RTT 源码(新版安装包常不含)。")
        print("   请从 SEGGER 官方仓库取(BSD 许可): https://github.com/SEGGERMicro/RTT")
        print("   需要: " + ", ".join(want) + "  (RTT/ 与 Config/ 两个目录下)")
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


def cmd_rtt_send(args):
    """向 RTT 下行通道发数据(可选依赖: pip install pylink-square)。

    固件侧用 SEGGER_RTT_HasKey()/SEGGER_RTT_GetKey() 取; 与 GDB server 互斥。
    """
    try:
        import pylink
    except ImportError:
        print("ERROR: 需要 pylink-square -> pip install pylink-square")
        print("       替代: 起 RTT server 后用 JLinkRTTClient(默认 localhost:19021) 手动交互")
        return 1
    elf = args.elf or infer_elf()
    dev = resolve_device(args, elf)
    addr = args.address
    if not addr:
        sym = symbol_addr_from_elf(elf, "_SEGGER_RTT")
        if sym:
            addr = "0x%08X" % sym
    _warn_stale_jlink()
    payload = bytes.fromhex(args.hex) if args.hex else _unescape(args.data or "")
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


def add_common(parser):
    parser.add_argument("--json", action="store_true", help="输出 JSON(供脚本/CI 调用)")
    parser.add_argument("--elf", default=None, help="ELF 路径(自动推断芯片)")
    parser.add_argument("--device", default=None, help="芯片型号 e.g. STM32H743VI")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="J-Link 序列号")


def build_parser():
    p = argparse.ArgumentParser(prog="stm32-dev")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("doctor"); add_common(s)
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("read"); add_common(s); s.add_argument("targets", nargs="+")
    s.add_argument("--keep-halted", action="store_true", help="读完后不恢复运行(默认会 monitor go)")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("write"); add_common(s); s.add_argument("targets", nargs="+")
    s.add_argument("--keep-halted", action="store_true", help="写完后不恢复运行(默认会 monitor go)")
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
    s = sub.add_parser("verify"); add_common(s)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("reset"); add_common(s)
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
    s.add_argument("--speed", type=int, default=4000)
    s.set_defaults(func=cmd_rtt_send)

    s = sub.add_parser("init-rtt"); add_common(s)
    s.add_argument("--dir", default=None, help="工程目录(默认当前目录)")
    s.add_argument("--subdir", default=os.path.join("Middlewares", "SEGGER_RTT"), help="存放子目录")
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

    s = sub.add_parser("flash"); add_common(s); s.add_argument("--hex", default=None)
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
    global _JSON, _SERIAL
    args = build_parser().parse_args()
    _JSON = bool(getattr(args, "json", False))
    _SERIAL = getattr(args, "serial", "") or DEFAULT_SERIAL
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
    except Exception as e:
        print("ERROR: %s: %s" % (type(e).__name__, e))
        sys.exit(1)
    sys.exit(0 if rc is None else (rc if isinstance(rc, int) else 1))


if __name__ == "__main__":
    main()
