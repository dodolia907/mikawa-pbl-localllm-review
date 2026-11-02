"""Ollama 上のローカルLLMに PR の差分を渡し、レビューを Markdown で返す。標準ライブラリのみ使用。

ジョブ(jobs/review.pbs)の中で、起動済みの Ollama に対して実行される:
  python3 review.py --host URL --model M --pr-json pr.json --diff pr.diff --out review.md [--stream stream.txt]

大きな PR(差分が PART_CHARS を超えるもの)は、ファイルごとにパートに分けてレビューし、
最後に各パートのレビューを 1 つにまとめる。パートごとのレビューは <out> と同じ場所の review-parts.md に残す。
"""
import argparse
import json
import os
import re
import time
import urllib.request

SYSTEM = """あなたは経験豊富なソフトウェアエンジニアで、大学の講義で学生が作成した GitHub の Pull Request をレビューします。
次の方針で日本語でレビューしてください。
- バグ・誤動作・セキュリティ上の問題を最優先で指摘する。根拠となる差分の箇所(ファイル名と行)を示す。
- なぜ問題なのか、どう直せばよいかを、学生が理解できるように具体的に書く。
- 確信が持てない指摘は「要確認」と明記する。推測で断定しない。
- 好みの問題(命名・書式)は重要なものに絞る。
- 問題がなければ無理に指摘を作らず「大きな問題なし」と書く。
出力形式:
## 概要
(PRが何をしているかを2〜3文で)
## 指摘事項
(重要度 高/中/低 を付けた箇条書き。なければ「なし」)
## 総評
"""

SYSTEM_MERGE = """あなたは経験豊富なソフトウェアエンジニアです。
大きな Pull Request をファイルごとのパートに分けてレビューした結果が与えられるので、1 つのレビューにまとめてください。
- 日本語で書く。各パートの指摘を重要度(高/中/低)の順に並べ直し、同じ内容の指摘は 1 つにまとめる。
- 指摘のファイル名と行、「要確認」の明記は残す。パートのレビューにない指摘を新しく作らない。
- パートをまたぐ問題(あるファイルの変更が別のファイルと食い違うなど)に気づいたら、「要確認」として加えてよい。
出力形式:
## 概要
(PR 全体が何をしているかを2〜3文で)
## 指摘事項
(重要度 高/中/低 を付けた箇条書き。なければ「なし」)
## 総評
"""

# 1 回のリクエストで渡す差分の上限(文字数)。約 4 文字 = 1 トークンなので約 1 万トークン。
# 大きすぎると、モデルが考える過程だけでコンテキストを使い切り、本文を書かずに終わる
# (PR #4、入力 5 万トークンで、考える過程が 8 万トークンを超えた)。
PART_CHARS = 40_000
# 1 回のリクエストで生成するトークン数の上限。これに達して本文が空なら、考える過程なしでやり直す。
MAX_OUTPUT_TOKENS = 40_960


def split_diff(diff):
    """unified diff をファイル単位に分け、(パス, 本文) のリストを返す。"""
    out = []
    for p in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", p)
        if m:
            out.append((m.group(2), p))
    return out


def split_file(path, body, limit):
    """1 ファイルの差分が limit を超えるとき、変更箇所(@@ で始まるハンク)の単位で分ける。

    各片の先頭にはファイルのヘッダ(diff --git 〜 +++)を付ける。1 つのハンクだけで limit を超えるときは行で切る。
    """
    m = re.search(r"(?m)^@@", body)
    if len(body) <= limit or not m:
        return [body]
    header, rest = body[:m.start()], body[m.start():]
    hunks = re.split(r"(?m)^(?=@@)", rest)
    pieces, cur = [], ""
    for h in hunks:
        while len(h) > limit - len(header):              # 巨大なハンクは行の区切りで切る
            cut = h.rfind("\n", 0, limit - len(header)) + 1 or limit - len(header)
            if cur:
                pieces.append(cur); cur = ""
            pieces.append(h[:cut]); h = h[cut:]
        if cur and len(cur) + len(h) > limit - len(header):
            pieces.append(cur); cur = ""
        cur += h
    if cur:
        pieces.append(cur)
    return [header + p for p in pieces]


def make_parts(diff, limit=PART_CHARS):
    """差分を、limit 文字以下のパートに分ける。ファイルの順番は保ち、小さなファイルはまとめる。

    戻り値: [(パートに含まれるファイルのリスト, 差分), ...]
    """
    parts, paths, buf = [], [], ""
    for path, body in split_diff(diff):
        for piece in split_file(path, body, limit):
            if buf and len(buf) + len(piece) > limit:
                parts.append((paths, buf)); paths, buf = [], ""
            if path not in paths:
                paths.append(path)
            buf += piece
    if buf:
        parts.append((paths, buf))
    return parts or [([], diff)]


def pr_header(pr, files):
    file_list = "\n".join(f"- {f['path']} (+{f['additions']}/-{f['deletions']})" for f in files)
    return (f"# PR #{pr['number']}: {pr['title']}\n"
            f"base: {pr['baseRefName']} <- head: {pr['headRefName']}\n\n"
            f"## 説明\n{pr.get('body') or '(なし)'}\n\n"
            f"## 変更ファイル\n{file_list}\n\n")


def build_prompt(pr, files, diff, note=""):
    return pr_header(pr, files) + note + f"## 差分\n```diff\n{diff}\n```\n"


class StreamWriter:
    """生成途中の出力をファイルに書き足す。review-pr がこれを読んで画面に流す。

    - <path>: 各リクエストの見出し、考える過程(=== Thinking ===)、本文(=== Review ===)を書き足す
    - <path>.stat: 「段階 トークン数 パート 全パート数」の 1 行
        段階は reading / thinking / writing / done、パートは番号か merge(まとめ)か -(分けていない)
    path が None なら何もしない。画面に出す見出しは英語にしている。
    """

    def __init__(self, path=None):
        self.path = path
        self.f = open(path, "w", encoding="utf-8") if path else None
        self.tokens = 0                        # 全リクエストの合計
        self.part, self.total = "-", 0
        self.t0 = time.time()
        self.section = None
        self.last_flush = self.t0
        self.stat("reading")

    def stat(self, phase):
        if not self.path:
            return
        tmp = self.path + ".stat.tmp"
        with open(tmp, "w") as f:
            f.write(f"{phase} {self.tokens} {self.part} {self.total}\n")
        os.replace(tmp, self.path + ".stat")   # 読む側が書きかけを見ないよう、置き換えで更新する

    def note(self, text):
        if self.f:
            self.f.write(text)
            self.f.flush()

    def begin(self, title, part="-", total=0):
        """1 回のリクエストの始まり。"""
        self.part, self.total, self.section = part, total, None
        self.req_tokens, self.req_t0 = 0, time.time()
        self.note(f"\n=== {title} ===\n")
        self.stat("reading")

    def write(self, kind, text):
        if not self.f or not text:
            return
        if kind != self.section:
            self.f.write(f"\n=== {'Thinking' if kind == 'thinking' else 'Review'} ===\n")
            self.section = kind
        self.f.write(text)
        self.tokens += 1                       # ストリームの 1 チャンクがおよそ 1 トークン
        self.req_tokens += 1
        now = time.time()
        if now - self.last_flush >= 0.3:       # 細かすぎる書き込みを避け、0.3 秒ごとに反映する
            self.f.flush()
            self.stat("thinking" if kind == "thinking" else "writing")
            self.last_flush = now

    def end(self, res, wait_s):
        """1 回のリクエストの終わり。トークン数と速さを書く。"""
        n, ns = res.get("eval_count") or self.req_tokens, res.get("eval_duration") or 0
        self.tokens += n - self.req_tokens     # 概算の数を、Ollama が数えた正しい数に置き換える
        rate = f", {n / (ns / 1e9):.0f} tok/s" if ns else ""
        self.note(f"\n\n=== {res.get('prompt_eval_count')} input tokens (read in {wait_s:.0f}s), "
                  f"{n} output tokens{rate}, {time.time() - self.req_t0:.0f}s ===\n")

    def finish(self):
        self.note(f"\n=== Done: {self.tokens} output tokens in total, {time.time() - self.t0:.0f}s ===\n")
        if self.f:
            self.f.close()
        self.stat("done")


def chat(host, model, system, user, stream, think=None, timeout=3600):
    """Ollama の /api/chat をストリーミングで呼ぶ。

    届いた分を stream に書き足し、最後の応答(トークン数などを含む)に本文と考える過程をまとめて返す。
    think=False なら考える過程なしで答えさせる(None ならモデルの既定)。
    """
    req_body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": True,
        "options": {"temperature": 0.2, "num_predict": MAX_OUTPUT_TOKENS},
    }
    if think is not None:
        req_body["think"] = think
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(req_body).encode(),
                                 headers={"Content-Type": "application/json"})
    content, thinking, first, res = [], [], None, None
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:                        # 1 行に 1 つの JSON が届く
            if not line.strip():
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(f"Ollama error: {chunk['error']}")
            msg = chunk.get("message", {})
            if first is None and (msg.get("thinking") or msg.get("content")):
                first = time.time() - t0      # 入力を読み終えて生成が始まるまでの時間
            if msg.get("thinking"):
                thinking.append(msg["thinking"])
                stream.write("thinking", msg["thinking"])
            if msg.get("content"):
                content.append(msg["content"])
                stream.write("content", msg["content"])
            if chunk.get("done"):
                res = chunk
                break
    if res is None:
        raise RuntimeError("the Ollama response ended unexpectedly")
    res["message"] = {"content": "".join(content), "thinking": "".join(thinking)}
    stream.end(res, first or 0)
    return res


def ask(host, model, system, user, stream, title, part="-", total=0):
    """1 回分のレビュー(またはまとめ)を得る。本文が空なら考える過程なしでやり直す。戻り値: (本文, 応答)"""
    stream.begin(title, part, total)
    res = chat(host, model, system, user, stream)
    text = res["message"]["content"].strip()
    if not text:
        # 考える過程だけで出力の上限に達すると、本文が空のまま終わる
        stream.begin(f"Output limit reached while thinking ({res.get('eval_count')} tokens); "
                     "retrying without thinking", part, total)
        res = chat(host, model, system, user, stream, think=False)
        text = res["message"]["content"].strip()
    if not text:
        raise RuntimeError(
            f"the review is empty (done_reason={res.get('done_reason')}, "
            f"prompt_tokens={res.get('prompt_eval_count')}, output_tokens={res.get('eval_count')})")
    return text, res


def review(host, model, pr, diff, stream=None):
    """PR をレビューする。戻り値: (レビュー本文, 実行情報の HTML コメント, パートごとのレビュー or None)"""
    stream = stream or StreamWriter()
    files = pr.get("files", [])
    parts = make_parts(diff)
    t0, prompt_tokens, output_tokens = time.time(), 0, 0

    def tally(res):
        nonlocal prompt_tokens, output_tokens
        prompt_tokens += res.get("prompt_eval_count") or 0
        output_tokens += res.get("eval_count") or 0

    if len(parts) == 1:
        text, res = ask(host, model, SYSTEM, build_prompt(pr, files, diff), stream,
                        f"Reviewing the diff ({len(diff):,} chars)")
        tally(res)
        part_reviews = None
    else:
        part_reviews = []
        n = len(parts)
        for i, (paths, part_diff) in enumerate(parts, 1):
            shown = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
            note = (f"## このレビューの範囲\n"
                    f"この PR は大きいため、ファイルごとに {n} 個のパートに分けてレビューしています。これはパート {i}/{n} です。\n"
                    f"下の差分({', '.join(paths)})だけをレビューしてください。ほかのファイルは別のパートでレビューします。\n"
                    f"「概要」には、このパートの変更について書いてください。\n\n")
            text, res = ask(host, model, SYSTEM, build_prompt(pr, files, part_diff, note), stream,
                            f"Part {i}/{n}: {shown} ({len(part_diff):,} chars)", i, n)
            tally(res)
            part_reviews.append((paths, text))
        merged_input = pr_header(pr, files) + "## 各パートのレビュー\n\n" + "\n\n".join(
            f"### パート {i}/{n}({', '.join(paths)})\n\n{t}" for i, (paths, t) in enumerate(part_reviews, 1))
        text, res = ask(host, model, SYSTEM_MERGE, merged_input, stream,
                        f"Merging {n} part reviews", "merge", n)
        tally(res)
    stream.finish()
    meta = (f"<!-- model: {model}, {time.time() - t0:.0f}s, "
            f"prompt_tokens: {prompt_tokens}, output_tokens: {output_tokens}"
            f"{f', parts: {len(parts)}' if len(parts) > 1 else ''} -->")
    return text, meta, part_reviews


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--pr-json", required=True)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stream", help="file to append the output to while it is generated")
    args = ap.parse_args()

    with open(args.pr_json, encoding="utf-8") as f:
        pr = json.load(f)
    with open(args.diff, encoding="utf-8", errors="replace") as f:
        diff = f.read()

    text, meta, part_reviews = review(args.host, args.model, pr, diff,
                                      StreamWriter(args.stream) if args.stream else None)
    if part_reviews:
        with open(os.path.join(os.path.dirname(os.path.abspath(args.out)), "review-parts.md"), "w",
                  encoding="utf-8") as f:
            for i, (paths, t) in enumerate(part_reviews, 1):
                f.write(f"# Part {i}/{len(part_reviews)}: {', '.join(paths)}\n\n{t}\n\n")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(meta + "\n\n" + text + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
