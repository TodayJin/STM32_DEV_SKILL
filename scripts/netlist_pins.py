#!/usr/bin/env python3
"""从 EasyEDA 网表导出「元件.引脚 -> 网络」并反查，用于拿网表核固件引脚配置。

为什么要有这个脚本：换板/改板后最容易错的是**引脚分配**（TX/RX 接反、AF 漏配、
端子编号和文档不一致）。网表是唯一权威，但手翻 PDF 容易漏；用它把两边对照打印出来。

支持两种格式（EasyEDA 导出）：
  *.enet  JSON（components[].props.Designator + pinInfoMap[].net）
  *.tel   文本（`$NETS` 段：网络名一行，后面跟 `元件.脚` 列表，可跨行，名字可能带引号）

用法：
  python netlist_pins.py board.tel                      # 概览：每个网络的行数/前几个脚
  python netlist_pins.py board.tel --part U21 U18       # 只看这些元件的每个脚接哪个网络
  python netlist_pins.py board.tel --net PC4 PC5 PA7    # 反查：这些网络上有哪些脚
  python netlist_pins.py board.enet --part U23 --net PA1
"""
import argparse
import json
import re
import sys

PIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_]+$")


def load_tel(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    if "$NETS" not in raw:
        sys.exit("这个 .tel 里没有 $NETS 段：%s" % path)
    body = raw.split("$NETS", 1)[1].split("$SCHEDULE")[0]

    nets = {}
    cur = None
    for line in body.split("\n"):
        line = line.strip().rstrip(",").strip()
        if not line or line.startswith("$"):
            continue
        toks = [t for t in re.split(r"[\s,;]+", line) if t]
        if not toks:
            continue
        # 行首不是「元件.脚」=> 这是新网络名（可能带单引号，如 '$4N12'）
        if not PIN_RE.match(toks[0]):
            cur = toks[0].strip("'")
            nets.setdefault(cur, [])
            toks = toks[1:]
        if cur is not None:
            nets[cur].extend(t for t in toks if PIN_RE.match(t))
    return nets


def load_enet(path):
    data = json.load(open(path, encoding="utf-8"))
    comps = data.get("components") or {}
    nets = {}
    if isinstance(comps, dict):
        items = comps.values()
    else:
        items = comps
    for comp in items:
        props = comp.get("props", {})
        ref = props.get("Designator", "")
        for pin in (comp.get("pinInfoMap") or {}).values():
            net = pin.get("net", "")
            num = pin.get("number", "")
            if ref and net and num:
                nets.setdefault(net, []).append("%s.%s" % (ref, num))
    return nets


def load(path):
    return load_enet(path) if path.lower().endswith(".enet") else load_tel(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    ap.add_argument("--part", nargs="*", default=[], help="只看这些元件（如 U21 U18）")
    ap.add_argument("--net", nargs="*", default=[], help="反查这些网络（如 PC4 PA7）")
    args = ap.parse_args()

    nets = load(args.netlist)

    if args.part:
        for part in args.part:
            print("=" * 24, part)
            rows = {}
            for name, pins in nets.items():
                for p in pins:
                    if p.upper().startswith(part.upper() + "."):
                        rows.setdefault(p.split(".", 1)[1], set()).add(name)
            if not rows:
                print("  (没找到该元件)")
            for pin in sorted(rows, key=lambda x: (len(x), x)):
                print("  pin %-6s -> %s" % (pin, sorted(rows[pin])))

    if args.net:
        for want in args.net:
            hit_names = [n for n in nets if n == want] or [n for n in nets if want.upper() in n.upper()]
            if not hit_names:
                print("%-10s -> (网表里没有这个网络名)" % want)
            for n in hit_names:
                print("%-10s -> %s" % (n, nets[n]))

    if not args.part and not args.net:
        print("共 %d 个网络，前 30 个：" % len(nets))
        for name in list(nets)[:30]:
            print("  %-16s %s" % (name, nets[name][:6]))


if __name__ == "__main__":
    main()
