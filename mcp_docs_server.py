"""Python・Django・HTML の公式資料を検索して返す MCP サーバ(stdio)。

  python mcp_docs_server.py <資料のディレクトリ>...

資料は fetch-docs が窓口サーバで /work/$USER/llm/docs に取得したもの(計算ノードではネットワークを使わない):
  python-<版>/   Python の公式ドキュメント(テキスト版)
  django-<版>/   Django の公式ドキュメント(reST)
  mdn-html/      MDN の HTML の項目(Markdown)
どの版を使うかは review-pr が、レビューするリポジトリの pyproject.toml などから選んで引数で渡す。
"""
import math
import os
import re
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_lines import numbered

DIRS = [os.path.realpath(d) for d in sys.argv[1:]]
BASE = os.path.dirname(DIRS[0]) if DIRS else "."
MAX_LINES = 300
MAX_CHARS = 40_000       # 1 回の結果の文字数の上限(約 1 万トークン)
MAX_RESULTS = 10
UNDERLINE = re.compile(r"^([=\-~*^\"#+'`])\1{2,}\s*$")

mcp = MCPServer("docs")


def source_of(d):
    """ディレクトリ名から資料の種類(python / django / html)を決める。"""
    name = os.path.basename(d)
    return "html" if name.startswith("mdn") else name.split("-")[0]


def sections(lines):
    """見出しで区切った (開始行, 見出し) のリストを返す。reST(下線)と Markdown(#)に対応。"""
    out = []
    for i, line in enumerate(lines):
        if line.startswith("#") and line.lstrip("#").startswith(" "):
            out.append((i, line.lstrip("#").strip()))
        elif (i + 1 < len(lines) and line.strip() and UNDERLINE.match(lines[i + 1])
              and len(lines[i + 1].strip()) >= len(line.strip()) - 2):
            out.append((i, line.strip()))
    return out or [(0, lines[0].strip() if lines else "")]


def load():
    """資料を読み込み、見出しごとの節にする: (種類, 相対パス, 開始行, 見出し, 本文の小文字)"""
    index = []
    for d in DIRS:
        src = source_of(d)
        for root, _, files in os.walk(d):
            for f in sorted(files):
                if not f.endswith((".txt", ".md", ".rst")):
                    continue
                p = os.path.join(root, f)
                with open(p, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
                relp = os.path.relpath(p, BASE)
                # MDN は前書き(--- title: ... ---)の title を見出しにする
                title0 = next((l[6:].strip().strip('"') for l in lines[:10] if l.startswith("title:")), None)
                secs = sections(lines)
                for k, (start, title) in enumerate(secs):
                    end = secs[k + 1][0] if k + 1 < len(secs) else len(lines)
                    body = "\n".join(lines[start:end])
                    index.append((src, relp, start + 1, title0 if (title0 and k == 0) else title, body.lower()))
    return index


INDEX = load()


# 検索で無視する語。モデルは "Django CSRF cookie set on which response ..." のような文で検索することがあり、
# 機能語まで「すべて含む」を求めると何も見つからなかった(pbl-app-2026 #49)
STOPWORDS = {
    "a", "an", "the", "on", "in", "at", "of", "to", "for", "from", "by", "with", "and", "or", "not", "no",
    "is", "are", "be", "been", "was", "were", "do", "does", "did", "can", "could", "should", "would", "will",
    "how", "what", "which", "when", "where", "why", "who", "whether", "if", "then", "than", "that", "this",
    "these", "those", "it", "its", "as", "into", "about", "via", "vs", "use", "used", "using",
    "docs", "documentation", "python", "django", "html", "mdn",
}


@mcp.tool()
def search_docs(query: str, source: str = "all") -> str:
    """Search the official documentation of Python, Django and HTML (MDN) for the versions this project uses.

    query: a few keywords, e.g. "ForeignKey on_delete", "csrf cookie get_token", "dialog element".
    Common words are ignored. Sections with the most (and rarest) keywords come first.
    source: "python", "django", "html" or "all".
    Each result gives the path and the line to pass to read_doc as start_line.
    """
    words = re.findall(r"[\w.<>/-]+", query.lower())
    terms = list(dict.fromkeys(w for w in words if w not in STOPWORDS and len(w) > 1))
    if not terms:
        raise ToolError("no keywords in the query (common words are ignored)")
    if source != "all" and source not in {s for s, *_ in INDEX}:
        raise ToolError(f"unknown source: {source} (available: {', '.join(sorted({s for s, *_ in INDEX}))}, all)")
    sections = [x for x in INDEX if source == "all" or x[0] == source]
    hits = [[body.count(t) for t in terms] for _, _, _, _, body in sections]
    # 語の珍しさ(IDF): 多くの節に出る語ほど軽くする
    df = [sum(1 for h in hits if h[k]) for k in range(len(terms))]
    idf = [math.log((len(sections) + 1) / (d + 1)) + 0.1 for d in df]
    scored = []
    for (src, path, line, title, body), h in zip(sections, hits, strict=True):
        matched = [k for k in range(len(terms)) if h[k]]
        if not matched:
            continue
        tl, pl = title.lower(), path.lower()
        score = sum(idf[k] * (1 + math.log(h[k])) + 3 * idf[k] * (terms[k] in tl) + idf[k] * (terms[k] in pl)
                    for k in matched) - len(body) / 50000
        if "/releases/" in path or "/whatsnew/" in path:
            score *= 0.5                                   # 変更履歴より、今の動作の説明を先に出す
        scored.append((len(matched), score, src, path, line, title, body, matched))
    if not scored:
        return f"No sections contain any of: {', '.join(terms)}. Try other keywords."
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    if scored[0][0] < len(terms):
        out.append(f"No section contains all of: {', '.join(terms)}. Showing the closest matches.")
    for n, _, src, path, line, title, body, matched in scored[:MAX_RESULTS]:
        rare = max(matched, key=lambda k: idf[k])          # いちばん珍しい語の周りを抜き出す
        pos = body.find(terms[rare])
        snippet = " ".join(body[max(pos - 80, 0):pos + 160].split())
        got = "" if n == len(terms) else f" (matched {n}/{len(terms)}: {', '.join(terms[k] for k in matched)})"
        out.append(f"{path}:{line}  [{src}] {title}{got}\n"
                   f"    read_doc(\"{path}\", start_line={line})\n    ...{snippet}...")
    return "\n".join(out)


@mcp.tool()
def read_doc(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read a documentation file found by search_docs, with line numbers.

    path: as shown by search_docs (e.g. "django-6.1.2/ref/models/fields.txt").
    start_line / end_line: 1-based, inclusive; end_line 0 means up to 300 lines from start_line.
    """
    p = os.path.realpath(os.path.join(BASE, path))
    if not any(p.startswith(d + os.sep) for d in DIRS) or not os.path.isfile(p):
        raise ToolError(f"no such document: {path}")
    with open(p, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return numbered(path, lines, start_line, end_line, MAX_LINES, MAX_CHARS)


if __name__ == "__main__":
    mcp.run()
