"""Python・Django・HTML の公式資料を検索して返す MCP サーバ(stdio)。

  python mcp_docs_server.py <資料のディレクトリ>...

資料は fetch-docs が窓口サーバで /work/$USER/llm/docs に取得したもの(計算ノードではネットワークを使わない):
  python-<版>/   Python の公式ドキュメント(テキスト版)
  django-<版>/   Django の公式ドキュメント(reST)
  mdn-html/      MDN の HTML の項目(Markdown)
どの版を使うかは review-pr が、レビューするリポジトリの pyproject.toml などから選んで引数で渡す。
"""
import os
import re
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DIRS = [os.path.realpath(d) for d in sys.argv[1:]]
BASE = os.path.dirname(DIRS[0]) if DIRS else "."
MAX_LINES = 300
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


@mcp.tool()
def search_docs(query: str, source: str = "all") -> str:
    """Search the official documentation of Python, Django and HTML (MDN) for the versions this project uses.

    query: keywords, e.g. "ForeignKey on_delete", "csrf_token", "dialog element", "zoneinfo".
    source: "python", "django", "html" or "all".
    Returns sections as path:line with a snippet; read them with read_doc.
    """
    terms = [t for t in re.findall(r"[\w.<>/-]+", query.lower()) if t]
    if not terms:
        raise ToolError("empty query")
    if source != "all" and source not in {s for s, *_ in INDEX}:
        raise ToolError(f"unknown source: {source} (available: {', '.join(sorted({s for s, *_ in INDEX}))}, all)")
    scored = []
    for src, path, line, title, body in INDEX:
        if source != "all" and src != source:
            continue
        hits = [body.count(t) for t in terms]
        if not all(hits):
            continue
        tl = title.lower()
        score = sum(min(h, 20) for h in hits) + 15 * sum(t in tl for t in terms) \
            + 5 * sum(t in path.lower() for t in terms) - len(body) / 20000
        scored.append((score, src, path, line, title, body))
    if not scored:
        return f"No sections contain all of: {', '.join(terms)}. Try fewer or different keywords."
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, src, path, line, title, body in scored[:MAX_RESULTS]:
        pos = body.find(terms[0])
        snippet = " ".join(body[max(pos - 80, 0):pos + 160].split())
        out.append(f"{path}:{line}  [{src}] {title}\n    ...{snippet}...")
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
    start = max(start_line, 1)
    end = min(end_line if end_line > 0 else start + MAX_LINES - 1, start + MAX_LINES - 1, len(lines))
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    rest = f"\n... ({len(lines)} lines in total; read more with start_line={end + 1})" if end < len(lines) else ""
    return f"{path} (lines {start}-{end} of {len(lines)})\n{body}{rest}"


if __name__ == "__main__":
    mcp.run()
