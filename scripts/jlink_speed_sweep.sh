#!/usr/bin/env bash
# jlink_speed_sweep.sh —— 扫 J-Link 的 SWD 速率：同段 Flash 各档读回，比 md5 + 记耗时
#
# 用法:
#   jlink_speed_sweep.sh <JLink.exe 路径> <型号> [地址] [长度] [速度kHz...]
# 例:
#   jlink_speed_sweep.sh "C:/Program Files/SEGGER/JLink_V794/JLink.exe" STM32H743VI 0x08000000 0x40000 4000 8000 12000 15000
#
# 判读:
#   - 各档 md5 必须全一致；不一致的那档跑不住，往下取。
#   - 看耗时拐点：USB 全速(12Mbit)的探针 8MHz 以上基本无增益。
#   - 最后把选定的速度写进工程配置（不要每次手写）。

set -u
JLINK="${1:?需要 JLink.exe 路径}"
DEV="${2:?需要芯片型号, 如 STM32H743VI}"
ADDR="${3:-0x08000000}"
SIZE="${4:-0x40000}"
shift 4 2>/dev/null || shift $#
SPEEDS="$*"
[ -z "${SPEEDS}" ] && SPEEDS="4000 8000 12000 15000"

OUTDIR="${TMPDIR:-/tmp}/jlink_speed_sweep"
mkdir -p "$OUTDIR"
OUTDIR_WIN=$(cd "$OUTDIR" && pwd -W 2>/dev/null || echo "$OUTDIR")

FAIL=0
REF=""
for sp in $SPEEDS; do
  SCRIPT="$OUTDIR/sp_$sp.jlink"
  BIN="$OUTDIR/rd_$sp.bin"
  {
    echo "si SWD"
    echo "device $DEV"
    echo "speed $sp"
    echo "connect"
    echo "speed $sp"
    echo "savebin $OUTDIR_WIN/rd_$sp.bin $ADDR $SIZE"
    echo "exit"
  } > "$SCRIPT"

  START=$(date +%s%N 2>/dev/null || echo 0)
  "$JLINK" -CommanderScript "$SCRIPT" >/dev/null 2>&1
  END=$(date +%s%N 2>/dev/null || echo 0)
  MS=$(( (END - START) / 1000000 ))

  if [ ! -s "$BIN" ]; then
    echo "speed ${sp} kHz : 读回失败(没有生成文件)"
    FAIL=1
    continue
  fi
  MD5=$(command md5sum "$BIN" 2>/dev/null | cut -c1-16)
  [ -z "$REF" ] && REF="$MD5"
  if [ "$MD5" = "$REF" ]; then
    echo "speed ${sp} kHz : ${MS} ms  md5=${MD5}  一致"
  else
    echo "speed ${sp} kHz : ${MS} ms  md5=${MD5}  **与基线不一致, 这档跑不住**"
    FAIL=1
  fi
done

exit $FAIL
