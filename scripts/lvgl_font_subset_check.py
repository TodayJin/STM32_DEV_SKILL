#!/usr/bin/env python3
"""校验 LVGL 子集字体是否覆盖界面源码里用到的全部汉字。

症状对应：界面新增/改动文案后，屏幕上那几个字变成空心方框（tofu）——
因为子集字体是按旧文案生成的，新字不在里面，而 LVGL 不会报错。

用法：
    python lvgl_font_subset_check.py --font app/fonts/cjk10.c --src app --src drv
    # 退出码 0 = 全部覆盖；1 = 缺字（并打印可直接执行的 lv_font_conv 命令）

依赖：仅标准库。lv_font_conv 输出格式兼容（range_start + unicode_list 偏移）。
"""
import argparse
import os
import re
import sys

CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef\u2000-\u27ff]")


def collect_chars(paths, exts=(".c", ".h")):
    found = {}
    for root in paths:
        if os.path.isfile(root):
            files = [root]
        else:
            files = []
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    if n.endswith(exts):
                        files.append(os.path.join(dirpath, n))
        for f in files:
            try:
                text = open(f, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for ch in CJK_RE.findall(text):
                found.setdefault(ch, set()).add(os.path.basename(f))
    return found


def parse_font(path):
    """返回字体覆盖的 codepoint 集合。"""
    src = open(path, encoding="utf-8", errors="ignore").read()

    lists = {}
    for m in re.finditer(r"static\s+const\s+uint16_t\s+(unicode_list_\d+)\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;", src, re.S):
        lists[m.group(1)] = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]+)", m.group(2))]
    if not lists and "unicode_list" not in src:
        pass

    covered = set()
    dsc = re.search(r"lv_font_fmt_txt_dsc_t\s+font_dsc\s*=\s*\{(.*?)\n\};?", src, re.S)
    body = dsc.group(1) if dsc else src

    entries = re.findall(r"\.range_start\s*=\s*(0x[0-9A-Fa-f]+|\d+).*?\.range_length\s*=\s*(0x[0-9A-Fa-f]+|\d+)(.*?)(?=\.range_start|\Z)", body, re.S)
    for start_s, len_s, tail in entries:
        start = int(start_s, 0)
        length = int(len_s, 0)
        m = re.search(r"\.unicode_list\s*=\s*(unicode_list_\d+|NULL|0)", tail)
        name = m.group(1) if m else "NULL"
        if name in lists:
            covered.update(start + off for off in lists[name])
        else:
            covered.update(range(start, start + max(length, 1)))

    if not covered:                                     # 退路：所有列出的偏移按 0 基解释
        for vals in lists.values():
            covered.update(vals)
    return covered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", required=True, help="lv_font_conv 生成的 .c，例如 app/fonts/cjk10.c")
    ap.add_argument("--src", action="append", required=True, help="扫汉字的源码目录/文件，可多次给")
    ap.add_argument("--ttf", default="C:/tmp/msyh0.ttf", help="生成字体用的 TTF（打印重建命令时用）")
    ap.add_argument("--size", default="10", help="字号，打印重建命令时用")
    args = ap.parse_args()

    used = collect_chars(args.src)
    covered = parse_font(args.font)
    missing = {c: v for c, v in used.items() if ord(c) not in covered}

    print(f"字体 {args.font}: 覆盖 {len(covered)} 个码位")
    print(f"源码用字 {len(used)} 个")
    if not missing:
        print("OK: 所有汉字都在子集字体里")
        return 0

    print(f"缺字 {len(missing)} 个：")
    for ch, where in sorted(missing.items()):
        print(f"  {ch} U+{ord(ch):04X}  ({', '.join(sorted(where))})")
    syms = "".join(sorted(used))
    print("\n重新生成（注意 --no-compress）：")
    print(f'  lv_font_conv --font {args.ttf} --size {args.size} --bpp 4 --no-compress \\')
    print(f'    --format lvgl --lv-include lvgl.h --range 0x20-0x7E --symbols "{syms}" -o {args.font}')
    return 1


if __name__ == "__main__":
    sys.exit(main())
