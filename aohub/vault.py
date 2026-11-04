"""Obsidian Vault 기록. 노트를 고치는 곳은 이 모듈 하나다.

- 모든 쓰기는 상태 폴더의 vault.lock 을 잡고 한다. Worker 와 Supervisor 는 노트를 직접 고치지 않고
  `aohub` 보고 명령으로 DB 에만 쓴다. 노트는 DB 를 다시 그려 만든다(같은 이벤트를 다시 받아도 결과가 같다).
- 노트에서 자동으로 고치는 곳은 두 군데다.
  1. Properties(frontmatter) 중 이 모듈이 맡은 키. 사용자가 더한 키는 그대로 둔다.
  2. `<!-- aohub:begin 이름 -->` 와 `<!-- aohub:end 이름 -->` 사이. 그 밖의 본문(사용자 메모)은 건드리지 않는다.
- 일일 로그(raw/YYYY-MM-DD.md)는 덧붙이기만 한다. 공통 스크립트(worklog.ps1)가 설정되어 있으면 그 스크립트로,
  아니면 같은 형식의 내장 기록기로 쓴다. 둘 다 `<!-- wl:<id> -->` 표시로 같은 기록을 한 번만 넣는다.
"""
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager

from . import config
from .store import KST, now

BEGIN = "<!-- aohub:begin %s -->"
END = "<!-- aohub:end %s -->"
MEMO = "\n## 메모\n\n<!-- 이 아래는 사람이 쓰는 곳입니다. 자동 갱신이 고치지 않습니다. -->\n"

STATUS_KO = {
    "created": "생성", "assigned": "배정", "working": "진행 중", "needs_input": "판단 대기",
    "reported_done": "완료 보고(검증 전)", "verified_done": "완료(검증됨)", "verify_failed": "검증 실패",
    "blocked": "막힘", "cancelled": "취소",
}


# ---------------------------------------------------------------- 잠금
@contextmanager
def file_lock(path, timeout=60):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+b")
    deadline = time.time() + timeout
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError("잠금 대기 시간 초과: " + path)
                    time.sleep(0.05)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError("잠금 대기 시간 초과: " + path)
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


# ---------------------------------------------------------------- 파일 다루기
def read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_if_changed(path, text):
    old = read(path)
    if old == text:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".aohub-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt == 19:
                os.unlink(tmp)
                raise
            time.sleep(0.1)


def yaml_value(v, indent="  "):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        return "".join("\n%s- %s" % (indent, yaml_value(x)) for x in v)
    s = str(v).replace("\r", "")
    return json.dumps(" ".join(s.split("\n")) if "\n" in s else s, ensure_ascii=False)


def split_frontmatter(text):
    """(frontmatter 키 블록 목록 [(키, 원문 줄들)], 본문)."""
    if not text.startswith("---\n"):
        return [], text
    if text.startswith("---\n---\n"):
        return [], text[8:]
    end = text.find("\n---\n", 4)
    if end < 0:
        return [], text
    fm, body = text[4:end], text[end + 5:]
    blocks = []
    for line in fm.split("\n"):
        m = re.match(r"^([^\s#:][^:]*):(\s|$)", line)
        if m or not blocks:
            blocks.append([m.group(1).strip() if m else None, [line]])
        else:
            blocks[-1][1].append(line)
    return [(k, ls) for k, ls in blocks], body


def merge_frontmatter(text, owned):
    """owned(dict) 의 키만 바꾸거나 더한다. 다른 키와 순서는 그대로 둔다."""
    blocks, body = split_frontmatter(text)
    while body.startswith("---\n---\n"):  # 0.1.0 초기판이 남긴 빈 frontmatter 정리
        body = body[8:]
    seen = set()
    out = []
    for key, lines in blocks:
        if key in owned:
            seen.add(key)
            out.append("%s: %s" % (key, yaml_value(owned[key])))
        else:
            out.extend(lines)
    for key, val in owned.items():
        if key not in seen:
            out.append("%s: %s" % (key, yaml_value(val)))
    return "---\n" + "\n".join(x for x in out if x is not None) + "\n---\n" + body


def replace_block(text, name, content):
    b, e = BEGIN % name, END % name
    block = "%s\n%s\n%s" % (b, content.strip("\n"), e)
    i = text.find(b)
    j = text.find(e, i + 1) if i >= 0 else -1
    if i >= 0 and j >= 0:
        return text[:i] + block + text[j + len(e):]
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block + "\n"


def upsert_note(path, title, owned, blocks, memo=True):
    """노트를 만들거나, 맡은 Properties 와 자동 구역만 고친다. 바뀌었으면 True."""
    text = read(path)
    if text is None:
        text = "---\n---\n# %s\n" % title
        for name, content in blocks:
            text = replace_block(text, name, content)
        if memo:
            text += MEMO
    else:
        for name, content in blocks:
            text = replace_block(text, name, content)
    text = merge_frontmatter(text, owned)
    return write_if_changed(path, text)


def md_cell(s, limit=160):
    s = " ".join(str(s or "").split())
    if len(s) > limit:
        s = s[:limit - 1] + "…"
    return s.replace("|", "\\|")


def short_time(iso):
    return (iso or "")[:16].replace("T", " ")


# ---------------------------------------------------------------- 일일 로그
def raw_entry_id(event_key):
    return "ao" + hashlib.sha1(event_key.encode("utf-8")).hexdigest()[:10]


def append_raw(cfg, vault_dir, entry, entry_id):
    """entry: date(YYYY-MM-DD), time(HH:mm), agent, project, status, title, result, check, next, reason."""
    raw_dir = os.path.join(vault_dir, cfg.get("raw_dir") or "raw")
    script = (cfg.get("raw_script") or "").strip()
    if script:
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="aohub-raw-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
        try:
            exe = "powershell" if os.name == "nt" else "pwsh"
            p = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-JsonFile", tmp,
                                "-Id", entry_id, "-RawDir", raw_dir], capture_output=True, timeout=60)
            out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
            if p.returncode != 0:
                raise RuntimeError("일일 로그 스크립트 실패: " + out.strip()[:300])
            return "기록함" in out
        finally:
            os.unlink(tmp)
    # 내장 기록기: worklog.ps1 과 같은 형식, 같은 잠금 파일 이름.
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, entry["date"] + ".md")
    marker = "<!-- wl:%s -->" % entry_id
    lines = ["### %s | %s | %s | %s | %s" % (entry["time"], entry["agent"], entry["project"], entry["status"], entry["title"]),
             "- 결과: " + entry["result"], "- 확인: " + (entry.get("check") or "확인 필요"),
             "- 다음: " + (entry.get("next") or "없음")]
    if entry.get("reason"):
        lines.append("- 결정: " + entry["reason"])
    lines.append(marker)
    text = "\n".join(" ".join(l.split()) if not l.startswith("<!--") else l for l in lines) + "\n"
    with file_lock(os.path.join(raw_dir, ".aohub-raw.lock")):
        existing = read(path) or ""
        if marker in existing:
            return False
        if not existing.strip():
            prefix = "# %s\n\n" % entry["date"]
        elif existing.endswith("\n\n"):
            prefix = ""
        elif existing.endswith("\n"):
            prefix = "\n"
        else:
            prefix = "\n\n"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(prefix + text)
    return True


# ---------------------------------------------------------------- 그리기
class Vault:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.load()
        self.dir = os.path.abspath(os.path.expanduser(self.cfg.get("vault_dir") or ""))
        self.wiki = self.cfg.get("wiki_dir") or "wiki"
        self.raw = self.cfg.get("raw_dir") or "raw"

    @property
    def enabled(self):
        return bool(self.cfg.get("vault_dir")) and os.path.isdir(self.dir)

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def link(self, *parts):
        return "[[%s]]" % "/".join(parts)

    def task_link(self, task_id):
        return self.link(self.wiki, "tasks", task_id)

    def decision_link(self, qid):
        return self.link(self.wiki, "decisions", qid)

    def project_link(self, project_id):
        return self.link(self.wiki, "projects", project_id)

    # ---- 전체 다시 그리기 ----
    def sync(self, store):
        """DB 상태로 노트를 다시 그린다. 바뀐 노트 경로 목록을 돌려준다."""
        if not self.enabled:
            return []
        changed = []
        with file_lock(os.path.join(config.home(), "vault.lock")):
            tasks = store.tasks()
            questions = store.questions()
            for t in tasks:
                if self._task_note(store, t, [q for q in questions if q["task_id"] == t["task_id"]]):
                    changed.append(self.p(self.wiki, "tasks", t["task_id"] + ".md"))
            for q in questions:
                if self._decision_note(q, store.task(q["task_id"])):
                    changed.append(self.p(self.wiki, "decisions", q["question_id"] + ".md"))
            projects = sorted({t["project_id"] for t in tasks})
            for pid in projects:
                if self._project_note(store, pid, [t for t in tasks if t["project_id"] == pid],
                                      [q for q in questions if q["project_id"] == pid]):
                    changed.append(self.p(self.wiki, "projects", pid + ".md"))
            if self._base_file():
                changed.append(self.p(self.wiki, "AO 현황.base"))
            if self._index(projects):
                changed.append(self.p(self.wiki, "index.md"))
            self._raw_entries(store)
        return changed

    def _task_note(self, store, t, qs):
        tid = t["task_id"]
        events = store.events(task_id=tid)
        ver = json.loads(t["verification"]) if t.get("verification") else None
        days = sorted({e["created_at"][:10] for e in events} | {t["created_at"][:10]})
        owned = {
            "type": "ao-task",
            "task_id": tid,
            "project": t["project_id"],
            "project_note": self.project_link(t["project_id"]),
            "status": t["status"],
            "status_ko": STATUS_KO.get(t["status"], t["status"]),
            "title": t["title"],
            "supervisor_session": t.get("supervisor_session") or "",
            "worker_sessions": json.loads(t.get("worker_sessions") or "[]"),
            "worker_agent": t.get("worker_agent") or "",
            "done_criteria": t["done_criteria"],
            "verify_cmd": t.get("verify_cmd") or "",
            "verified": bool(t.get("verified")) if t.get("verified") is not None else None,
            "workspace": t.get("workspace") or "",
            "commit": t.get("commit_sha") or "",
            "checked_at": t.get("checked_at") or "",
            "created": t["created_at"],
            "updated": t["updated_at"],
            "decisions": [self.decision_link(q["question_id"]) for q in qs],
            "daily": [self.link(self.raw, d) for d in days],
            "tags": ["ao/task"],
        }
        lines = ["## 요청", "", t["request"].strip(), "", "## 완료 조건", "", t["done_criteria"].strip()]
        if t.get("verify_cmd"):
            lines += ["", "검증 명령: `%s`" % t["verify_cmd"]]
        lines += ["", "## 실행 정보", "",
                  "| 항목 | 값 |", "|---|---|",
                  "| 프로젝트 | %s |" % self.project_link(t["project_id"]),
                  "| Supervisor | `%s` |" % (t.get("supervisor_session") or "-"),
                  "| Worker | %s |" % (", ".join("`%s`" % w for w in json.loads(t.get("worker_sessions") or "[]")) or "-"),
                  "| 상태 | %s (AO 확인 %s) |" % (STATUS_KO.get(t["status"], t["status"]), short_time(t.get("checked_at")) or "-"),
                  "| 결과물 위치 | %s |" % (("`%s`" % t["workspace"]) if t.get("workspace") else "-"),
                  "| 커밋 | %s |" % (("`%s`" % t["commit_sha"]) if t.get("commit_sha") else "-")]
        lines += ["", "## 검증 (Main 이 직접 확인한 것)", ""]
        if ver:
            lines += ["- 확인 시각: %s" % short_time(ver.get("at")),
                      "- 명령: `%s` (작업 공간 `%s`)" % (ver.get("cmd"), ver.get("cwd")),
                      "- 결과: exit %s → %s" % (ver.get("exit_code"), "통과" if ver.get("passed") else "실패")]
            if ver.get("commits"):
                lines.append("- 기준 브랜치 뒤 커밋: " + ", ".join("`%s`" % c for c in ver["commits"]))
            if ver.get("diffstat"):
                lines.append("- 변경 파일: " + md_cell(ver["diffstat"], 300))
            if ver.get("dirty"):
                lines.append("- 커밋 안 된 변경: " + md_cell(ver["dirty"], 300))
            if ver.get("output_tail"):
                lines += ["", "```text", ver["output_tail"].strip()[-1500:], "```"]
        else:
            lines.append("- 아직 검증하지 않았습니다.")
        lines += ["", "## 보고만 받은 것 (검증 전)", ""]
        lines.append(t["reported_summary"].strip() if t.get("reported_summary") else "- 없음")
        if qs:
            lines += ["", "## 질문과 결정", "", "| 질문 | 상태 | 내용 |", "|---|---|---|"]
            for q in qs:
                lines.append("| %s | %s | %s |" % (self.decision_link(q["question_id"]), q["status"], md_cell(q["text"], 100)))
        lines += ["", "## 진행 기록", "", "| 시각 | 출처 | 종류 | 구분 | 내용 |", "|---|---|---|---|---|"]
        for e in events:
            lines.append("| %s | %s | %s | %s | %s |" % (short_time(e["created_at"]), md_cell(e["source"], 40), e["kind"],
                                                     "확인" if e["source"].startswith("main") else "보고",
                                                     md_cell(e["summary"], 200)))
        return upsert_note(self.p(self.wiki, "tasks", tid + ".md"), "%s %s" % (tid, t["title"]), owned,
                           [("task", "\n".join(lines))])

    def _decision_note(self, q, t):
        qid = q["question_id"]
        opts = json.loads(q["options"]) if q.get("options") else []
        owned = {
            "type": "ao-decision",
            "decision_id": qid,
            "task_id": q["task_id"],
            "task_note": self.task_link(q["task_id"]),
            "project": q["project_id"],
            "status": q["status"],
            "asked_by": q.get("asked_by") or "",
            "worker_session": q.get("worker_session") or "",
            "asked_at": q["created_at"],
            "answered_at": q.get("answered_at") or "",
            "delivered_at": q.get("delivered_at") or "",
            "daily": [self.link(self.raw, q["created_at"][:10])],
            "tags": ["ao/decision"],
        }
        lines = ["## 질문", "", q["text"].strip(), ""]
        if opts:
            lines += ["선택지:", ""] + ["- %s" % o for o in opts] + [""]
        lines += ["- 작업: %s%s" % (self.task_link(q["task_id"]), (" " + t["title"]) if t else ""),
                  "- 묻은 쪽: Worker `%s` → Supervisor `%s` → Main" % (q.get("worker_session") or "-", q.get("asked_by") or "-"),
                  "", "## 결정", ""]
        if q.get("answer"):
            lines += ["- 답: " + q["answer"].strip(), "- 이유: " + (q.get("reason") or "기록 없음").strip(),
                      "- 답한 시각: " + short_time(q.get("answered_at")),
                      "- Supervisor 전달: " + (("확인 " + short_time(q["delivered_at"])) if q.get("delivered_at") else "확인 전")]
        else:
            lines.append("- 사용자 답을 기다립니다. 이 노트의 상태나 체크박스를 바꿔도 작업은 재개되지 않습니다. Main 에서 답하세요.")
        title = "%s %s" % (qid, md_cell(q["text"], 50))
        return upsert_note(self.p(self.wiki, "decisions", qid + ".md"), title, owned, [("decision", "\n".join(lines))])

    def _project_note(self, store, pid, tasks, qs):
        sup = store.supervisor(pid) or {}
        open_tasks = [t for t in tasks if t["status"] not in ("verified_done", "cancelled")]
        open_q = [q for q in qs if q["status"] == "open"]
        owned = {
            "ao_project": pid,
            "ao_supervisor": sup.get("session_id") or "",
            "ao_open_tasks": len(open_tasks),
            "ao_open_questions": len(open_q),
            "ao_checked_at": max([t.get("checked_at") or "" for t in tasks] or [""]),
            "ao_updated": now(),
        }
        if not os.path.exists(self.p(self.wiki, "projects", pid + ".md")):
            owned = dict({"type": "ao-project", "tags": ["ao/project"]}, **owned)
        lines = ["## AO 작업 현황", "",
                 "Supervisor: `%s` · 진행 중 %d · 판단 대기 %d" % (sup.get("session_id") or "-", len(open_tasks), len(open_q)),
                 "", "| 작업 | 상태 | 검증 | Worker | AO 확인 |", "|---|---|---|---|---|"]
        for t in reversed(tasks):
            lines.append("| %s %s | %s | %s | %s | %s |" % (
                self.task_link(t["task_id"]), md_cell(t["title"], 60), STATUS_KO.get(t["status"], t["status"]),
                {1: "통과", 0: "실패"}.get(t.get("verified"), "-"),
                ", ".join(json.loads(t.get("worker_sessions") or "[]")) or "-", short_time(t.get("checked_at")) or "-"))
        if open_q:
            lines += ["", "### 판단 대기", ""] + ["- %s %s (%s)" % (self.decision_link(q["question_id"]),
                                                                md_cell(q["text"], 100), self.task_link(q["task_id"]))
                                                   for q in open_q]
        done_q = [q for q in qs if q["status"] != "open"]
        if done_q:
            lines += ["", "### 최근 결정", ""] + ["- %s %s → %s" % (self.decision_link(q["question_id"]),
                                                               md_cell(q["text"], 60), md_cell(q.get("answer"), 80))
                                                  for q in done_q[-10:]]
        return upsert_note(self.p(self.wiki, "projects", pid + ".md"), pid, owned, [("ao-project", "\n".join(lines))])

    def _base_file(self):
        w = self.wiki
        text = "\n".join([
            "# aohub 가 만든 보기입니다. 파일을 고치면 다음 갱신 때 되돌아갑니다. 바꾸려면 복사해서 쓰세요.",
            "filters:",
            "  or:",
            '    - file.inFolder("%s/tasks")' % w,
            '    - file.inFolder("%s/decisions")' % w,
            "views:",
            "  - type: table",
            "    name: 프로젝트 현황",
            "    filters:",
            "      and:",
            '        - type == "ao-task"',
            '        - status != "cancelled"',
            "    groupBy:",
            "      property: project",
            "      direction: ASC",
            "    order:",
            "      - file.name",
            "      - title",
            "      - status_ko",
            "      - verified",
            "      - worker_sessions",
            "      - checked_at",
            "  - type: table",
            "    name: 판단 대기",
            "    filters:",
            "      and:",
            '        - type == "ao-decision"',
            '        - status == "open"',
            "    order:",
            "      - file.name",
            "      - project",
            "      - task_note",
            "      - asked_by",
            "      - asked_at",
            "  - type: table",
            "    name: 완료 작업",
            "    filters:",
            "      and:",
            '        - type == "ao-task"',
            '        - status == "verified_done"',
            "    order:",
            "      - file.name",
            "      - project",
            "      - title",
            "      - commit",
            "      - updated",
            "  - type: table",
            "    name: 결정 기록",
            "    filters:",
            "      and:",
            '        - type == "ao-decision"',
            "    order:",
            "      - file.name",
            "      - project",
            "      - status",
            "      - answered_at",
            "",
        ])
        return write_if_changed(self.p(self.wiki, "AO 현황.base"), text)

    def _index(self, projects):
        path = self.p(self.wiki, "index.md")
        text = read(path)
        if text is None:
            text = "# 위키 목차\n"
        lines = ["## AO 작업", "", "- 보기: [[%s/AO 현황.base|AO 현황]] (프로젝트 현황 · 판단 대기 · 완료 작업 · 결정 기록)"]
        lines += ["- %s" % self.project_link(p) for p in projects]
        new = replace_block(text, "ao-index", "\n".join(lines))
        return write_if_changed(path, new)

    def _raw_entries(self, store):
        """일일 로그에 남길 이벤트(완료 검증, 질문, 결정)를 한 번씩 덧붙인다."""
        for e in store.events():
            if e["kind"] not in ("verified", "verify_failed", "question", "decision"):
                continue
            key = "raw:" + e["event_key"]
            if store.db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
                continue
            t = store.task(e["task_id"]) if e["task_id"] else None
            if not t:
                continue
            payload = json.loads(e["payload"]) if e.get("payload") else {}
            if e["kind"] in ("question", "decision") and not payload.get("question_id"):
                continue  # Supervisor 보고 원문. 일일 로그는 질문 등록(q:…:raised) 이벤트로 한 번만 쓴다
            ts =_dt.datetime.fromisoformat(e["created_at"]).astimezone(KST)
            entry = {"date": ts.strftime("%Y-%m-%d"), "time": ts.strftime("%H:%M"),
                     "agent": self.cfg.get("raw_agent") or "Claude", "project": t["project_id"]}
            tl = self.task_link(t["task_id"])
            if e["kind"] in ("verified", "verify_failed"):
                ok = e["kind"] == "verified"
                entry.update(status="완료" if ok else "부분완료",
                             title="%s %s" % (t["task_id"], t["title"]),
                             result="%s AO Supervisor %s 가 Worker %s(%s)에게 맡긴 작업. 결과물 %s 커밋 %s." % (
                                 tl, t.get("supervisor_session") or "-",
                                 ", ".join(json.loads(t.get("worker_sessions") or "[]")) or "-",
                                 t.get("worker_agent") or "-", t.get("workspace") or "-", t.get("commit_sha") or "-"),
                             check="Main 직접 실행: %s → exit %s(%s). Worker·Supervisor 보고 내용은 작업 노트 '보고만 받은 것'에 따로 둠" % (
                                 payload.get("cmd"), payload.get("exit_code"), "통과" if ok else "실패"),
                             next="없음" if ok else "검증 실패 원인 확인")
            elif e["kind"] == "question":
                q = store.question(payload.get("question_id")) if payload.get("question_id") else None
                entry.update(status="보류", title="%s 판단 대기 %s" % (payload.get("question_id"), t["task_id"]),
                             result="%s 에서 질문: %s" % (tl, (q or {}).get("text", e.get("summary") or "")),
                             check="Worker→Supervisor→Main 경로로 받음. 사용자 답 대기",
                             next="사용자가 Main 에서 답하면 %s 로 기록" % self.decision_link(payload.get("question_id")))
            else:
                q = store.question(payload.get("question_id")) or {}
                entry.update(status="완료", title="%s 결정 %s" % (q.get("question_id"), t["task_id"]),
                             result="%s 질문 '%s' 에 답: %s" % (self.decision_link(q.get("question_id")),
                                                            q.get("text", ""), q.get("answer", "")),
                             check="Supervisor %s 에 ao send 전달%s" % (
                                 t.get("supervisor_session"), ", transcript 도착 확인" if payload.get("confirmed") else ", 도착 확인 전"),
                             next="작업 재개 후 결과 보고 대기", reason=q.get("reason") or "")
            append_raw(self.cfg, self.dir, entry, raw_entry_id(e["event_key"]))
            with store.tx() as db:
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, now()))
