# review-pr と review-server から source して使う。レビューサーバ(jobs/server.pbs)の一覧と生死の判定。
# 前提: ROOT が設定済み。
SERVERS=$ROOT/server

# サーバのディレクトリの一覧(新しい順)
list_servers() { ls -1d "$SERVERS"/*/ 2>/dev/null | sed 's:/$::' | sort -r || true; }

# サーバが生きているか: 状態が starting/ready で、ready なら heartbeat が 60 秒以内に更新されている。
# starting のあいだ(Ollama の準備中)は heartbeat が止まるので、ジョブがあるかで見る。
server_alive() {
  local d=$1 st hb now
  [ -f "$d/status" ] || return 1
  read -r st < "$d/status" || return 1
  case $st in
    starting) qstat "$(cat "$d/job" 2>/dev/null)" > /dev/null 2>&1 ;;
    ready) hb=$(stat -c %Y "$d/heartbeat" 2>/dev/null || echo 0); now=$(date +%s); (( now - hb < 60 )) ;;
    *) return 1 ;;
  esac
}

# server_desc <ディレクトリ>: 「iEduq yind01, flint:40b, 6h 12m left」のような説明
server_desc() {
  local d=$1 host=- model end=0 left q
  model=$(cat "$d/model"); q=$(cat "$d/queue_name")
  [ -f "$d/info" ] && read -r host model end < "$d/info"
  left=$(( end - $(date +%s) )); (( left < 0 )) && left=0
  if (( end > 0 )); then printf '%s %s, %s, %dh %02dm left' "$q" "$host" "$model" $((left / 3600)) $((left % 3600 / 60))
  else printf '%s, %s, starting' "$q" "$model"; fi
}
