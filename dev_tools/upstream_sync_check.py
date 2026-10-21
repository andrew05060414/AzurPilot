"""Fork 与上游同步检查（只读）。

本仓库是 wess09/AzurPilot 的 fork，fork 上带有自己的修复提交。上游以
「dev 合并至 master」的大提交节奏更新，fork 不定期跟进就会越落越远，
等到出问题时才发现已落后几百个提交。本脚本把「落后多少、能不能干净合并、
运行目录是不是最新」一次查清，并打印建议的合并命令，不做任何合并。

检查内容：
1. ``git fetch <upstream remote>``（只更新远程跟踪分支，不动工作区与本地分支）。
2. 本地分支相对上游分支的领先 / 落后提交数、上游最新提交时间、落后提交列表。
3. 用 ``git merge-tree --write-tree`` 在内存中试合并，列出会冲突的文件
   （不写工作区、不建分支；需要 git >= 2.38）。
4. 可选：运行目录（默认 ``D:\\模拟器\\AzurPilot``）当前 HEAD 是否等于本地分支、
   是否有已跟踪文件改动。只读取，不在运行目录里 fetch / pull。

退出码：0 已同步；1 落后但可干净合并；2 落后且有冲突；3 检查本身出错。
运行目录不是最新只打印提示，不影响退出码。

用法：
    uv run python -m dev_tools.upstream_sync_check
    uv run python -m dev_tools.upstream_sync_check --no-fetch --deploy ""
    uv run python -m dev_tools.upstream_sync_check --json
"""

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPLOY = r"D:\模拟器\AzurPilot" if os.name == "nt" else ""
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    pass


def git(repo, *args, check=True):
    """运行只读 git 命令，返回 (returncode, stdout)。"""
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.returncode, proc.stdout


def rev(repo, ref):
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")[1].strip()


def parse_merge_tree(output):
    """解析 ``merge-tree --write-tree --name-only`` 的输出，返回冲突文件列表。

    输出格式：第 1 行是树 OID；随后是冲突文件名（每行一个）；空行之后是
    信息性消息（Auto-merging / CONFLICT ...）。
    """
    lines = output.splitlines()
    files = []
    for line in lines[1:]:
        if not line.strip():
            break
        files.append(line.strip())
    return sorted(set(files))


def check(repo, branch, upstream, deploy, fetch, max_list):
    remote = upstream.split("/", 1)[0]
    result = {"repo": str(repo), "branch": branch, "upstream": upstream}

    if fetch:
        code, _ = git(repo, "fetch", "--quiet", remote, check=False)
        result["fetched"] = code == 0
    else:
        result["fetched"] = None

    local_sha = rev(repo, branch)
    up_sha = rev(repo, upstream)
    ahead, behind = (
        int(x) for x in git(repo, "rev-list", "--left-right", "--count", f"{branch}...{upstream}")[1].split()
    )
    result.update(local_sha=local_sha, upstream_sha=up_sha, ahead=ahead, behind=behind)
    result["upstream_date"] = git(repo, "log", "-1", "--format=%cI", up_sha)[1].strip()
    result["behind_commits"] = git(
        repo, "log", "--format=%h %cs %s", f"-{max_list}", f"{branch}..{upstream}"
    )[1].splitlines()

    if behind:
        base = git(repo, "merge-base", branch, upstream)[1].strip()
        result["merge_base_date"] = git(repo, "log", "-1", "--format=%cI", base)[1].strip()
        code, out = git(repo, "merge-tree", "--write-tree", "--name-only", branch, upstream, check=False)
        if code not in (0, 1):
            raise GitError(f"merge-tree 失败（git 版本需 >= 2.38）：{out.strip()}")
        result["conflicts"] = parse_merge_tree(out) if code == 1 else []
        diffstat = git(repo, "diff", "--shortstat", f"{base}..{upstream}")[1].strip()
        result["upstream_diffstat"] = diffstat
    else:
        result["conflicts"] = []

    if deploy:
        result["deploy"] = check_deploy(Path(deploy), repo, local_sha, up_sha)
    return result


def check_deploy(path, repo, local_sha, up_sha):
    info = {"path": str(path)}
    if not (path / ".git").exists():
        info["error"] = "不是 git 仓库或路径不存在"
        return info
    try:
        head = rev(path, "HEAD")
    except GitError as exc:
        info["error"] = str(exc)
        return info
    info["head"] = head
    info["matches_branch"] = head == local_sha
    # 在代码仓库的对象库里判断祖先关系；运行目录的提交若不在代码仓库里会返回非 0。
    info["contains_upstream"] = git(repo, "merge-base", "--is-ancestor", up_sha, head, check=False)[0] == 0
    dirty = git(path, "status", "--porcelain", "--untracked-files=no")[1].splitlines()
    info["tracked_changes"] = dirty
    return info


def suggested_commands(r):
    today = _dt.date.today().isoformat()
    name = f"merge/upstream-{today}"
    wt = Path(r["repo"]) / ".worktrees" / f"upstream-sync-{today}"
    cmds = [
        f'git -C "{r["repo"]}" worktree add "{wt}" -b {name} {r["branch"]}',
        f'git -C "{wt}" merge --no-ff {r["upstream"]}',
    ]
    if r["conflicts"]:
        cmds.append("# 逐个解决上面列出的冲突文件后：git add <文件> && git commit --no-edit")
    cmds += [
        "# 配置相关文件被合并后，按 AGENTS.md 重新生成并确认无多余改动：",
        f'cd "{wt}" && uv sync --frozen && uv run python -m module.config.config_updater && git status --short',
        f'cd "{wt}" && uv run python -m dev_tools.import_smoke_test',
        f'# 验证通过后让 {r["branch"]} 快进：git -C "{r["repo"]}" merge --ff-only {name}',
    ]
    dep = r.get("deploy") or {}
    if dep.get("path") and not dep.get("error"):
        cmds.append(f'# 更新运行目录（属于部署，人工执行）：git -C "{dep["path"]}" pull --ff-only local-code {r["branch"]}')
    return cmds


def render(r):
    out = []
    state = "已同步" if not r["behind"] else ("落后，可干净合并" if not r["conflicts"] else "落后，有冲突")
    out.append(f"结论：{state}")
    if r["fetched"] is False:
        out.append("警告：fetch 失败，以下结果基于本地已有的远程跟踪分支")
    out.append(f"{r['branch']} 相对 {r['upstream']}：领先 {r['ahead']}，落后 {r['behind']}")
    out.append(f"上游最新提交时间：{r['upstream_date']}")
    if r["behind"]:
        out.append(f"共同祖先时间：{r['merge_base_date']}")
        out.append(f"上游自共同祖先以来的改动：{r['upstream_diffstat']}")
        out.append("落后的提交（最新在前）：")
        out += [f"  {c}" for c in r["behind_commits"]]
        if r["behind"] > len(r["behind_commits"]):
            out.append(f"  ……另有 {r['behind'] - len(r['behind_commits'])} 个")
        if r["conflicts"]:
            out.append(f"试合并冲突文件（{len(r['conflicts'])} 个）：")
            out += [f"  {f}" for f in r["conflicts"]]
        else:
            out.append("试合并：无冲突")
    dep = r.get("deploy")
    if dep:
        out.append(f"运行目录 {dep['path']}：")
        if dep.get("error"):
            out.append(f"  无法检查：{dep['error']}")
        else:
            out.append(f"  HEAD {dep['head'][:10]}，{'等于' if dep['matches_branch'] else '不等于'} {r['branch']}")
            out.append(f"  {'已包含' if dep['contains_upstream'] else '未包含'}上游最新提交")
            if dep["tracked_changes"]:
                out.append(f"  有 {len(dep['tracked_changes'])} 个已跟踪文件改动，更新前先处理")
    if r["behind"]:
        out.append("建议命令（本脚本不执行）：")
        out += [f"  {c}" for c in suggested_commands(r)]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fork 与上游同步检查（只读）")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="代码仓库路径，默认本仓库")
    parser.add_argument("--branch", default="master", help="fork 的主分支，默认 master")
    parser.add_argument("--upstream", default="upstream/master", help="上游分支，默认 upstream/master")
    parser.add_argument("--deploy", default=DEFAULT_DEPLOY, help='运行目录路径，传 "" 跳过')
    parser.add_argument("--no-fetch", action="store_true", help="不 fetch，只用本地已有的远程跟踪分支")
    parser.add_argument("--max-list", type=int, default=20, help="最多列出多少个落后提交")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    try:
        r = check(Path(args.repo), args.branch, args.upstream, args.deploy, not args.no_fetch, args.max_list)
    except GitError as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        return 3
    if r["behind"]:
        r["suggested_commands"] = suggested_commands(r)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else render(r))
    if not r["behind"]:
        return 0
    return 2 if r["conflicts"] else 1


if __name__ == "__main__":
    sys.exit(main())
