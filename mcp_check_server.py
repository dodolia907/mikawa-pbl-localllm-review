"""PR を適用した後のリポジトリを静的に検査する MCP サーバ(stdio)。PR のコードは実行しない。

  python mcp_check_server.py <ソースのディレクトリ> [--scope teams/invtechno]

ツール:
  check_python   ruff で構文エラー・未定義の名前・使っていない import などを調べる
  check_template HTML と Django テンプレートの開きタグと閉じタグの対応を調べる

PR のコードを import したり実行したりすると、計算ノードで学生のコードが動き、共有の /home にある
認証情報などに触れられてしまうので、ここではファイルを読んで調べることだけをする。
ruff は Rust 製のリンタで、コードを実行しない。
"""
import html.parser
import os
import re
import shutil
import subprocess
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_scope import ROOT, bases, inside, rel

MAX_PROBLEMS = 100
SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", "__pycache__", ".venv", "venv"}

mcp = MCPServer("check")


def files_under(p, exts):
    if os.path.isfile(p):
        return [p]
    out = []
    for d, dirs, files in os.walk(p):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        out += [os.path.join(d, f) for f in sorted(files)
                if f.endswith(exts) and inside(os.path.realpath(os.path.join(d, f)))]
    return out


def ruff_command():
    exe = shutil.which("ruff") or os.path.join(os.path.dirname(sys.executable), "ruff")
    if os.path.exists(exe):
        return [exe]
    return [sys.executable, "-m", "ruff"]


def target_version():
    """pyproject.toml の requires-python(例 ">=3.14.8,<3.15")から ruff の --target-version を決める。"""
    try:
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            m = re.search(r'requires-python\s*=\s*"[^"]*?(\d+)\.(\d+)', f.read())
        return f"py{m.group(1)}{m.group(2)}" if m else None
    except OSError:
        return None


# ruff の規則: E9 構文エラー、F pyflakes(未定義の名前、使っていない import・変数など)、
# E7 比較の誤りなど、B bugbear(可変の既定値、例外の握りつぶしなど)。書式や好みの規則は入れない
RUFF_RULES = "E9,E7,F,B"


@mcp.tool()
def check_python(path: str = ".") -> str:
    """Lint Python files of the repository (after the PR) with ruff: syntax errors, undefined names,
    unused imports and variables, and similar likely bugs. The code is not executed.

    path: a .py file or a directory, relative to the repository root.
    Only rules that point at likely bugs are used (not style); the Python version comes from
    requires-python in pyproject.toml.
    """
    targets = bases(path)
    # 範囲の外を指すシンボリックリンクを ruff に読ませないよう、ファイルを列挙して渡す
    files = [rel(f) for b in targets for f in files_under(b, (".py", ".pyi"))]
    if not files:
        return f"No Python files under {path}."
    # --isolated: プロジェクトや利用者の ruff の設定を読まず、決めた規則だけで調べる
    tv = target_version()
    r = subprocess.run(ruff_command() + ["check", "--isolated", "--no-cache", "--exit-zero",
                                         "--output-format", "concise", "--select", RUFF_RULES,
                                         *(["--target-version", tv] if tv else []), "--", *files],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith(("Found ", "[*]", "No fixes"))]
    if r.returncode not in (0, 1) and not lines:
        raise ToolError(f"ruff failed: {r.stderr.strip()[:500]}")
    if not lines:
        return f"No problems found in {len(files)} file(s) (ruff {RUFF_RULES}{', ' + tv if tv else ''})."
    more = f"\n... and {len(lines) - MAX_PROBLEMS} more" if len(lines) > MAX_PROBLEMS else ""
    return f"{len(lines)} problems:\n" + "\n".join(lines[:MAX_PROBLEMS]) + more


# ---- HTML / Django テンプレート ----------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
        "param", "keygen"}
# 閉じタグを省略できる要素(省略されていても指摘しない)
OPTIONAL_END = {"p", "li", "dt", "dd", "td", "th", "tr", "thead", "tbody", "tfoot", "option", "optgroup",
                "colgroup", "caption", "rt", "rp", "html", "head", "body"}
DJ_BLOCK = {"if", "for", "block", "with", "comment", "verbatim", "autoescape", "filter", "spaceless",
            "ifchanged", "blocktrans", "blocktranslate", "localize", "localtime", "timezone", "cache",
            "partialdef"}
DJ_MIDDLE = {"elif": {"if"}, "else": {"if", "for", "ifchanged"}, "empty": {"for"},
             "plural": {"blocktrans", "blocktranslate"}}
DJ_TOKEN = re.compile(r"\{%-?\s*(.*?)\s*-?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def check_django(text):
    """Django テンプレートのブロックタグ({% if %} 〜 {% endif %} など)の対応を調べる。"""
    problems, stack = [], []
    # 独自のタグでも、同じファイルに end<名前> があればブロックタグとみなす
    block = DJ_BLOCK | {m.group(1) for m in re.finditer(r"\{%-?\s*end(\w+)", text)}
    raw = None                         # comment / verbatim の中は読み飛ばす
    for m in DJ_TOKEN.finditer(text):
        if m.group(1) is None:
            continue
        words = m.group(1).split()
        if not words:
            continue
        tag, ln = words[0], line_of(text, m.start())
        if raw:
            if tag == "end" + raw:
                stack.pop(); raw = None
            continue
        if tag.startswith("end") and tag[3:] in block:
            name = tag[3:]
            if stack and stack[-1][0] == name:
                stack.pop()
            elif any(s[0] == name for s in stack):
                while stack[-1][0] != name:
                    t, l = stack.pop()
                    problems.append(f"line {l}: {{% {t} %}} is not closed before {{% {tag} %}} (line {ln})")
                stack.pop()
            else:
                problems.append(f"line {ln}: {{% {tag} %}} has no matching {{% {name} %}}")
        elif tag in DJ_MIDDLE:
            if not stack or stack[-1][0] not in DJ_MIDDLE[tag]:
                problems.append(f"line {ln}: {{% {tag} %}} is outside "
                                f"{' / '.join('{% ' + t + ' %}' for t in sorted(DJ_MIDDLE[tag]))}")
        elif tag in block:
            stack.append((tag, ln))
            if tag in ("comment", "verbatim"):
                raw = tag
    problems += [f"line {l}: {{% {t} %}} is never closed" for t, l in stack]
    return problems


class TagChecker(html.parser.HTMLParser):
    """HTML の開きタグと閉じタグの対応を調べる(閉じタグを省略できる要素は大目に見る)。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        seen = set()
        for k, _ in attrs:
            if k in seen:
                self.problems.append(f"line {self.getpos()[0]}: <{tag}> has the attribute {k} twice")
            seen.add(k)
        self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        pass                               # <br /> など

    def handle_endtag(self, tag):
        ln = self.getpos()[0]
        if tag in VOID:
            return
        if not any(t == tag for t, _ in self.stack):
            self.problems.append(f"line {ln}: </{tag}> has no matching <{tag}>")
            return
        while self.stack[-1][0] != tag:
            t, l = self.stack.pop()
            if t not in OPTIONAL_END:
                self.problems.append(f"line {l}: <{t}> is not closed before </{tag}> (line {ln})")
        self.stack.pop()

    def finish(self):
        self.close()
        self.problems += [f"line {l}: <{t}> is never closed" for t, l in self.stack if t not in OPTIONAL_END]
        return self.problems


def blank_django(text):
    """Django のタグと変数を、行の位置を保ったまま空白に置き換える(HTML として読むため)。"""
    return DJ_TOKEN.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def check_one(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        text = f.read()
    problems = [f"[django] {x}" for x in check_django(text)]
    # <script> と <style> の中は HTML として読まない(HTMLParser が扱う)
    checker = TagChecker()
    try:
        checker.feed(blank_django(text))
        problems += [f"[html] {x}" for x in checker.finish()]
    except Exception as e:
        problems.append(f"[html] cannot parse: {e}")
    return [f"{rel(p)}: {x}" for x in problems]


@mcp.tool()
def check_template(path: str = ".") -> str:
    """Check HTML files and Django templates of the repository (after the PR): unbalanced Django block tags
    ({% if %} without {% endif %}, ...), unclosed or stray HTML tags, duplicated attributes.

    path: an .html file or a directory, relative to the repository root ("." for everything readable).
    Tags that are opened in one {% if %} branch and closed in another may be reported falsely; check the
    lines before reporting them.
    """
    files = [f for b in bases(path) for f in files_under(b, (".html", ".htm", ".tpl", ".jinja", ".j2"))]
    if not files:
        raise ToolError(f"no HTML files under {path}")
    problems = []
    for f in files:
        problems += check_one(f)
    if not problems:
        return f"No problems found in {len(files)} file(s)."
    more = f"\n... and {len(problems) - MAX_PROBLEMS} more" if len(problems) > MAX_PROBLEMS else ""
    return f"{len(problems)} problems in {len(files)} file(s):\n" + "\n".join(problems[:MAX_PROBLEMS]) + more


if __name__ == "__main__":
    mcp.run()
