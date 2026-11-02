#!/bin/bash
# 窓口サーバ(ydev)で実行する。指定時刻から指定時間、PR を監視して自動レビューするジョブを予約する。
# 使い方: ./schedule_watch.sh <config.json> [開始日時] [監視する分数]
# 例:     ./schedule_watch.sh config.json "2026-11-10 13:00" 100   # 講義時間に合わせて予約
#         ./schedule_watch.sh config.json                          # 今すぐ 90 分
# iEduq は同時実行 1 ジョブまでだが、待ち行列には複数積めるので、毎週分をまとめて予約できる。
set -euo pipefail

CONFIG=$(realpath "${1:?config.json を指定してください}")
START=${2:-}
MINUTES=${3:-90}
MARGIN=20   # 終了直前に始まったレビューを書き終えるための余裕(分)

if (( MINUTES + MARGIN > 480 )); then
  echo "iEduq の上限は 8 時間です(監視 ${MINUTES} 分 + 余裕 ${MARGIN} 分)" >&2; exit 1
fi
python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); assert "/" in c["repo"] and c["model"]' "$CONFIG"

ROOT=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$ROOT/jobs/logs"
TOTAL=$(( MINUTES + MARGIN ))
WALL=$(printf '%02d:%02d:00' $(( TOTAL / 60 )) $(( TOTAL % 60 )))

ARGS=(-l "walltime=$WALL" -v "CONFIG=$CONFIG,WATCH_MINUTES=$MINUTES,ROOT=$ROOT")
# GPU ノードを固定したいとき: HOST=yind01 ./schedule_watch.sh ...(yind00 が不調だったことがある)
if [ -n "${HOST:-}" ]; then
  ARGS+=(-l "select=1:host=$HOST:ncpus=8:mem=64gb:ngpus=1")
fi
if [ -n "$START" ]; then
  ARGS+=(-a "$(date -d "$START" +%Y%m%d%H%M)")
  LOG=$ROOT/jobs/logs/watch-$(date -d "$START" +%Y%m%d-%H%M).log
else
  LOG=$ROOT/jobs/logs/watch-$(date +%Y%m%d-%H%M).log
fi
qsub -o "$LOG" "${ARGS[@]}" "$ROOT/jobs/watch.pbs"
echo "開始: ${START:-すぐ} / 監視 ${MINUTES} 分 / ログ: $LOG"
