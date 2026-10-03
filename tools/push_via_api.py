"""经 GitHub Git Data API 推送本地提交（绕过被代理拦掉的 git 传输）。

## 为什么需要它

这个环境里 `HTTP_PROXY` 指向的代理对 `github.com` 的 git-over-HTTPS 一律返回
502（`CONNECT tunnel failed`），但 `api.github.com` 是通的 —— 所以 `gh` 能用、
`git push` 不能用。本脚本改走 Git Data API（blob → tree → commit → ref）。

## 为什么本地与远端的 sha 对不上（以及怎么处理）

**API 造的 commit 与 git 造的永远不可能同 sha**，实测差异有两处：

1. GitHub 保留发送时的**时区偏移**（`+0800`），而不是规范化成 UTC
2. GitHub 存的消息**不带结尾换行**；而 `git commit` 总会补一个 `\\n`

第 2 点无法从本地规避 —— 所以本脚本不去"对齐 sha"，而是**反过来**：
推送成功后，用 GitHub 返回的字段在本地**逐字节重建那个 commit 对象**
（`git hash-object -t commit -w`），再把本地 `main` 指过去。
这样本地与远端完全一致，以后网络恢复了 `git push` 也不会冲突。

重建前会校验算出的 sha 与远端一致 —— 对不上就中止，绝不让两边分叉。
"""
from __future__ import annotations

import base64
import datetime
import json
import subprocess
import sys
from pathlib import Path

REPO = "bbmio/meidochan"
RELEASE = Path(r"C:\Users\A\Desktop\meidochanv0.1-release")


def git(*args: str, binary: bool = False, stdin: bytes | None = None):
    r = subprocess.run(["git", *args], cwd=RELEASE, capture_output=True, input=stdin)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{r.stderr.decode('utf-8', 'replace')}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()


def gh_api(method: str, path: str, body: dict | None = None) -> dict:
    args = ["gh", "api", "-X", method, path]
    if body is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(body, ensure_ascii=False) if body else None,
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"gh api {method} {path} 失败：{r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _identity(line: str) -> tuple[dict, str]:
    """把 `name <email> <ts> <tz>` 拆成 (API 用的 dict, **发送时会用的** tz)。

    ⚠️ tz 必须取自"我们即将发出去的 ISO 字符串"，不能取本地 commit 里那个 ——
    GitHub 会原样保留发送时的偏移。本地 commit 若是 `+0000` 而发送时算出 `+08:00`，
    重建就会差一个时区，sha 对不上。
    """
    who, _, rest = line.partition(" <")
    email, _, tail = rest.partition("> ")
    stamp = tail.split()[0]
    local = datetime.datetime.fromtimestamp(
        int(stamp), datetime.timezone.utc).astimezone()
    return ({"name": who, "email": email, "date": local.isoformat()},
            local.strftime("%z") or "+0000")


def _adopt_remote_commit(sha: str, tree: str, parent: str,
                         message: str, author: dict, committer: dict,
                         tz: str) -> None:
    """在本地逐字节重建远端那个 commit 对象，并把 main 指过去。

    GitHub 的序列化是：`tree/parent/author/committer` + 空行 + **不带尾换行**的消息。
    """
    def _line(role: str, who: dict) -> str:
        stamp = int(datetime.datetime.fromisoformat(
            who["date"].replace("Z", "+00:00")).timestamp())
        return f"{role} {who['name']} <{who['email']}> {stamp} {tz}"

    text = "\n".join([
        f"tree {tree}",
        f"parent {parent}",
        _line("author", author),
        _line("committer", committer),
        "",
        message,
    ])
    data = text.encode("utf-8")
    created = git("hash-object", "-t", "commit", "-w", "--stdin",
                  stdin=data, binary=False)
    if created != sha:
        raise SystemExit(
            f"本地重建的 commit sha 与远端不一致（{created} != {sha}）—— "
            "中止，避免本地与远端分叉。")
    git("update-ref", "refs/heads/main", created)
    print(f"✅ 本地 main 已指向 {created}（与远端一致）")


def adopt_remote_history() -> int:
    """把远端整条历史在本地重建，让两边 sha 完全一致。

    改写历史 / 强制推送之后跑它：远端那串提交本地没有对应对象，
    不重建的话 `git log` 会断在父提交上，以后 `git push` 也会冲突。

    ⚠️ 时区取**本机当前偏移** —— 远端 commit 的时区就是当初推送时
    `_identity()` 算出来的本机偏移，API 的日期是 UTC 化的、读不出偏移。
    """
    tz = datetime.datetime.now().astimezone().strftime("%z") or "+0000"
    commits = gh_api("GET", f"repos/{REPO}/commits?sha=main&per_page=100")
    order = [c["sha"] for c in commits][::-1]        # 从根到 HEAD
    print(f"远端共 {len(order)} 个提交，在本地重建…（tz={tz}）")
    parent = ""
    for sha in order:
        c = gh_api("GET", f"repos/{REPO}/git/commits/{sha}")
        lines = [f"tree {c['tree']['sha']}"]
        if parent:
            lines.append(f"parent {parent}")
        for role in ("author", "committer"):
            who = c[role]
            stamp = int(datetime.datetime.fromisoformat(
                who["date"].replace("Z", "+00:00")).timestamp())
            lines.append(f"{role} {who['name']} <{who['email']}> {stamp} {tz}")
        lines += ["", c["message"]]
        created = git("hash-object", "-t", "commit", "-w", "--stdin",
                      stdin="\n".join(lines).encode("utf-8"), binary=False)
        if created != sha:
            raise SystemExit(
                f"重建 {sha[:7]} 失败（得到 {created[:7]}）—— 中止，"
                "本地与远端会分叉。多半是时区不对。")
        parent = created
        print(f"  {created[:7]}  {c['message'].splitlines()[0]}")
    git("update-ref", "refs/heads/main", parent)
    print(f"✅ 本地 main 已指向 {parent[:7]}（与远端一致）")
    return 0


def _tree_exists(sha: str) -> bool:
    r = subprocess.run(["gh", "api", f"repos/{REPO}/git/trees/{sha}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def _commit_fields(sha: str) -> tuple[str, str, str, str]:
    """返回 (tree, 第一条 parent 或空, 完整 message, 作者行)。"""
    raw = git("cat-file", "commit", sha)
    fields, lines, in_msg = {}, [], False
    for line in raw.splitlines():
        if in_msg:
            lines.append(line)
        elif line == "":
            in_msg = True
        else:
            key, _, value = line.partition(" ")
            fields.setdefault(key, value)
    parents = [l.split(" ", 1)[1] for l in raw.splitlines()
               if l.startswith("parent ")]
    return (fields["tree"], parents[0] if parents else "",
            "\n".join(lines).rstrip("\n"), fields["author"])


def _blob_entries(sha: str) -> list:
    """本提交相对其父改动过的文件 → tree entries（blob 逐字节从 git 取）。

    `--root` 让根提交也能列出全部文件（对空树求差异）。
    """
    entries = []
    out = git("diff-tree", "--no-commit-id", "--name-status", "-r", "--root", sha)
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob_sha = git("ls-tree", sha, "--", path).split()[2]
        content = git("cat-file", "blob", blob_sha, binary=True)
        created = gh_api("POST", f"repos/{REPO}/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64"})
        if created["sha"] != blob_sha:
            raise SystemExit(f"{path} 的 blob sha 对不上")
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
    return entries


def rewrite_remote_history() -> int:
    """把本地历史整体镜像到远端（改写提交信息后用它强制推送）。

    与 `main()` 的「追加」模式不同：这里从**根提交**开始逐个重建 ——
    改写过的提交与远端已无共同祖先，没法只推差异。

    已存在的 tree 直接复用（`POST /git/commits` 接受任意 tree sha），
    所以只有真正新增的内容才会产生 blob。
    """
    commits = git("log", "--reverse", "--format=%H").splitlines()
    print(f"本地共 {len(commits)} 个提交，整体镜像到远端…")
    parent_sha, parent_tree = "", ""
    for sha in commits:
        tree, _lp, message, author_line = _commit_fields(sha)
        if not _tree_exists(tree):
            body = {"tree": _blob_entries(sha)}
            if parent_tree:
                body["base_tree"] = parent_tree
            tree = gh_api("POST", f"repos/{REPO}/git/trees", body)["sha"]
            if tree != _commit_fields(sha)[0]:
                raise SystemExit(f"{sha[:7]} 的 tree 重建后 sha 不一致，中止")
        who, _tz = _identity(author_line)
        created = gh_api("POST", f"repos/{REPO}/git/commits", {
            "message": message,
            "tree": tree,
            "parents": [parent_sha] if parent_sha else [],
            "author": who,
            "committer": who,
        })
        print(f"  {sha[:7]} → {created['sha'][:7]}  {message.splitlines()[0]}")
        parent_sha, parent_tree = created["sha"], tree
    gh_api("PATCH", f"repos/{REPO}/git/refs/heads/main",
           {"sha": parent_sha, "force": True})
    print(f"✅ 远端 main 已强制指向 {parent_sha[:7]}")
    return 0


def main() -> int:
    if "--rewrite" in sys.argv:
        return rewrite_remote_history()
    if "--adopt" in sys.argv:
        return adopt_remote_history()

    local_head = git("rev-parse", "HEAD")
    remote_head = gh_api("GET", f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
    print(f"本地 HEAD : {local_head}")
    print(f"远端 main : {remote_head}")
    if local_head == remote_head:
        print("已经一致，无需推送。")
        return 0

    # 幂等：远端那个提交的 tree 若已等于本地的，内容其实已经同步过了
    # （上一次推送可能成功更新了 ref、只是本地对齐失败），此时只需对齐本地。
    # ⚠️ 这个判断必须在「祖先检查」**之前** —— 对齐失败时本地与远端是**兄弟**
    # 提交（同一个父），过不了祖先检查，会误报「历史已分叉」。
    remote_commit = gh_api("GET", f"repos/{REPO}/git/commits/{remote_head}")
    if remote_commit["tree"]["sha"] == git("rev-parse", f"{local_head}^{{tree}}"):
        print("远端内容已是最新，只需把本地对齐到远端。")
        raw = git("cat-file", "commit", local_head)
        fields, message_lines, in_message = {}, [], False
        for line in raw.splitlines():
            if in_message:
                message_lines.append(line)
            elif line == "":
                in_message = True
            else:
                key, _, value = line.partition(" ")
                fields[key] = value
        author, author_tz = _identity(fields["author"])
        committer, _ = _identity(fields["committer"])
        _adopt_remote_commit(remote_head, remote_commit["tree"]["sha"],
                             remote_commit["parents"][0]["sha"],
                             remote_commit["message"], author, committer, author_tz)
        return 0

    if subprocess.run(["git", "merge-base", "--is-ancestor", remote_head, local_head],
                      cwd=RELEASE, capture_output=True).returncode != 0:
        raise SystemExit("远端不是本地的祖先 —— 历史已分叉，请人工处理。")

    raw = git("cat-file", "commit", local_head)
    fields, message_lines, in_message = {}, [], False
    for line in raw.splitlines():
        if in_message:
            message_lines.append(line)
        elif line == "":
            in_message = True
        else:
            key, _, value = line.partition(" ")
            fields[key] = value
    message = "\n".join(message_lines).rstrip("\n")

    entries = []
    # ⚠️ 必须带 `-c core.quotepath=false`：git 默认把非 ASCII 路径转义成
    # `"\345\220\257\345\212\250..."`（带引号的八进制），拿去 `git ls-tree` 会查不到
    # → `split()[2]` 直接 IndexError。本项目有 `启动妹抖酱.bat` 这种中文文件名，
    # 只要它被改动就会踩到。
    diff_out = git("-c", "core.quotepath=false",
                   "diff", "--name-status", remote_head, local_head)
    for line in diff_out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        # 兜底：含特殊字符的路径 git 仍可能加引号
        if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if status.startswith("D"):
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print(f"  删除 {path}")
            continue
        blob_sha = git("ls-tree", local_head, "--", path).split()[2]
        content = git("cat-file", "blob", blob_sha, binary=True)
        created = gh_api("POST", f"repos/{REPO}/git/blobs", {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64"})
        if created["sha"] != blob_sha:
            raise SystemExit(f"{path} 的 blob sha 对不上：{created['sha']} != {blob_sha}")
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})
        print(f"  {status} {path}")

    if not entries:
        print("没有文件差异。")
        return 0

    base_tree = gh_api("GET", f"repos/{REPO}/git/commits/{remote_head}")["tree"]["sha"]
    tree = gh_api("POST", f"repos/{REPO}/git/trees",
                  {"base_tree": base_tree, "tree": entries})
    if tree["sha"] != fields["tree"]:
        raise SystemExit(f"tree sha 对不上：{tree['sha']} != {fields['tree']}")

    author, author_tz = _identity(fields["author"])
    committer, committer_tz = _identity(fields["committer"])
    if author_tz != committer_tz:
        raise SystemExit(f"作者与提交者时区不一致（{author_tz} / {committer_tz}），"
                         "本地重建会失准，请先统一。")

    commit = gh_api("POST", f"repos/{REPO}/git/commits", {
        "message": message,
        "tree": tree["sha"],
        "parents": [remote_head],
        "author": author,
        "committer": committer,
    })
    print(f"远端新提交: {commit['sha']}")
    gh_api("PATCH", f"repos/{REPO}/git/refs/heads/main",
           {"sha": commit["sha"], "force": False})

    # 远端已更新，把本地对齐到它 —— 否则两边分叉，以后 git push 会冲突
    _adopt_remote_commit(commit["sha"], tree["sha"], remote_head,
                         commit["message"], author, committer, author_tz)
    return 0


if __name__ == "__main__":
    sys.exit(main())
