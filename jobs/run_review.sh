# ジョブスクリプトと review-pr から source して使う。review.py の実行と、MCP 用の Python 環境の用意。
#
# 実行ディレクトリに mcp.json があれば(review-pr --mcp)、モデルが MCP のツールを使えるようにする。
# mcp SDK は Python 3.10 以上が要るので(システムの python3 は 3.9)、uv で SDK 入りの環境を作って動かす。
# uv のキャッシュ(~/.cache/uv)は /home にあり、計算ノードと共有される。review-pr が窓口サーバで
# 先に用意しておく(mcp_prepare)ので、ジョブの中ではネットワークを使わない(--offline)。
UV=${UV:-$HOME/.local/bin/uv}
MCP_SDK="mcp==2.3.0"
MCP_RUFF="ruff==0.17.3"            # mcp_check_server.py が使うリンタ
MCP_PYTHON=3.12

mcp_uv_run() { "$UV" run -q --no-project --python "$MCP_PYTHON" --with "$MCP_SDK" --with "$MCP_RUFF" "$@"; }

# 窓口サーバで実行する: SDK と Python を取得してキャッシュに入れる
mcp_prepare() { mcp_uv_run python -c 'import mcp' > /dev/null; }

# run_review <Ollama の URL> <モデル> <実行ディレクトリ>
run_review() {
  local url=$1 model=$2 run=$3
  local args=(--host "$url" --model "$model" --pr-json "$run/pr.json" --diff "$run/pr.diff"
              --out "$run/review.md" --stream "$run/stream.txt")
  # 入力の変わっていないパートのレビューを使い回す(review-pr --no-cache なら使わない)
  [ -e "$run/no-cache" ] || args+=(--cache-dir "$ROOT/cache")
  if [ -f "$run/mcp.json" ]; then
    echo "Using MCP tools ($run/mcp.json)"
    RUN_DIR=$run ROOT=$ROOT UV_PYTHON_DOWNLOADS=never \
      mcp_uv_run --offline python "$ROOT/review.py" "${args[@]}" --mcp-config "$run/mcp.json"
  else
    python3 "$ROOT/review.py" "${args[@]}"
  fi
}
