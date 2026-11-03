"""MCP サーバ(mcp_repo_server.py / mcp_docs_server.py)が、ファイルの一部を行番号付きで返すための共通の処理。"""
from mcp.server.mcpserver.exceptions import ToolError


def numbered(name, lines, start_line, end_line, max_lines, max_chars):
    """lines の start_line〜end_line 行目(1 から数える。end_line が 0 なら max_lines 行まで)を、
    「行番号: 中身」の形で返す。max_chars を超える前の、最後まで収まった行で止める。

    続きがあれば、次に読み始める行(start_line=…)を添える。範囲がおかしいときは、何が悪いかをエラーで返す。
    """
    n = len(lines)
    if n == 0:
        return f"{name} (empty file)"
    start = max(start_line, 1)
    if start > n:
        raise ToolError(f"start_line {start_line} is past the end of {name} ({n} lines)")
    if 0 < end_line < start:
        raise ToolError(f"end_line ({end_line}) must be 0 or at least start_line ({start})")
    last = min(end_line if end_line > 0 else n, start + max_lines - 1, n)
    out, size, end = [], 0, start - 1
    for i in range(start, last + 1):
        row = f"{i}: {lines[i - 1]}"
        if out and size + len(row) + 1 > max_chars:     # 1 行目だけは長くても返す(進めなくならないように)
            break
        out.append(row[:max_chars])
        size += len(row) + 1
        end = i
    rest = f"\n... ({n} lines in total; read more with start_line={end + 1})" if end < n else ""
    return f"{name} (lines {start}-{end} of {n})\n" + "\n".join(out) + rest
