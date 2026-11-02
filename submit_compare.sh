#!/bin/bash
# 窓口サーバ(ydev)で実行する。submit_review.sh が作った実行ディレクトリの PR を、複数のモデルでレビューして比べる。
# 使い方: [QUEUE=iEduq|Eduq] [NGPUS=n] [HOST=node] ./submit_compare.sh <runs/... のディレクトリ> <モデル> [モデル...]
# 例:     ./submit_compare.sh runs/TechnoTUT_dmxctl/pr3-20261103-023550 flint:40b qwen3.8:27b-q8_0
# 結果はそのディレクトリの review-<モデル>.md に出力される。
set -euo pipefail

RUN=$(realpath "${1:?実行ディレクトリを指定してください}")
shift
(( $# > 0 )) || { echo "モデルを 1 つ以上指定してください" >&2; exit 1; }
[ -f "$RUN/pr.json" ] && [ -f "$RUN/pr.diff" ] || { echo "$RUN に pr.json と pr.diff がありません" >&2; exit 1; }

ROOT=$(cd "$(dirname "$0")" && pwd)
. "$ROOT/jobs/qsub_common.sh"
# PBS の -v はカンマで区切るので、モデル名の区切りは空白にする
qsub -o "$RUN/compare-$QUEUE.log" "${QSUB_ARGS[@]}" \
  -v "$QSUB_VARS,RUN_DIR=$RUN,ROOT=$ROOT,MODELS=$*" "$ROOT/jobs/compare.pbs"
echo "キュー: $QUEUE / モデル: $* / 進み具合: $RUN/compare-live.log"
