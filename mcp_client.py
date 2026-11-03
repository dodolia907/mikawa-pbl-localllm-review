"""MCP サーバにつなぎ、そのツールを Ollama のツール呼び出しから使えるようにする(公式 mcp SDK を使う)。

review.py は同期的に Ollama を呼ぶので、MCP のやりとり(asyncio)は別スレッドのイベントループで行い、
同期的な call() で結果を待つ。

設定ファイルは Claude Code の .mcp.json と同じ形:
  {"mcpServers": {
     "repo": {"command": "${PYTHON}", "args": ["${ROOT}/mcp_repo_server.py", "${RUN_DIR}/src"]},
     "docs": {"type": "http", "url": "http://..."}}}
文字列の中の ${変数} は環境変数で置き換える。PYTHON はこのプログラムを動かしている Python
(mcp SDK が入った uv の環境)になる。
"""
import asyncio
import contextlib
import json
import os
import re
import sys
import threading

from mcp import Client, StdioServerParameters

MAX_RESULT_CHARS = 20_000     # ツールの結果をモデルに渡すときの文字数の上限


def expand(v):
    if isinstance(v, str):
        return os.path.expandvars(v)
    if isinstance(v, list):
        return [expand(x) for x in v]
    if isinstance(v, dict):
        return {k: expand(x) for k, x in v.items()}
    return v


def load_config(path):
    """戻り値: (サーバの設定, 読める範囲のリスト)。範囲は review-pr --scope が書く。"""
    os.environ.setdefault("PYTHON", sys.executable)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return expand(cfg.get("mcpServers", {})), cfg.get("scope") or []


class MCPTools:
    """設定ファイルの MCP サーバをすべて起動し、ツールを「サーバ名__ツール名」でまとめて扱う。"""

    def __init__(self, config_path, log=sys.stderr):
        self.servers, self.scope = load_config(config_path)
        self.log = log
        self.clients = {}             # サーバ名 -> Client
        self.tools = {}               # Ollama に見せる名前 -> (サーバ名, ツール名)
        self.schemas = []             # Ollama の tools に渡す定義
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.stop = None
        self.main = asyncio.run_coroutine_threadsafe(self._main(), self.loop)
        self.ready.wait()
        if self.error:
            raise self.error

    async def _main(self):
        # Client は開いたタスクの中で閉じる必要がある(anyio の制約)ので、1 つのタスクで開いて閉じる
        self.stop = asyncio.Event()
        try:
            async with contextlib.AsyncExitStack() as stack:
                for name, cfg in self.servers.items():
                    if cfg.get("url"):
                        target = cfg["url"]
                    else:
                        target = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []),
                                                       env=cfg.get("env"), cwd=cfg.get("cwd"))
                    client = await stack.enter_async_context(Client(target, read_timeout_seconds=120))
                    self.clients[name] = client
                    listed = (await client.list_tools()).tools
                    for t in listed:
                        full = re.sub(r"[^A-Za-z0-9_-]", "_", f"{name}__{t.name}")
                        self.tools[full] = (name, t.name)
                        self.schemas.append({"type": "function", "function": {
                            "name": full, "description": t.description or "",
                            "parameters": t.input_schema or {"type": "object", "properties": {}}}})
                    print(f"MCP server {name}: {len(listed)} tools", file=self.log)
                self.ready.set()
                await self.stop.wait()
        except BaseException as e:   # 起動に失敗したら、呼び出し側(__init__)で例外にする
            self.error = e if isinstance(e, Exception) else RuntimeError(str(e))
            self.ready.set()

    async def _call(self, server, tool, args):
        return await self.clients[server].call_tool(tool, args)

    def call(self, full_name, args):
        """ツールを呼び、モデルに渡す文字列を返す。失敗も文字列で返し、モデルに伝える。"""
        if full_name not in self.tools:
            return f"error: unknown tool {full_name}"
        server, tool = self.tools[full_name]
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                return f"error: arguments are not valid JSON: {args}"
        try:
            res = asyncio.run_coroutine_threadsafe(self._call(server, tool, args or {}), self.loop).result(180)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
        parts = []
        for c in res.content:
            parts.append(c.text if getattr(c, "type", "") == "text" else f"[{getattr(c, 'type', 'content')}]")
        if not parts and res.structured_content is not None:
            parts.append(json.dumps(res.structured_content, ensure_ascii=False))
        text = "\n".join(parts)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n... (truncated; {len(text):,} chars in total)"
        return ("error: " if res.is_error else "") + text

    def close(self):
        if self.stop is not None:
            self.loop.call_soon_threadsafe(self.stop.set)
            with contextlib.suppress(Exception):
                self.main.result(30)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
