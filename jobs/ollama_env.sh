# ジョブスクリプトから source して使う。Ollama を起動し、モデルを用意して OLLAMA_URL を設定する。
# 前提: MODEL が設定済み。
LLM_HOME=${LLM_HOME:-/work/$USER/llm}
OLLAMA=$LLM_HOME/ollama/bin/ollama
export OLLAMA_MODELS=$LLM_HOME/models      # モデルは /work に置き、ジョブ間で使い回す
export OLLAMA_HOST=127.0.0.1:$((20000 + RANDOM % 10000))
# 入力(差分は最大 16 万文字 ≒ 5 万トークン)+ 考える過程(大きな PR で 1.5 万トークン以上)+ 本文が収まる長さ。
# 65536 では PR #4(入力 5 万トークン)で考える途中に上限に達し、本文が空になった。
export OLLAMA_CONTEXT_LENGTH=${OLLAMA_CONTEXT_LENGTH:-131072}
export OLLAMA_KEEP_ALIVE=-1                # 監視中にモデルを GPU から降ろさない
OLLAMA_URL=http://$OLLAMA_HOST

if [ ! -x "$OLLAMA" ]; then
  echo "Ollama が見つかりません: $OLLAMA (README の手順で導入してください)" >&2; exit 1
fi

hostname; nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv

"$OLLAMA" serve > "${OLLAMA_LOG:-/dev/null}" 2>&1 &
OLLAMA_PID=$!
trap 'kill $OLLAMA_PID 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -sf "$OLLAMA_URL/api/version" >/dev/null && break; sleep 2; done

# モデルのファイル(blob)を curl で先に取得する。
# Ollama の配信網は中継サーバによって極端に遅いことがあり(32MB/s と 8KB/s の差を確認)、
# ollama pull は遅い接続でも待ち続ける。curl なら遅いときに切って、続きからつなぎ直せる。
# blob が揃っていれば、ollama pull は目録の取得だけで終わる。
REGISTRY=https://registry.ollama.ai
fetch_blobs() {
  local m=$1 name tag repo digest size dest tmp try
  name=${m%%:*}; tag=${m#*:}; [ "$tag" = "$m" ] && tag=latest
  case $name in */*) repo=$name ;; *) repo=library/$name ;; esac
  local manifest
  manifest=$(curl -sfL -m 60 -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "$REGISTRY/v2/$repo/manifests/$tag") || { echo "fetch $m: 目録を取得できません" >&2; return 1; }
  mkdir -p "$OLLAMA_MODELS/blobs"
  python3 -c 'import json,sys; m=json.load(sys.stdin)
for l in m["layers"] + [m["config"]]: print(l["digest"], l["size"])' <<< "$manifest" |
  while read -r digest size; do
    dest=$OLLAMA_MODELS/blobs/${digest/:/-}
    [ -s "$dest" ] && continue
    tmp=$dest.curl
    for try in $(seq 30); do
      curl -sfL -C - --speed-limit 1000000 --speed-time 30 -m 3600 -o "$tmp" \
        "$REGISTRY/v2/$repo/blobs/$digest" && break
      echo "fetch $m: ${digest:7:12} 遅いか切れたので再接続 ($try/30, $(( $(stat -c %s "$tmp" 2>/dev/null || echo 0) / 1000000 ))/$((size / 1000000)) MB)"
    done
    if [ "$(stat -c %s "$tmp" 2>/dev/null)" = "$size" ] && \
       [ "$(sha256sum "$tmp" | cut -d' ' -f1)" = "${digest#sha256:}" ]; then
      mv "$tmp" "$dest"; rm -f "$dest"-partial*     # ollama pull の取得途中のファイルは不要になる
      echo "fetch $m: ${digest:7:12} 完了 ($((size / 1000000)) MB)"
    else
      echo "fetch $m: ${digest:7:12} 取得失敗" >&2; rm -f "$tmp"; return 1
    fi
  done
}

# モデルを取得する。2回目以降はキャッシュ済みなのですぐ終わる。
# curl での事前取得に失敗した場合に備え、ollama pull も止まったら中断してやり直す。
pull_model() {
  local m=$1 try size last stalled
  fetch_blobs "$m" || echo "fetch $m: 事前取得に失敗したので ollama pull に任せる"
  for try in 1 2 3 4 5; do
    "$OLLAMA" pull "$m" > /dev/null 2>&1 &
    local pid=$! last=-1 stalled=0
    while kill -0 $pid 2>/dev/null; do
      sleep 15
      size=$(du -sk "$OLLAMA_MODELS" | cut -f1)   # 実際に書き込まれた量(KB)
      if [ "$size" = "$last" ]; then stalled=$((stalled + 15)); else stalled=0; last=$size; fi
      if [ $stalled -ge "${STALL_SEC:-180}" ]; then
        echo "pull $m: ${stalled} 秒進まないので再試行 ($try/5)"; kill $pid; break
      fi
    done
    if wait $pid; then echo "pull $m: 完了"; return 0; fi
  done
  echo "pull $m: 失敗" >&2; return 1
}
# モデルを読み込み、GPU に載ったことを確かめる。
# CUDA の初期化に失敗すると Ollama は黙って CPU で動き、レビュー 1 件に何十分もかかる。
# その場合は続けずにエラーで終わる(同じ手順でも GPU に載る回と載らない回があった)。
load_on_gpu() {
  local m=$1 vram
  curl -sf -m 600 "$OLLAMA_URL/api/generate" -d "{\"model\":\"$m\",\"keep_alive\":-1}" > /dev/null
  vram=$(curl -sf "$OLLAMA_URL/api/ps" | python3 -c 'import json,sys,os
print(sum(x.get("size_vram",0) for x in json.load(sys.stdin)["models"] if x["name"]==sys.argv[1] or x["model"]==sys.argv[1]))' "$m")
  if [ "${vram:-0}" -le 0 ]; then
    echo "load $m: GPU に載りませんでした(CPU で動いている)。ollama のログで CUDA のエラーを確認してください" >&2
    grep -m3 -iE 'cuda.*(fail|error)' "${OLLAMA_LOG:-/dev/null}" >&2 || true
    return 1
  fi
  echo "load $m: GPU に $((vram / 1024 / 1024)) MiB 載りました"
}

pull_model "$MODEL"
load_on_gpu "$MODEL"
