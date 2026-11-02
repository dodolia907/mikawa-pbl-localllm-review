"""Ollama 上のローカルLLMに PR の差分を渡し、レビューを Markdown で返す。標準ライブラリのみ使用。

単発で使う場合(submit_review.sh から呼ばれる):
  python3 review.py --host URL --model M --pr-json pr.json --diff pr.diff --out review.md
watch.py からは review_pr() を import して使う。
"""
import argparse
import json
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


def chat(host, model, user, timeout=3600):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(f"{host}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def review_pr(host, model, pr, files, diff, note=""):
    """レビュー本文と、実行情報(HTMLコメント)を返す。"""
    prompt, truncated = build_prompt(pr, files, diff, note)
    t0 = time.time()
    res = chat(host, model, prompt)
    meta = (f"<!-- model: {model}, {time.time() - t0:.0f}s, "
            f"prompt_tokens: {res.get('prompt_eval_count')}, "
            f"output_tokens: {res.get('eval_count')}"
            f"{', diff truncated' if truncated else ''} -->")
    text = res["message"]["content"].strip()
    if not text:
        # 考える過程(thinking)だけでコンテキストを使い切ると、本文が空のまま終わる
        raise RuntimeError(
            f"レビュー本文が空です(done_reason={res.get('done_reason')}, "
            f"prompt_tokens={res.get('prompt_eval_count')}, output_tokens={res.get('eval_count')})。"
            "コンテキスト長(OLLAMA_CONTEXT_LENGTH)が足りない可能性があります")
    return text, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--pr-json", required=True)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.pr_json, encoding="utf-8") as f:
        pr = json.load(f)
    with open(args.diff, encoding="utf-8", errors="replace") as f:
        diff = f.read()

    text, meta = review_pr(args.host, args.model, pr, pr.get("files", []), diff)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(meta + "\n\n" + text + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
