"""Ollama 上のローカルLLMに PR の差分を渡し、レビューを Markdown で返す。標準ライブラリのみ使用。

ジョブ(jobs/review.pbs)の中で、起動済みの Ollama に対して実行される:
  python3 review.py --host URL --model M --pr-json pr.json --diff pr.diff --out review.md
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

# 差分が長すぎるとコンテキストに収まらないため、文字数で打ち切る(約 4 文字 = 1 トークン目安)。
MAX_DIFF_CHARS = 160_000


def split_diff(diff):
    """unified diff をファイル単位に分け、(パス, 本文) のリストを返す。"""
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    out = []
    for p in parts:
        m = re.match(r"diff --git a/(\S+) b/(\S+)", p)
        if m:
            out.append((m.group(2), p))
    return out


def filter_diff(diff, prefixes):
    """prefixes のいずれかで始まるファイルの差分だけを残す。prefixes が空なら全体を返す。"""
    if not prefixes:
        return diff
    return "".join(body for path, body in split_diff(diff) if path.startswith(tuple(prefixes)))


def build_prompt(pr, files, diff, note=""):
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (差分が長いため以降は省略)\n"
    file_list = "\n".join(f"- {f['path']} (+{f['additions']}/-{f['deletions']})" for f in files)
    prompt = (
        f"# PR #{pr['number']}: {pr['title']}\n"
        f"base: {pr['baseRefName']} <- head: {pr['headRefName']}\n\n"
        f"## 説明\n{pr.get('body') or '(なし)'}\n\n"
        f"## 変更ファイル\n{file_list}\n\n"
        f"{note}"
        f"## 差分\n```diff\n{diff}\n```\n"
    )
    return prompt, truncated


class StreamWriter:
    """生成途中の出力をファイルに書き足す。review-pr がこれを読んで画面に流す。

    - <path>: 考える過程(=== Thinking ===)と本文(=== Review ===)を、見出しで区切って書き足す
    - <path>.stat: 「段階 トークン数」の 1 行(reading / thinking / writing / done)
    path が None なら何もしない。画面に出す見出しは英語にしている。
    """

    def __init__(self, path=None):
        self.path = path
        self.f = open(path, "w", encoding="utf-8") if path else None
        self.section = None
        self.chunks = 0
        self.t0 = time.time()
        self.last_flush = self.t0
        self.stat("reading")

    def stat(self, phase):
        if not self.path:
            return
        tmp = self.path + ".stat.tmp"
        with open(tmp, "w") as f:
            f.write(f"{phase} {self.chunks}\n")
        os.replace(tmp, self.path + ".stat")   # 読む側が書きかけを見ないよう、置き換えで更新する

    def note(self, text):
        if self.f:
            self.f.write(text)
            self.f.flush()

    def write(self, kind, text):
        if not self.f or not text:
            return
        if kind != self.section:
            self.f.write(f"\n=== {'Thinking' if kind == 'thinking' else 'Review'} ===\n")
            self.section = kind
        self.f.write(text)
        self.chunks += 1                       # ストリームの 1 チャンクがおよそ 1 トークン
        now = time.time()
        if now - self.last_flush >= 0.3:       # 細かすぎる書き込みを避け、0.3 秒ごとに反映する
            self.f.flush()
            self.stat("thinking" if kind == "thinking" else "writing")
            self.last_flush = now

    def close(self, res, wait_s):
        if not self.f:
            return
        n, ns = res.get("eval_count") or self.chunks, res.get("eval_duration") or 0
        rate = f", {n / (ns / 1e9):.0f} tok/s" if ns else ""
        self.f.write(f"\n\n=== Done: {res.get('prompt_eval_count')} input tokens "
                     f"(read in {wait_s:.0f}s), {n} output tokens{rate}, "
                     f"{time.time() - self.t0:.0f}s total ===\n")
        self.f.close()
        self.chunks = n
        self.stat("done")


def chat(host, model, user, stream=None, timeout=3600):
    """Ollama の /api/chat をストリーミングで呼ぶ。

    届いた分を stream に書き足し、最後の応答(トークン数などを含む)に本文と考える過程をまとめて返す。
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "stream": True,
        "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(f"{host}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    stream = stream or StreamWriter()
    stream.note("=== Reading the diff (takes a few minutes for large diffs) ===\n")
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
    stream.close(res, first or 0)
    return res


def review_pr(host, model, pr, files, diff, note="", stream=None):
    """レビュー本文と、実行情報(HTMLコメント)を返す。"""
    prompt, truncated = build_prompt(pr, files, diff, note)
    t0 = time.time()
    res = chat(host, model, prompt, stream)
    meta = (f"<!-- model: {model}, {time.time() - t0:.0f}s, "
            f"prompt_tokens: {res.get('prompt_eval_count')}, "
            f"output_tokens: {res.get('eval_count')}"
            f"{', diff truncated' if truncated else ''} -->")
    text = res["message"]["content"].strip()
    if not text:
        # 考える過程(thinking)だけでコンテキストを使い切ると、本文が空のまま終わる
        raise RuntimeError(
            f"the review is empty (done_reason={res.get('done_reason')}, "
            f"prompt_tokens={res.get('prompt_eval_count')}, output_tokens={res.get('eval_count')}); "
            "the model may have used up the context (OLLAMA_CONTEXT_LENGTH) while thinking")
    return text, meta


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

    stream = StreamWriter(args.stream) if args.stream else None
    text, meta = review_pr(args.host, args.model, pr, pr.get("files", []), diff, stream=stream)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(meta + "\n\n" + text + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
