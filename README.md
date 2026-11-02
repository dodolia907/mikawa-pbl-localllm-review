# mikawa-pbl-localllm-review

豊橋技科大 HPC の GPU でローカル LLM を動かし、GitHub の PR をレビューする CLI。
`iEduq`(NVIDIA A100)と `Eduq`(AMD MI210)のどちらでも動く。

```bash
./review-pr TechnoTUT/dmxctl 3
./review-pr https://github.com/TechnoTUT/dmxctl/pull/3
```

## 仕組み

```
review-pr(窓口サーバ ydev)
  ├ gh で PR の情報と差分を取得(GitHub のトークンは計算ノードに渡さない)
  ├ qsub で jobs/review.pbs を投入 ─→ GPU ノード(iEduq: A100 / Eduq: MI210)
  │                                    ├ Ollama を起動し、モデルを用意して GPU に読み込む
  │                                    └ review.py: 差分を渡してレビューを得る
  ├ ジョブが終わるまで進み具合を表示(Ctrl-C でジョブも取り消す)
  └ レビューを表示(--post なら確認のうえ PR にコメント)
```

結果は `runs/<owner>_<repo>/pr<番号>-<日時>/` に残る。

| ファイル | 中身 |
|---|---|
| `review.md` | レビュー |
| `pr.json` / `pr.diff` | PR の情報と差分 |
| `progress` | ジョブの進み具合 |
| `stream.txt` / `stream.txt.stat` | 生成途中の出力 / 段階とトークン数 |
| `job.log` / `ollama.log` | ジョブと Ollama のログ |

## 初回セットアップ(窓口サーバで 1 回だけ)

```bash
# gh(管理者権限なしで ~/.local/bin に導入済み)のログイン
gh auth login

# Ollama を /work/$USER/llm に展開(約 1.4GB)
mkdir -p /work/$USER/llm/ollama && cd /work/$USER/llm
curl -fL -o ollama-linux-amd64.tar.zst \
  https://github.com/ollama/ollama/releases/download/v0.40.13/ollama-linux-amd64.tar.zst
tar --use-compress-program=zstd -xf ollama-linux-amd64.tar.zst -C ollama

# Eduq(AMD MI210)でも使う場合は、AMD(ROCm)版のライブラリを同じ場所に追加で展開する(約 1GB)
curl -fL -o ollama-linux-amd64-rocm.tar.zst \
  https://github.com/ollama/ollama/releases/download/v0.40.13/ollama-linux-amd64-rocm.tar.zst
tar --use-compress-program=zstd -xf ollama-linux-amd64-rocm.tar.zst -C ollama
```

モデル(既定 `flint:40b`、約 23GB)は初回のジョブの中で `/work/$USER/llm/models` に取得され、以降は使い回される。

## 使い方

```
review-pr [オプション] <owner/repo> <PR番号>
review-pr [オプション] <PR の URL>
review-pr                       (引数なし: 対話モードで順に選ぶ)

  -i, --interactive    対話モードで選ぶ(引数やオプションで指定した値が既定値になる)
  -q, --queue QUEUE    使うキュー: iEduq(NVIDIA A100、既定)か Eduq(AMD MI210)
  -g, --gpus N         使う GPU 数(iEduq は 1、Eduq は 1〜2。既定 1)
  -H, --host NODE      ノードを固定する(例 yind01)
  -m, --model MODEL    Ollama のモデル(既定 flint:40b)
  -o, --output FILE    レビューを FILE にも保存する
      --post           レビューを PR にコメントとして投稿する(投稿前に確認する)
      --no-wait        ジョブを投入したら待たずに終わる
      --quiet          生成途中の出力を流さず、状態表示だけにする
```

進み具合は標準エラーに、レビュー本文は標準出力に出るので、`> review.md` で本文だけ保存できる。

### 対話モード

引数なしで実行すると、リポジトリを入力した後、次の項目を矢印キーか Vim のキーで選ぶ。

- PR(オープンな PR の一覧。「その他」で番号を入力すれば、マージ済みの PR なども指定できる)
- キューと GPU(iEduq × 1、Eduq × 1、Eduq × 2)
- モデル(取得済みの一覧。「その他」で名前を入力できる)
- ノード(自動か固定)、PR に投稿するか、終わるまで待つか

| キー | 動き |
|---|---|
| `↑` / `k`、`↓` / `j` | 上下に移動 |
| `g` / `G` | 先頭 / 末尾へ |
| `1`〜`9` | その番号の選択肢へ |
| Enter | 決定 |
| `q`(または Ctrl-C) | 中止 |

最後に同じ内容を引数で指定するコマンドを表示し、確認してから実行する。
`-i` を付けると、引数やオプションで指定した値を既定値にして対話モードに入る。

```
$ ./review-pr
Repository (owner/repo): TechnoTUT/dmxctl
Pull request: #4  リファクタリング: engine.rs / ui_map.rs の分割と clippy 対応  (dodolia907, +2661/-2428)
Queue / GPU  (↑↓ / j k to move, Enter to select, q to cancel)
  iEduq  NVIDIA A100 80GB x1 (yind00/01)
> Eduq   AMD MI210 64GB x1 (ysnd00/01)
  Eduq   AMD MI210 64GB x2 (ysnd00/01; for models over 64GB, no speedup for models that fit on one)
```

### 実行中の表示

ジョブを待つ間、画面の一番下に Claude Code 風の状態表示の行を出し、モデルが生成した内容を上に流す。
考える過程(thinking)は薄い色、レビュー本文は通常の色で表示する。

```
── thinking ──
We need to review the CI workflow changes. The mac and windows jobs only run on ...
✻ Deliberating… (2m 41s · 3.4k tokens · thinking)  so a push to a branch never builds
```

- 状態表示の行は、経過時間・生成したトークン数・段階(queued → starting ollama → loading model onto gpu → reading diff → thinking → writing review)を示す。末尾は生成途中の行
- 本文はすでに画面に流れているので、終わった後は保存先だけを表示する。標準出力をリダイレクトしたときは、本文を出力する
- `--quiet` を付けると、生成途中の出力を流さず、状態表示だけにする
- 端末でないとき(ログに保存するときなど)は、アニメーションの代わりに段階の変化を 1 行ずつ出す
- CLI が表示する文言は英語。レビュー本文は日本語(`review.py` のプロンプトで指定)

### キュー

| キュー | ノード | GPU | 1 ジョブの上限 |
|---|---|---|---|
| `iEduq` | yind00, yind01 | NVIDIA A100 80GB × 1 | CPU 8、メモリ 64GB、8 時間。flint で約 56 トークン/秒 |
| `Eduq` | ysnd00, ysnd01 | AMD Instinct MI210 64GB × 2 | CPU 8、メモリ 64GB、GPU 2、8 時間。flint で約 30 トークン/秒 |

- どちらも同時実行は 1 人 1 ジョブ。2 つのキューは別々なので、iEduq と Eduq で 1 本ずつ同時に動かせる
- ジョブが保留(H)になると、`review-pr` はジョブを取り消して終わる。ノードの不調の可能性があるので、`-H` で別のノードを指定する

## モデルの選び方

PR #3(CI の変更、差分 99 行)で、指摘を差分と照らし合わせて比べた結果(2026-11-03、iEduq):

| モデル | 正しい指摘 | 誤った指摘 | 時間 |
|---|---|---|---|
| qwen3-coder:30b | 0 | 2 | 19 秒 |
| qwen3.8:27b-q8_0 | 3 | 0 | 243 秒 |
| muse-glimmer:30b-q8_0 | 4 | 2 | 181 秒 |
| **flint:40b**(既定) | **5** | **0** | 274 秒 |
| ornith-1.5:35b | 5 | 1 | 412 秒 |

PR 1 件だけでの比較なので、別の PR でも比べるとよい。同じ PR を複数のモデルで比べるには:

```bash
./submit_compare.sh runs/TechnoTUT_dmxctl/pr3-<日時> flint:40b qwen3.8:27b-q8_0
```

## 制約と注意

- 差分は約 16 万文字で打ち切る(コンテキスト長 131072 トークン)。打ち切られた後ろのファイルはレビューされない
- レビューは誤りを含むことがある。`--post` で投稿するときは、内容を確かめてから投稿する
- gh の認証情報は窓口サーバでだけ使う(計算ノードには PR の情報と差分だけを渡す)

## ファイル

| ファイル | 役割 |
|---|---|
| `review-pr` | CLI。PR の取得、ジョブの投入と待機、結果の表示と投稿 |
| `review.py` | Ollama にプロンプトを渡してレビューを得る(標準ライブラリのみ) |
| `jobs/review.pbs` | レビューのジョブ |
| `jobs/ollama_env.sh` | Ollama の起動、モデルの取得、GPU への読み込みの確認 |
| `jobs/qsub_common.sh` | キューに応じた qsub の引数(コンテナ・GPU 数・ノード) |
| `submit_compare.sh` / `jobs/compare.pbs` | 同じ PR を複数のモデルでレビューして比べる |
| `jobs/probe.pbs` / `jobs/probe-amd.pbs` | GPU とネットワークの疎通確認(iEduq / Eduq) |
