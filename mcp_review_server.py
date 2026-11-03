"""このレビュー CLI を、ほかの Claude Code などから MCP のツールとして使えるようにするサーバ(stdio)。

窓口サーバ(ydev)で review-mcp から起動する。ポートは開けないので、ほかのマシンからは ssh 越しに起動する:
  claude mcp add llm-review -- /work/$USER/mikawa-pbl-localllm-review/review-mcp
  claude mcp add llm-review -- ssh ydev /work/$USER/mikawa-pbl-localllm-review/review-mcp

レビューは 5〜10 分かかるので、start_review で依頼して run_id を受け取り、get_review で結果を取りに来る。
中では review-pr --no-wait を呼ぶだけで、ジョブの投入やサーバへの依頼の仕方は review-pr と同じ。
PR へのコメントの投稿はしない(外に出る操作なので、呼び出し側が利用者に確かめてから gh で行う)。
"""
import json
import os
import re
import subprocess
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

ROOT = os.path.dirname(os.path.realpath(__file__))
RUNS = os.path.join(ROOT, "runs")
MAX_WAIT = 120                    # get_review で待つ秒数の上限(呼び出し側のツールのタイムアウトより短く)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
NAME_RE = re.compile(r"^[\w.:/-]+$")         # モデル名や --scope のフォルダ

mcp = MCPServer("llm-review", instructions=(
    "Reviews GitHub pull requests with a local LLM on the university HPC GPUs (TUT iEduq / Eduq). "
    "Call start_review, then get_review with the run_id until the status is done or failed "
    "(a review usually takes 5-10 minutes; get_review can wait up to 120 seconds per call). "
    "Reviews are written in Japanese and may contain mistakes: check them before relying on them. "
    "This server never posts to GitHub; if the user wants the review posted, ask them first."))


def run(cmd, timeout=120):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return r.returncode, ANSI.sub("", r.stdout), ANSI.sub("", r.stderr)


def run_dir(run_id):
    """run_id(runs/ からの相対パス)を実行ディレクトリにする。runs/ の外はエラー。"""
    p = os.path.realpath(os.path.join(RUNS, run_id))
    if not p.startswith(RUNS + os.sep) or not os.path.isfile(os.path.join(p, "pr.json")):
        raise ToolError(f"no such review: {run_id} (see list_reviews)")
    return p


def read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def job_state(job):
    """PBS のジョブの状態(Q / R / E / H …)。もう qstat に出なければ ""(終わっている)。"""
    code, out, _ = run(["qstat", "-f", job], timeout=30)
    m = re.search(r"job_state = (\w)", out) if code == 0 else None
    return m.group(1) if m else ""


def status_of(d):
    """実行ディレクトリから状態を調べる。戻り値: dict(status, detail, ...)"""
    info = {}
    review = os.path.join(d, "review.md")
    if os.path.exists(review) and os.path.getsize(review) > 0:
        info["status"] = "done"
        return info
    # 生成の段階(review.py が書く): 段階 トークン数 パート 全パート数
    stat = read(os.path.join(d, "stream.txt.stat")).split()
    if stat:
        info["phase"] = stat[0]
        info["output_tokens"] = int(stat[1]) if stat[1:2] and stat[1].isdigit() else 0
        if stat[2:3] and stat[2] != "-":
            info["part"] = f"{stat[2]}/{stat[3]}" if stat[2] != "merge" else "merging parts"
    progress = [l for l in read(os.path.join(d, "progress")).splitlines() if l.strip()]
    if progress:
        info["last_log_line"] = progress[-1]
    srv = read(os.path.join(d, "status")).strip()        # レビューサーバに依頼したとき
    job = read(os.path.join(d, "job")).strip()            # 新しいジョブで実行したとき
    if srv:
        word, _, reason = srv.partition(" ")
        if word in ("failed", "rejected"):
            info.update(status="failed", detail=srv,
                        hint="rejected: the review server had too little time left; start again with new_job=true"
                        if word == "rejected" else "")
        elif word == "done":                              # review.md の書き出しを待っている
            info["status"] = "running"
        else:
            info["status"] = "queued" if word == "queued" else "running"
        return info
    if job:
        st = job_state(job)
        info["job"] = job.split(".")[0]
        if st in ("Q", "W"):
            info["status"] = "queued"
        elif st in ("R", "E"):
            info["status"] = "running"
        elif st == "H":
            info.update(status="failed", detail="the job was held; the node may be unhealthy (try another queue)")
        else:
            log = read(os.path.join(d, "job.log"))
            info.update(status="failed", detail="the job ended without a review",
                        log_tail="\n".join(log.splitlines()[-15:]))
        return info
    info.update(status="unknown", detail="no job or server request was recorded for this run")
    return info


def summary(d):
    pr = json.loads(read(os.path.join(d, "pr.json"), "{}") or "{}")
    rel = os.path.relpath(d, RUNS)
    repo = rel.split("/")[0].replace("_", "/", 1)
    return {"run_id": rel, "pr": f"{repo}#{pr.get('number', '?')}", "title": pr.get("title", "")}


@mcp.tool()
def start_review(repo: str, pr_number: int, model: str = "", queue: str = "", use_tools: bool = False,
                 scope: str = "", new_job: bool = False) -> str:
    """Start a review of a GitHub pull request on the HPC GPUs. Returns a run_id for get_review.

    repo: "owner/repo". pr_number: the PR number.
    model: Ollama model (default flint:40b). queue: "iEduq" (NVIDIA A100) or "Eduq" (AMD MI210);
    empty lets review-pr choose (a running review server first, else iEduq).
    use_tools: let the model read the repository, lint and search Python/Django/HTML docs through MCP
    (slower, often more accurate). scope: with use_tools, limit reading to this directory
    (e.g. "teams/invtechno" in a monorepo).
    new_job: always submit a new job even if a review server is running.
    """
    if not REPO_RE.match(repo):
        raise ToolError("repo must look like owner/repo")
    if pr_number <= 0:
        raise ToolError("pr_number must be positive")
    cmd = [os.path.join(ROOT, "review-pr"), "--no-wait", "--quiet"]
    if model:
        if not NAME_RE.match(model):
            raise ToolError(f"invalid model name: {model}")
        cmd += ["-m", model]
    if queue:
        if queue not in ("iEduq", "Eduq"):
            raise ToolError("queue must be iEduq or Eduq")
        cmd += ["-q", queue]
    if use_tools or scope:
        cmd.append("--mcp")
    if scope:
        if not NAME_RE.match(scope) or ".." in scope:
            raise ToolError(f"invalid scope: {scope}")
        cmd += ["--scope", scope]
    if new_job:
        cmd.append("--new-job")
    cmd += [repo, str(pr_number)]
    code, out, err = run(cmd, timeout=600)    # --mcp ではソースと uv の環境を用意するので時間がかかる
    m = re.search(r"Output: (\S+)", err)
    if code != 0 or not m:
        raise ToolError(f"review-pr failed (exit {code}):\n" + "\n".join((err or out).splitlines()[-15:]))
    d = m.group(1)
    lines = [l for l in err.splitlines() if l.strip() and not l.startswith("Result will be written")]
    return json.dumps({**summary(d), "status": "queued", "log": lines[-6:],
                       "next": "call get_review with this run_id (it can wait up to 120 seconds)"},
                      ensure_ascii=False, indent=1)


@mcp.tool()
def get_review(run_id: str, wait_seconds: int = 0) -> str:
    """Get the status of a review, and the review itself (Markdown, in Japanese) when it is done.

    run_id: from start_review or list_reviews.
    wait_seconds: wait up to this many seconds (max 120) for the review to finish before answering.
    Status is queued, running (with phase and output tokens), done (with the review) or failed (with the reason).
    """
    d = run_dir(run_id)
    deadline = time.time() + max(0, min(wait_seconds, MAX_WAIT))
    while True:
        info = status_of(d)
        if info["status"] in ("done", "failed", "unknown") or time.time() >= deadline:
            break
        time.sleep(min(10, max(deadline - time.time(), 0)))
    info = {**summary(d), **info}
    info["elapsed_seconds"] = int(time.time() - os.path.getmtime(os.path.join(d, "pr.json")))
    if info["status"] == "done":
        text = read(os.path.join(d, "review.md"))
        meta = re.match(r"<!--(.*?)-->\s*", text)
        if meta:
            info["run_info"] = meta.group(1).strip()      # モデル・時間・トークン数・ツールの回数
            text = text[meta.end():]
        info.pop("elapsed_seconds")
        return json.dumps(info, ensure_ascii=False, indent=1) + "\n\n" + text
    return json.dumps(info, ensure_ascii=False, indent=1)


@mcp.tool()
def list_reviews(limit: int = 10) -> str:
    """List recent reviews (newest first) with their run_id and status."""
    dirs = [os.path.dirname(p) for p in
            (os.path.join(RUNS, r, d, "pr.json") for r in os.listdir(RUNS) if os.path.isdir(os.path.join(RUNS, r))
             for d in os.listdir(os.path.join(RUNS, r)))
            if os.path.exists(p)] if os.path.isdir(RUNS) else []
    dirs.sort(key=lambda d: os.path.getmtime(os.path.join(d, "pr.json")), reverse=True)
    out = []
    for d in dirs[:max(1, min(limit, 50))]:
        s = status_of(d)
        out.append({**summary(d), "status": s["status"]})
    return json.dumps(out, ensure_ascii=False, indent=1) if out else "No reviews yet."


@mcp.tool()
def cancel_review(run_id: str) -> str:
    """Cancel a queued or running review (deletes the job, or asks the review server to stop it)."""
    d = run_dir(run_id)
    s = status_of(d)
    if s["status"] not in ("queued", "running"):
        return f"Nothing to cancel: the review is {s['status']}."
    job = read(os.path.join(d, "job")).strip()
    if job:
        code, _, err = run(["qdel", job], timeout=30)
        return f"Deleted job {job.split('.')[0]}." if code == 0 else f"qdel failed: {err.strip()}"
    open(os.path.join(d, "cancel"), "w").close()        # サーバは cancel ファイルを見て止める
    return "Asked the review server to stop this review."


@mcp.tool()
def list_pull_requests(repo: str, state: str = "open") -> str:
    """List pull requests of a GitHub repository (number, title, author, size), to pick one to review.

    state: "open", "merged", "closed" or "all".
    """
    if not REPO_RE.match(repo):
        raise ToolError("repo must look like owner/repo")
    if state not in ("open", "merged", "closed", "all"):
        raise ToolError("state must be open, merged, closed or all")
    code, out, err = run(["gh", "pr", "list", "-R", repo, "--state", state, "--limit", "30",
                          "--json", "number,title,author,isDraft,additions,deletions,updatedAt"], timeout=60)
    if code != 0:
        raise ToolError(f"gh failed: {err.strip()}")
    prs = [{"number": p["number"], "title": p["title"], "author": p["author"]["login"],
            "size": f"+{p['additions']}/-{p['deletions']}", "draft": p["isDraft"], "updated": p["updatedAt"]}
           for p in json.loads(out)]
    return json.dumps(prs, ensure_ascii=False, indent=1) if prs else f"No {state} pull requests."


@mcp.tool()
def server_status() -> str:
    """Show the review servers (resident GPU jobs that keep the model loaded) and their queues.

    When one is running, start_review sends reviews to it and they start within seconds.
    """
    code, out, err = run([os.path.join(ROOT, "review-server"), "status"], timeout=60)
    return (out + err).strip() or f"review-server status exited with {code}"


if __name__ == "__main__":
    mcp.run()
