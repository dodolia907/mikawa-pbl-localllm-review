"""PR を適用した後のリポジトリのファイルを、読み取り専用で返す MCP サーバ(stdio)。

review.py が --mcp-config の設定に従って計算ノードで起動し、ローカル LLM がツールとして呼ぶ:
  python mcp_repo_server.py <ソースのディレクトリ> [--scope teams/invtechno]

ソースは review-pr が窓口サーバで gh を使って取得し、実行ディレクトリの src/ に置く
(GitHub のトークンは計算ノードに渡さない)。ディレクトリの外は読めない。
--scope を付けると、その中だけを読める(mcp_scope.py)。
公式の mcp SDK(Python 3.10 以上)が必要なので、uv で用意した環境で動かす(jobs/run_review.sh)。
"""
import fnmatch
import os
import re

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_lines import numbered
from mcp_scope import bases, inside, rel, resolve

MAX_LINES = 400          # read_file で 1 回に返す行数の上限
MAX_CHARS = 40_000       # 1 回の結果の文字数の上限(約 1 万トークン)
MAX_LIST = 300           # list_files で返すパスの上限
MAX_MATCHES = 100        # search で返す行の上限
SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", "__pycache__", ".venv", "venv"}

mcp = MCPServer("repo")


def glob_match(path, pattern):
    """モデルは "**/*.py" や "src/*.py" のような書き方もするので、それも受け付ける。"""
    while pattern.startswith("**/"):
        pattern = pattern[3:]
    pattern = pattern or "*"
    return fnmatch.fnmatch(path if "/" in pattern else os.path.basename(path), pattern)


def walk(base):
    for d, dirs, files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for f in sorted(files):
            p = os.path.join(d, f)
            if inside(os.path.realpath(p)):    # 範囲の外を指すシンボリックリンクは読まない
                yield p


@mcp.tool()
def list_files(path: str = ".", pattern: str = "*") -> str:
    """List files under a directory of the repository (after the PR is applied), recursively.

    path: directory relative to the repository root.
    pattern: glob on the file name (e.g. "*.rs"), or on the path if it contains "/" (e.g. "teams/*/views.py").
    """
    out = []
    for base in bases(path):
        if not os.path.isdir(base):
            raise ToolError(f"not a directory: {path}")
        # パターンに "/" があれば、path からの相対パスに照合する(例 path="teams/x", pattern="static/**")
        out += [rel(f) for f in walk(base) if glob_match(os.path.relpath(f, base), pattern)]
    more = f"\n... and {len(out) - MAX_LIST} more" if len(out) > MAX_LIST else ""
    return "\n".join(out[:MAX_LIST]) + more if out else "(no files)"


@mcp.tool()
def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read a file of the repository (after the PR is applied), with line numbers.

    path: file path relative to the repository root. start_line / end_line: 1-based, inclusive;
    end_line 0 means up to 400 lines from start_line.
    """
    p = resolve(path)
    if not os.path.isfile(p):
        raise ToolError(f"not a file: {path}")
    with open(p, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return numbered(rel(p), lines, start_line, end_line, MAX_LINES, MAX_CHARS)


@mcp.tool()
def search(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
    """Search the repository (after the PR is applied) for a regular expression, like grep -n.

    pattern: Python regular expression. path: directory or file to search.
    file_pattern: glob on the file name (e.g. "*.py").
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"invalid regular expression: {e}") from e
    out = []
    files = [(b, f) for b in bases(path) for f in ([b] if os.path.isfile(b) else walk(b))]
    for base, f in files:
        if f != base and not glob_match(os.path.relpath(f, base), file_pattern):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append(f"{rel(f)}:{i}: {line.rstrip()[:300]}")
                        if len(out) >= MAX_MATCHES:
                            return "\n".join(out) + f"\n... (stopped at {MAX_MATCHES} matches)"
        except (UnicodeDecodeError, OSError):
            continue                       # バイナリなどは飛ばす
    return "\n".join(out) if out else "(no matches)"


if __name__ == "__main__":
    mcp.run()
