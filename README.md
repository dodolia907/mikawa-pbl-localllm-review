# mikawa-pbl-localllm-review

豊橋技科大 HPC の IoT・AI 基盤(`iEduq`、NVIDIA A100 80GB)でローカル LLM を動かし、GitHub の PR をレビューする。
講義では、モノレポに自分のチームの PR が出るたびに自動でレビューする。

## 仕組み

GitHub から HPC へは外から接続できず、窓口サーバでは cron も常駐も使えない。そのため、
**講義の時間帯に iEduq のジョブを 1 本動かし、その中で Ollama を起動したまま GitHub を定期的に確認する**。

```
schedule_watch.sh(窓口サーバ) --qsub -a 開始時刻--> jobs/watch.pbs(yind00/01, A100)
                                                     ├ Ollama 起動・モデル読み込み(GPU に載せたまま)
                                                     └ watch.py: 60 秒ごとにオープンな PR を確認
                                                          対象チームの新しい PR / 新しい push を見つけたら
                                                          レビュー → runs/<repo>/pr<番号>-<sha>.md
                                                          (post_comment が true なら PR にコメント)
```

- 同じコミットは 2 回レビューしない(`runs/<repo>/state.json` に記録)。push されると新しいコミットとしてレビューする
- 1 件の失敗で監視は止まらない。同じコミットで 3 回失敗したら諦める
- iEduq は同時実行 1 ジョブまでだが、待ち行列には複数積めるので、毎週分を前もって予約できる

| ファイル | 役割 |
|---|---|
| `schedule_watch.sh` | 窓口サーバで実行。監視ジョブを予約投入する |
| `jobs/watch.pbs` | 監視ジョブ |
| `watch.py` | PR の確認、対象の判定、レビュー、コメント投稿 |
| `review.py` | Ollama にプロンプトを渡してレビューを得る(標準ライブラリのみ) |
| `jobs/ollama_env.sh` | Ollama の起動とモデル取得(各ジョブから読み込む) |
| `submit_review.sh` / `jobs/review.pbs` | 1 件だけ単発でレビューする(試験用) |
| `jobs/probe.pbs` | GPU とネットワークの疎通確認用 |

## 初回セットアップ(窓口サーバで 1 回だけ)

```bash
# gh(管理者権限なしで ~/.local/bin に導入済み)のログイン
gh auth login

# Ollama を /work/$USER/llm に展開(約 1.4GB)
mkdir -p /work/$USER/llm/ollama && cd /work/$USER/llm
curl -fL -o ollama-linux-amd64.tar.zst \
  https://github.com/ollama/ollama/releases/download/v0.40.13/ollama-linux-amd64.tar.zst
tar --use-compress-program=zstd -xf ollama-linux-amd64.tar.zst -C ollama
```

モデル(既定 `flint:40b`、約 23GB。PR #3 での 5 モデル比較で最も正確だった)は初回ジョブの中で `/work/$USER/llm/models` に取得され、以降は使い回される。
初回だけ取得に数分かかるので、講義前に一度試しておく。

## 設定(config.json)

`config.example.json` をコピーして編集する。

| キー | 意味 |
|---|---|
| `repo` | 監視するリポジトリ(`owner/repo`) |
| `model` | Ollama のモデル名 |
| `team.paths` | チームのディレクトリ(例 `["teams/team3/"]`)。**末尾に `/` を付ける**(`teams/team3` だと `teams/team30` も一致する) |
| `team.authors` | チームメンバーの GitHub アカウント |
| `team.labels` | チームのラベル |
| `base_branches` | マージ先ブランチを限定する場合に指定 |
| `diff_team_paths_only` | true なら、チームのディレクトリ以外の差分は LLM に渡さない |
| `skip_draft` | true なら下書き PR は対象外 |
| `post_comment` | true なら PR にコメントを投稿する。false ならファイルに保存するだけ |
| `poll_interval_sec` | GitHub を確認する間隔(秒) |

`team` の各条件は、空なら制限しない。複数指定したらすべてを満たす PR だけが対象になる。

## 使い方

```bash
# 講義時間に合わせて予約(例: 11/10 13:00 から 100 分)。毎週分を並べて予約してよい
./schedule_watch.sh config.json "2026-11-10 13:00" 100
./schedule_watch.sh config.json "2026-11-17 13:00" 100

# 今すぐ 90 分
./schedule_watch.sh config.json

qstat -u $USER                       # 状態確認(予約中は W と表示される)
tail -f jobs/logs/live-<ジョブ番号>.log   # 監視中のログ(watch-*.log はジョブ終了後に書き出される)
ls runs/<owner>_<repo>/              # レビュー結果
qdel <ジョブID>                       # 予約の取り消し
```

1 件だけ試す: `./submit_review.sh TechnoTUT/dmxctl 3`

## 制約と注意

- iEduq は同時実行 1 ジョブ、GPU 1 枚、最長 8 時間。監視時間 + 20 分(書き終えるための余裕)が 8 時間を超える予約はできない
- 混雑していると、指定した開始時刻を過ぎてから始まることがある
- 差分は約 16 万文字で打ち切る(コンテキスト長 131072 トークン)。打ち切られた後ろのファイルはレビューされない
- 計算ノードでは `~/.config/gh` の認証情報を使う。講義で使う場合は、対象リポジトリだけに権限を絞った
  fine-grained token(Contents: Read、Pull requests: Read and write)を使うほうが安全
