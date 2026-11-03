"""MCP サーバ(mcp_repo_server.py / mcp_check_server.py)が読める範囲を決める。

  python mcp_xxx_server.py <ソースのディレクトリ> [--scope teams/invtechno[,teams/other]]

--scope を付けると、リポジトリのうちその中だけを読める(モノレポで 1 チームだけをレビューするとき)。
パスは常にリポジトリ直下からの相対パスで受け取り、返す。範囲の外のパス(.. やシンボリックリンクで
抜けるものも含む)はエラーにする。path を省略した(".")ときは、範囲のフォルダを対象にする。
"""
import argparse
import os

from mcp.server.mcpserver.exceptions import ToolError

_ap = argparse.ArgumentParser()
_ap.add_argument("root", nargs="?", default=".")
_ap.add_argument("--scope", default="", help="comma-separated directories (relative to root) that may be read")
_args = _ap.parse_args()

ROOT = os.path.realpath(_args.root)
SCOPES = [os.path.realpath(os.path.join(ROOT, s.strip().strip("/"))) for s in _args.scope.split(",") if s.strip()]
for _s in SCOPES:
    if not (_s == ROOT or _s.startswith(ROOT + os.sep)) or not os.path.isdir(_s):
        raise SystemExit(f"--scope must be a directory in the repository: {_s}")
SCOPES = SCOPES or [ROOT]


def rel(p):
    return os.path.relpath(p, ROOT)


def scope_desc():
    return ", ".join(rel(s) + "/" for s in SCOPES) if SCOPES != [ROOT] else "the whole repository"


def inside(p):
    return any(p == s or p.startswith(s + os.sep) for s in SCOPES)


def bases(path):
    """path(リポジトリ直下からの相対パス)を、調べる対象の絶対パスのリストにする。

    "." や "" や "/" は範囲のフォルダすべて。範囲を含む上のフォルダ(例 "teams")なら、その中の範囲だけ。
    """
    p = os.path.realpath(os.path.join(ROOT, path.strip().lstrip("/") or "."))
    if inside(p):
        if not os.path.exists(p):
            raise ToolError(f"not found: {path}")
        return [p]
    below = [s for s in SCOPES if s.startswith(p + os.sep)]
    if below:
        return below
    raise ToolError(f"outside the readable scope: {path} (readable: {scope_desc()})")


def resolve(path):
    """1 つのファイルかフォルダのパス。範囲の外ならエラー。"""
    found = bases(path)
    if len(found) != 1:
        raise ToolError(f"{path} contains several readable directories: {', '.join(rel(b) for b in found)}")
    return found[0]
