#!/bin/bash
# 窓口サーバ(ydev)で実行する。PRの情報と差分を gh で取得し、iEduq にレビュージョブを投入する。
# 使い方: ./submit_review.sh <owner/repo> <PR番号>
# 例:     ./submit_review.sh TechnoTUT/dmxctl 3
set -euo pipefail

REPO=${1:?owner/repo を指定してください}
PR=${2:?PR番号を指定してください}
MODEL=${MODEL:-flint:40b}

ROOT=$(cd "$(dirname "$0")" && pwd)
RUN="$ROOT/runs/${REPO//\//_}/pr${PR}-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"

gh pr view "$PR" -R "$REPO" --json number,title,body,author,baseRefName,headRefName,files,additions,deletions \
  > "$RUN/pr.json"
gh pr diff "$PR" -R "$REPO" > "$RUN/pr.diff"
echo "差分: $(wc -l < "$RUN/pr.diff") 行 -> $RUN"

qsub -o "$RUN/job.log" -v "RUN_DIR=$RUN,MODEL=$MODEL,ROOT=$ROOT" "$ROOT/jobs/review.pbs"
echo "結果は $RUN/review.md に出力されます(qstat -u $USER で状態確認)"
