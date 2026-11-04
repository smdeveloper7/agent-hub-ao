"""aohub 명령. scripts/aohub.py 가 부른다.

Main 이 쓰는 명령: init, doctor, projects, setup-project, assign, status, questions, answer, verify, notices, ack,
                  resume, watch, poll, sync, daily
Supervisor 가 쓰는 명령: sup-report
"""
import argparse
import datetime as _dt
import json
import os
import sys
import time

from . import config, inbox
from .ao import AO, AOError
from .bridge import Bridge, aohub_cmd
from .store import KST, Store, now
from .vault import STATUS_KO, Vault, file_lock, read, replace_block, write_if_changed


def out(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 명령
def cmd_init(a):
    cfg = config.load()
    for k in ("vault_dir", "raw_script", "ao_bin", "python", "raw_agent"):
        v = getattr(a, k, None)
        if v is not None:
            cfg[k] = v.replace("\\", "/")
    if not cfg.get("python"):
        cfg["python"] = sys.executable.replace("\\", "/")
    if not cfg.get("ao_bin"):
        found = config.find_ao(cfg)
        if found:
            cfg["ao_bin"] = found.replace("\\", "/")
    config.save(cfg)
    Store().close()
    print("상태 폴더: %s" % config.home())
    print(json.dumps(cfg, ensure_ascii=False, indent=2))


def cmd_doctor(a):
    cfg = config.load()
    rows = []
    ao_bin = config.find_ao(cfg)
    rows.append(("ao", bool(ao_bin), ao_bin or "찾지 못함"))
    ao = AO(cfg)
    st = ao.status() if ao_bin else {}
    rows.append(("ao daemon", st.get("state") == "ready", st.get("state")))
    rows.append(("state", os.path.exists(os.path.join(config.home(), "state.sqlite3")), config.home()))
    v = Vault(cfg)
    rows.append(("vault", v.enabled, v.dir if cfg.get("vault_dir") else "설정 안 됨"))
    rows.append(("raw_script", bool(cfg.get("raw_script")) and os.path.exists(cfg["raw_script"]), cfg.get("raw_script") or "내장 기록기"))
    rows.append(("main inbox", inbox.available(), inbox.address() or "이 셸에는 없음(Main 세션 안에서만 보임)"))
    rows.append(("main session", bool(os.environ.get("CLAUDE_CODE_SESSION_ID")), os.environ.get("CLAUDE_CODE_SESSION_ID") or "-"))
    s = Store()
    m = s.active_main()
    rows.append(("watcher", bool(m and pid_alive(m.get("watcher_pid"))),
                 ("Main %s, watcher pid %s" % (m["session_id"], m.get("watcher_pid"))) if m else "등록된 Main 없음"))
    ok = True
    for name, good, detail in rows:
        print("%s %-13s %s" % ("PASS" if good else "WARN", name, detail))
        ok = ok and (good or name in ("main inbox", "main session", "watcher", "raw_script"))
    return 0 if ok else 1


def cmd_projects(a):
    b = Bridge()
    rows = []
    for p in b.ao.projects():
        pid = p.get("id")
        orch = [o for o in b.ao.orchestrators(pid) if not o.get("isTerminated")]
        cfgp = (b.ao.project(pid) or {}).get("config") or {}
        rows.append({"project": pid, "path": p.get("path"),
                     "supervisor": ", ".join("%s(%s)" % (o["id"], o.get("activity")) for o in orch) or "-",
                     "worker_agent": (cfgp.get("worker") or {}).get("agent") or p.get("agent"),
                     "aohub_rules": "<!-- aohub:begin -->" in (cfgp.get("orchestratorRules") or ""),
                     "open_tasks": len(b.store.tasks(pid, open_only=True))})
    if a.json:
        return out(rows, True)
    for r in rows:
        print("%-14s supervisor=%-24s worker=%-12s rules=%s open=%d  %s" % (
            r["project"], r["supervisor"], r["worker_agent"], "Y" if r["aohub_rules"] else "N", r["open_tasks"], r["path"]))


def cmd_setup_project(a):
    b = Bridge()
    changed = b.ensure_project_rules(a.project)
    print("%s orchestratorRules %s" % (a.project, "갱신" if changed else "이미 최신"))


def cmd_assign(a):
    b = Bridge()
    t, created, delivery = b.assign(a.project, a.title, a.request, a.done, a.verify, a.worker_agent, a.key)
    res = {"task_id": t["task_id"], "new_task": created, "status": t["status"], "supervisor": t.get("supervisor_session"),
           "delivery": delivery}
    if delivery not in ("confirmed", "existing"):
        res["note"] = "배정 도착을 확인하지 못했습니다. 다시 실행하면 먼저 transcript 를 확인하고 필요할 때만 다시 보냅니다."
    out(res, True)


def task_view(b, t, live):
    v = {"task_id": t["task_id"], "project": t["project_id"], "title": t["title"],
         "status": t["status"], "status_ko": STATUS_KO.get(t["status"], t["status"]),
         "supervisor": t.get("supervisor_session"), "workers": json.loads(t.get("worker_sessions") or "[]"),
         "done_criteria": t["done_criteria"], "verified": t.get("verified"), "workspace": t.get("workspace"),
         "commit": t.get("commit_sha"), "checked_at": t.get("checked_at"),
         "open_questions": [q["question_id"] for q in b.store.questions(status="open", task_id=t["task_id"])]}
    if live:
        ao_live = {}
        for sid in [t.get("supervisor_session")] + v["workers"]:
            if not sid:
                continue
            try:
                s = b.ao.session(sid).get("session") or {}
                ao_live[sid] = {"kind": s.get("kind"), "status": s.get("status"), "activity": (s.get("activity") or {}).get("state"),
                                "terminated": s.get("isTerminated"), "branch": s.get("branch"),
                                "commits": (s.get("branchState") or {}).get("commits")}
            except AOError as e:
                ao_live[sid] = {"error": str(e)[:200]}
        v["ao_live"] = ao_live
        v["ao_checked_at"] = now()
    if b.vault.enabled:
        v["note"] = b.vault.p(b.vault.wiki, "tasks", t["task_id"] + ".md")
    return v


def cmd_status(a):
    b = Bridge()
    if a.task:
        t = b.store.task(a.task)
        if not t:
            print("모르는 작업 ID: %s" % a.task)
            return 1
        return out(task_view(b, t, not a.no_live), True)
    tasks = b.store.tasks(a.project, open_only=not a.all)
    out([task_view(b, t, not a.no_live) for t in tasks], True)


def cmd_questions(a):
    s = Store()
    qs = s.questions(None if a.all else "open")
    out([{k: q[k] for k in ("question_id", "task_id", "project_id", "status", "text", "options", "worker_session",
                             "asked_by", "answer", "reason", "created_at")} for q in qs], True)


def cmd_answer(a):
    out(Bridge().answer(a.question, a.text, a.reason), True)


def cmd_sup_report(a):
    res = Bridge().sup_report(a.task, a.kind, a.text, worker=a.worker, options=a.option or None, event_id=a.event_id)
    if a.kind == "question":
        print("질문 %s 를 Main 에 %s. worker 에게 답을 기다리게 하세요. 답은 `[답변] %s` 로 옵니다."
              % (res["question_id"], "올렸습니다" if res["new"] else "이미 올렸습니다(다시 기록하지 않음)", res["question_id"]))
    else:
        print("%s %s 보고를 기록했습니다%s." % (a.task, a.kind, "" if res["new"] else "(이미 받은 보고라 다시 기록하지 않음)"))


def cmd_tell(a):
    out(Bridge().tell(a.task, a.text), True)


def cmd_worker_state(a):
    out(Bridge().worker_state(a.session), True)


def cmd_verify(a):
    out(Bridge().verify(a.task), True)


def cmd_notices(a):
    b = Bridge()
    out([dict(n, text=b.render_notice(n)) for n in b.store.notices(unacked=not a.all)], True)


def cmd_ack(a):
    s = Store()
    for nid in a.notice:
        print("%s %s" % (nid, "처리 기록" if s.ack_notice(nid) else "이미 처리했거나 없음"))


def cmd_poll(a):
    b = Bridge()
    n = b.poll_once(auto_verify=not a.no_verify)
    print("새 이벤트 %d건" % n)


def cmd_sync(a):
    for p in Bridge().sync_vault():
        print("갱신 %s" % p)


def cmd_resume(a):
    """Main 이 새로 열렸을 때: 이 세션을 Main 으로 등록하고, 진행 중 작업과 미처리 질문·알림을 AO 에 다시 확인해 보여 준다."""
    b = Bridge()
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        b.store.attach_main(sid, pid=int(os.environ["CLAUDE_PID"]) if os.environ.get("CLAUDE_PID") else None)
    try:
        b.poll_once(auto_verify=False)
    except AOError as e:
        print("AO 확인 실패: %s" % e)
    tasks = b.store.tasks(open_only=True)
    res = {"main_session": sid, "open_tasks": [task_view(b, t, True) for t in tasks],
           "open_questions": [{"question_id": q["question_id"], "task_id": q["task_id"], "text": q["text"]}
                              for q in b.store.questions("open")],
           "unacked_notices": [n["notice_id"] for n in b.store.notices()],
           "notes_to_read": []}
    if b.vault.enabled:
        for pid in sorted({t["project_id"] for t in tasks}):
            res["notes_to_read"].append(b.vault.p(b.vault.wiki, "projects", pid + ".md"))
        for q in b.store.questions():
            if q["task_id"] in {t["task_id"] for t in tasks} or q["status"] != "open":
                res["notes_to_read"].append(b.vault.p(b.vault.wiki, "decisions", q["question_id"] + ".md"))
    m = b.store.active_main()
    res["watcher_running"] = bool(m and m["session_id"] == sid and pid_alive(m.get("watcher_pid")))
    if not res["watcher_running"]:
        res["next"] = "감시 프로세스를 이 세션에서 백그라운드로 시작하세요: %s watch" % aohub_cmd(b.cfg)
    out(res, True)


def log_line(msg):
    path = os.path.join(config.home(), "logs", "watch.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("%s %s\n" % (now(), msg))


def cmd_watch(a):
    """Main 세션 안에서 시작한다. AO 를 주기적으로 읽고, 알림을 Main inbox 로 보낸다."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid or not inbox.available():
        print("Main Claude Code 세션 안에서 실행해야 합니다(CLAUDE_CODE_SESSION_ID, CLAUDE_CODE_MESSAGING_SOCKET/TOKEN 필요).")
        return 2
    main_pid = int(os.environ["CLAUDE_PID"]) if os.environ.get("CLAUDE_PID") else None
    lock = os.path.join(config.home(), "locks", "watch-%s.lock" % sid)
    try:
        ctx = file_lock(lock, timeout=1)
        ctx.__enter__()
    except TimeoutError:
        print("이 Main 세션의 감시 프로세스가 이미 실행 중입니다.")
        return 0
    b = Bridge()
    b.store.attach_main(sid, pid=main_pid, watcher_pid=os.getpid())
    log_line("watch start session=%s pid=%s main_pid=%s" % (sid, os.getpid(), main_pid))
    print("감시 시작: Main %s, 주기 %ss" % (sid, b.cfg.get("poll_seconds")), flush=True)
    period = float(a.interval or b.cfg.get("poll_seconds") or 5)
    end = time.time() + a.max_seconds if a.max_seconds else None
    try:
        while True:
            m = b.store.active_main()
            if not m or m["session_id"] != sid:
                log_line("watch stop: 다른 Main 이 등록됨 (%s)" % (m or {}).get("session_id"))
                print("다른 Main 세션이 등록되어 감시를 끝냅니다.", flush=True)
                return 0
            if main_pid and not pid_alive(main_pid):
                log_line("watch stop: Main 프로세스 종료")
                return 0
            try:
                n = b.poll_once(auto_verify=True)
                sent = b.deliver_notices(sid)
                b.store.touch_main(sid)
                if n or sent:
                    log_line("poll new=%d sent=%s" % (n, ",".join(sent)))
                    print("%s 새 이벤트 %d, 알림 %s" % (now()[11:19], n, ",".join(sent) or "-"), flush=True)
            except inbox.InboxUnavailable as e:
                log_line("inbox 오류: %s" % e)
                if main_pid and not pid_alive(main_pid):
                    return 0
            except (AOError, OSError) as e:
                log_line("poll 오류: %s" % e)
            if end and time.time() > end:
                return 0
            time.sleep(period)
    finally:
        ctx.__exit__(None, None, None)


def cmd_daily(a):
    """오늘(또는 --date) AO 작업을 정리한다: 노트 다시 그리기, 요약 출력, wiki/log.md 한 줄."""
    b = Bridge()
    day = a.date or _dt.datetime.now(KST).strftime("%Y-%m-%d")
    changed = b.sync_vault()
    evs = [e for e in b.store.events() if e["created_at"].startswith(day)]
    task_ids = sorted({e["task_id"] for e in evs if e["task_id"]})
    tasks = [b.store.task(t) for t in task_ids]
    qs = [q for q in b.store.questions() if q["created_at"].startswith(day) or (q.get("answered_at") or "").startswith(day)]
    summary = {"date": day, "tasks": [{"task_id": t["task_id"], "project": t["project_id"], "title": t["title"],
                                       "status": STATUS_KO.get(t["status"], t["status"]), "verified": t.get("verified")}
                                      for t in tasks],
               "decisions": [{"question_id": q["question_id"], "task_id": q["task_id"], "status": q["status"],
                              "answer": q.get("answer"), "reason": q.get("reason")} for q in qs],
               "events": len(evs), "notes_changed": changed}
    if b.vault.enabled and not a.no_log:
        path = b.vault.p(b.vault.wiki, "log.md")
        docs = sorted({"projects/%s" % t["project_id"] for t in tasks} | {"tasks/%s" % t["task_id"] for t in tasks} |
                      {"decisions/%s" % q["question_id"] for q in qs})
        line = "- %s | %s | aohub [[%s/%s]] AO 기록 | %s" % (
            _dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"), b.cfg.get("raw_agent") or "Claude", b.vault.raw, day,
            ", ".join(docs) or "변경 없음")
        with file_lock(os.path.join(config.home(), "vault.lock")):
            text = read(path) or "# 위키 갱신 이력\n"
            text = text if text.endswith("\n") else text + "\n"
            write_if_changed(path, text + line + "\n")
        summary["log_line"] = line
    out(summary, True)


# ---------------------------------------------------------------- 진입
def build():
    p = argparse.ArgumentParser(prog="aohub", description="agent-hub-ao ↔ Agent Orchestrator 연동")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="상태 폴더와 설정 만들기")
    s.add_argument("--vault-dir", dest="vault_dir")
    s.add_argument("--raw-script", dest="raw_script")
    s.add_argument("--ao-bin", dest="ao_bin")
    s.add_argument("--python")
    s.add_argument("--raw-agent", dest="raw_agent")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("doctor", help="연결 점검").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("projects", help="AO 프로젝트와 Supervisor")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("setup-project", help="프로젝트 orchestratorRules 에 연동 규칙 넣기")
    s.add_argument("project")
    s.set_defaults(fn=cmd_setup_project)

    s = sub.add_parser("assign", help="작업 배정")
    s.add_argument("--project", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--request", required=True)
    s.add_argument("--done", required=True, help="완료 조건")
    s.add_argument("--verify", help="worker 작업 공간에서 Main 이 다시 돌릴 검증 명령")
    s.add_argument("--worker-agent", dest="worker_agent", help="claude-code, codex 등")
    s.add_argument("--key", help="같은 요청을 다시 보내도 새 작업을 만들지 않게 하는 키")
    s.set_defaults(fn=cmd_assign)

    s = sub.add_parser("status", help="작업 상태(AO 에 다시 확인)")
    s.add_argument("task", nargs="?")
    s.add_argument("--project")
    s.add_argument("--all", action="store_true")
    s.add_argument("--no-live", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("questions", help="판단 대기 질문")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_questions)

    s = sub.add_parser("answer", help="사용자 답을 Supervisor 에 전달")
    s.add_argument("question")
    s.add_argument("--text", required=True)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_answer)

    s = sub.add_parser("sup-report", help="(Supervisor 용) Main 에 보고")
    s.add_argument("--task", required=True)
    s.add_argument("--kind", required=True, choices=("worker", "progress", "question", "done", "blocked"))
    s.add_argument("--text", required=True)
    s.add_argument("--worker")
    s.add_argument("--option", action="append")
    s.add_argument("--event-id", dest="event_id")
    s.set_defaults(fn=cmd_sup_report)

    s = sub.add_parser("tell", help="같은 작업의 Supervisor 에 후속 지시")
    s.add_argument("task")
    s.add_argument("--text", required=True)
    s.set_defaults(fn=cmd_tell)

    s = sub.add_parser("worker-state", help="(Supervisor 용) worker 의 실제 상태(idle 에 가려진 turn 실패 포함)")
    s.add_argument("session")
    s.set_defaults(fn=cmd_worker_state)

    s = sub.add_parser("verify", help="worker 작업 공간에서 검증 명령 실행")
    s.add_argument("task")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("notices", help="Main 에 보낼 알림")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_notices)

    s = sub.add_parser("ack", help="알림 처리 기록")
    s.add_argument("notice", nargs="+")
    s.set_defaults(fn=cmd_ack)

    s = sub.add_parser("poll", help="AO 를 한 번 읽어 기록")
    s.add_argument("--no-verify", action="store_true")
    s.set_defaults(fn=cmd_poll)

    sub.add_parser("sync", help="Obsidian 노트 다시 그리기").set_defaults(fn=cmd_sync)
    sub.add_parser("resume", help="Main 재시작 뒤 복구").set_defaults(fn=cmd_resume)

    s = sub.add_parser("watch", help="감시하며 Main inbox 로 알림(Main 세션 안에서)")
    s.add_argument("--interval", type=float)
    s.add_argument("--max-seconds", dest="max_seconds", type=float)
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("daily", help="오늘 AO 작업 정리")
    s.add_argument("--date")
    s.add_argument("--no-log", action="store_true")
    s.set_defaults(fn=cmd_daily)
    return p


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    a = build().parse_args(argv)
    try:
        rc = a.fn(a)
    except (AOError, ValueError) as e:
        print("오류: %s" % e, file=sys.stderr)
        return 1
    return rc or 0
