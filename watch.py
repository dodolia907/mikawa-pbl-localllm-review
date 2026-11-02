"""PR を定期的に確認し、対象チームの新しい PR(または新しい push)を自動でレビューする。

計算ノードのジョブ(jobs/watch.pbs)内で、起動済みの Ollama に対して実行する。
GitHub へのアクセスは gh CLI(~/.config/gh の認証情報)を使う。

  python3 watch.py --config config.json --host http://127.0.0.1:PORT --until <UNIX時刻>
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from review import filter_diff, review_pr

GH = os.environ.get("GH", os.path.expanduser("~/.local/bin/gh"))
MARKER = "<!-- localllm-review"
MAX_FAILURES = 3   # 同じコミットで失敗し続けた場合は諦める


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def gh(*args, parse=True):
    out = subprocess.run([GH, *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out) if parse else out


def list_open_prs(repo):
    return gh("pr", "list", "-R", repo, "--state", "open", "--limit", "200", "--json",
              "number,title,headRefOid,author,labels,isDraft,baseRefName")


def pr_files(repo, number):
    pages = gh("api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/files")
    return [{"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"]}
            for page in pages for f in page]


def is_candidate(pr, cfg):
    """ファイルを見なくても判定できる条件(下書き・base・作者・ラベル)。"""
    t = cfg.get("team", {})
    if pr["isDraft"] and cfg.get("skip_draft", True):
        return False
    if cfg.get("base_branches") and pr["baseRefName"] not in cfg["base_branches"]:
        return False
    if t.get("authors") and pr["author"]["login"] not in t["authors"]:
        return False
    if t.get("labels") and not {l["name"] for l in pr["labels"]} & set(t["labels"]):
        return False
    return True


def touches_team_paths(files, cfg):
    prefixes = cfg.get("team", {}).get("paths", [])
    return not prefixes or any(f["path"].startswith(tuple(prefixes)) for f in files)


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"reviewed": {}, "skipped": {}, "failures": {}}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def handle(pr, cfg, host, out_dir):
    """1 件の PR をレビューする。対象外なら None、レビューしたら保存先パスを返す。"""
    repo, n, sha = cfg["repo"], pr["number"], pr["headRefOid"]
    files = pr_files(repo, n)
    if not touches_team_paths(files, cfg):
        return None

    prefixes = cfg.get("team", {}).get("paths", []) if cfg.get("diff_team_paths_only", True) else []
    diff = filter_diff(gh("pr", "diff", str(n), "-R", repo, parse=False), prefixes)
    shown = [f for f in files if not prefixes or f["path"].startswith(tuple(prefixes))]
    note = (f"(モノレポのため、{', '.join(prefixes)} 以下の変更だけを示しています。"
            f"PR 全体の変更ファイルは {len(files)} 件です)\n\n") if prefixes and len(shown) < len(files) else ""

    detail = gh("pr", "view", str(n), "-R", repo, "--json", "number,title,body,baseRefName,headRefName")
    text, meta = review_pr(host, cfg["model"], detail, shown, diff, note)

    body = (f"{MARKER} sha={sha} -->\n{meta}\n"
            f"> 🤖 ローカルLLM(`{cfg['model']}`)による自動レビューです(コミット {sha[:7]})。"
            f"誤りを含むことがあるので、参考情報として扱ってください。\n\n{text}\n")
    path = os.path.join(out_dir, f"pr{n}-{sha[:7]}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    if cfg.get("post_comment", False):
        gh("pr", "comment", str(n), "-R", repo, "--body-file", path, parse=False)
        log(f"PR #{n}: コメントを投稿しました")
    return path


def poll_once(cfg, host, out_dir, state, state_path):
    for pr in list_open_prs(cfg["repo"]):
        key, sha = str(pr["number"]), pr["headRefOid"]
        if state["reviewed"].get(key) == sha or state["skipped"].get(key) == sha:
            continue
        if not is_candidate(pr, cfg):
            continue
        if state["failures"].get(f"{key}@{sha}", 0) >= MAX_FAILURES:
            continue
        log(f"PR #{key} ({sha[:7]}) {pr['title']}")
        try:
            path = handle(pr, cfg, host, out_dir)
        except Exception as e:  # 1 件の失敗で監視全体を止めない
            fk = f"{key}@{sha}"
            state["failures"][fk] = state["failures"].get(fk, 0) + 1
            log(f"  失敗({state['failures'][fk]}/{MAX_FAILURES}): {e!r}")
        else:
            if path:
                state["reviewed"][key] = sha
                log(f"  -> {path}")
            else:
                state["skipped"][key] = sha
                log("  対象チームのファイルを含まないためスキップ")
        save_state(state_path, state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--until", type=float, required=True, help="この UNIX 時刻までに終了する")
    ap.add_argument("--once", action="store_true", help="1 回だけ確認して終了する")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    root = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(root, "runs", cfg["repo"].replace("/", "_"))
    os.makedirs(out_dir, exist_ok=True)
    state_path = os.path.join(out_dir, "state.json")
    state = load_state(state_path)

    log(f"監視開始: {cfg['repo']} model={cfg['model']} post_comment={cfg.get('post_comment', False)} "
        f"終了予定 {datetime.fromtimestamp(args.until):%H:%M}")
    interval = cfg.get("poll_interval_sec", 60)
    while True:
        try:
            poll_once(cfg, args.host, out_dir, state, state_path)
        except subprocess.CalledProcessError as e:  # GitHub 側の一時的な失敗は次回に再試行
            log(f"PR 一覧の取得に失敗: {e.stderr.strip()[:300]}")
        if args.once or time.time() + interval > args.until:
            break
        time.sleep(interval)
    log("監視終了")


if __name__ == "__main__":
    sys.exit(main())
