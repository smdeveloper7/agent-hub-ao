"""AO 프로젝트 등록 전 점검과 설정 한 번에 넣기.

AO 프로젝트는 git 저장소 하나다. Supervisor·Worker 는 그 저장소의 git worktree(복사본 브랜치)에서 일한다.
여러 저장소를 담은 상위 폴더(`--as-workspace`)는 AO 가 상위 폴더에 git 저장소를 새로 만들고 하위 저장소의
작업 공간 자산을 복사하는데, 정션·링크나 origin/HEAD 없는 하위 저장소에서 Supervisor 시작이 실패했다(11-04 실측).
그래서 aohub 는 대표 저장소 하나만 등록하고, 다른 저장소는 배정 요청에 절대 경로로 적어 읽게 한다.
"""
import os
import subprocess

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".next",
             "dist", "build", "target", ".tox"}
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows 정션·심볼릭 링크)


def _git(path, *args):
    try:
        p = subprocess.run(["git", "-C", path] + list(args), capture_output=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_link(path):
    try:
        if os.path.islink(path):
            return True
        isj = getattr(os.path, "isjunction", None)
        if isj and isj(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def find_links(root, max_depth=4, limit=20):
    """정션·심볼릭 링크를 찾는다(큰 폴더는 건너뜀). 저장소 경로 기준 상대 경로 목록."""
    found = []

    def walk(d, depth):
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if len(found) >= limit:
                return
            if e.name in SKIP_DIRS:
                continue
            if is_link(e.path):
                found.append(os.path.relpath(e.path, root).replace("\\", "/"))
                continue
            if depth < max_depth and e.is_dir(follow_symlinks=False):
                walk(e.path, depth + 1)

    walk(root, 1)
    return found


def child_repos(path):
    try:
        return sorted(e.name for e in os.scandir(path)
                      if e.is_dir(follow_symlinks=False) and os.path.exists(os.path.join(e.path, ".git")))
    except OSError:
        return []


def preflight(path, default_branch=None):
    """등록 전 점검. errors 가 있으면 등록하지 않는다. warnings 는 알리고 진행한다.

    결과: {path, ok, default_branch, remote, origin_head, links, child_repos, errors, warnings}
    """
    res = {"path": path, "default_branch": None, "remote": None, "origin_head": None, "links": [],
           "child_repos": [], "errors": [], "warnings": []}
    if not path or not os.path.isdir(path):
        res["errors"].append("폴더가 없습니다: %s" % path)
        res["ok"] = False
        return res
    rc, top, _ = _git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not _same(top, path):
        kids = child_repos(path)
        res["child_repos"] = kids
        if rc != 0:
            msg = "git 저장소가 아닙니다. AO 프로젝트는 git 저장소 하나만 등록합니다."
        else:
            msg = "저장소 최상위가 아닙니다(최상위: %s). 그 경로로 등록하세요." % top
        if kids:
            msg += " 이 폴더 아래 저장소: %s. 일할 대표 저장소 하나를 등록하고, 나머지는 배정 요청에 절대 경로로 적어 읽게 하세요." % ", ".join(kids)
        res["errors"].append(msg)
        res["ok"] = False
        return res
    kids = child_repos(path)
    if kids:
        res["child_repos"] = kids
        res["errors"].append("하위 저장소(%s)를 담은 상위 폴더입니다. AO 가 상위 폴더 묶음(workspace)으로 다뤄 작업 공간 생성이 "
                             "실패하기 쉽습니다. 일할 대표 저장소 하나를 등록하고, 나머지는 배정 요청에 절대 경로로 적어 읽게 하세요."
                             % ", ".join(kids))
        res["ok"] = False
        return res
    if _git(path, "rev-parse", "--verify", "HEAD")[0] != 0:
        res["errors"].append("커밋이 하나도 없습니다. 첫 커밋 뒤에 등록하세요(worktree 를 만들 기준이 없음).")
    remotes = _git(path, "remote")[1].split()
    res["remote"] = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    current = _git(path, "branch", "--show-current")[1] or None
    if res["remote"]:
        rc, head, _ = _git(path, "symbolic-ref", "--short", "refs/remotes/%s/HEAD" % res["remote"])
        res["origin_head"] = head.split("/", 1)[1] if rc == 0 and "/" in head else None
    branch = default_branch or res["origin_head"] or current
    if not branch:
        res["errors"].append("기본 브랜치를 정할 수 없습니다(분리된 HEAD). --default-branch 로 주세요.")
    elif _git(path, "rev-parse", "--verify", "refs/heads/%s" % branch)[0] != 0 and \
            not (res["remote"] and _git(path, "rev-parse", "--verify", "refs/remotes/%s/%s" % (res["remote"], branch))[0] == 0):
        res["errors"].append("기본 브랜치 %s 가 저장소에 없습니다." % branch)
    res["default_branch"] = branch
    if res["remote"] and not res["origin_head"]:
        res["warnings"].append("%s/HEAD 가 없어 기본 브랜치를 %s 로 직접 지정합니다. AO 가 원격 기준을 못 찾으면 "
                               "`git -C <저장소> remote set-head %s %s` 가 필요할 수 있습니다(사용자 확인 뒤)." %
                               (res["remote"], branch, res["remote"], branch))
    if not res["remote"]:
        res["warnings"].append("원격이 없습니다. 기본 브랜치를 %s 로 직접 지정합니다(지정하지 않으면 Supervisor 대화 시작이 실패함)." % branch)
    res["links"] = find_links(path)
    if res["links"]:
        res["warnings"].append("정션·링크가 있습니다: %s. 상위 폴더 workspace 등록에서는 이것 때문에 작업 공간 생성이 실패했습니다. "
                               "Supervisor 가 멈추면 이 목록부터 보세요." % ", ".join(res["links"][:5]))
    res["ok"] = not res["errors"]
    return res


def merged_config(current, rules_fn, default_branch, worker_agent=None, worker_model=None, permission=None):
    """기존 설정 위에 aohub 가 필요한 값만 덮는다. set-config 는 통째 교체라 빠진 값은 지워진다."""
    c = dict(current or {})
    if default_branch:
        c["defaultBranch"] = default_branch
    ac = dict(c.get("agentConfig") or {})
    if permission or not ac.get("permissions"):
        ac["permissions"] = permission or "auto"
    c["agentConfig"] = ac
    orch = dict(c.get("orchestrator") or {})
    orch.setdefault("agent", "claude-code")
    orch.setdefault("agentConfig", {})
    c["orchestrator"] = orch
    w = dict(c.get("worker") or {})
    if worker_agent:
        if w.get("agent") and w.get("agent") != worker_agent:
            w["agentConfig"] = {}  # 다른 agent 의 모델 설정을 끌고 가지 않는다
        w["agent"] = worker_agent
    w.setdefault("agent", "claude-code")
    wac = dict(w.get("agentConfig") or {})
    if worker_model:
        wac["model"] = worker_model
    w["agentConfig"] = wac
    c["worker"] = w
    c["orchestratorRules"] = rules_fn(c.get("orchestratorRules") or "")
    return c


def missing_settings(c):
    """Supervisor 를 띄우기 전에 있어야 하는 설정 중 빠진 것."""
    c = c or {}
    miss = []
    if not c.get("defaultBranch"):
        miss.append("defaultBranch")
    if not (c.get("orchestrator") or {}).get("agent"):
        miss.append("orchestrator.agent")
    if not (c.get("worker") or {}).get("agent"):
        miss.append("worker.agent")
    if "<!-- aohub:begin -->" not in (c.get("orchestratorRules") or ""):
        miss.append("orchestratorRules(aohub)")
    return miss
