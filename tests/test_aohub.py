"""aohub Mock 시험. 가짜 AO 로 상태 기록, 중복 방지, 전달 재시도, Obsidian 노트 갱신을 본다.

실제 AO 와 에이전트를 쓰는 검증은 docs/ao-bridge.md 의 '실제 실행 검증' 을 따른다.
실행: python tests/test_aohub.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from aohub import config  # noqa: E402
from aohub.ao import AOError  # noqa: E402
from aohub.bridge import Bridge  # noqa: E402
from aohub.store import Store  # noqa: E402
from aohub.vault import Vault, merge_frontmatter, replace_block  # noqa: E402


class FakeAO:
    def __init__(self, project_path):
        self.project_path = project_path
        self.orchs = []
        self.sessions_ = {}
        self.sent = []  # 데몬이 기록한 전달
        self.attempts = []  # send 호출 전부
        self.spawned = []
        self.reports_ = []
        self.fail_next_send = None  # None | "uncertain" | "failed"
        self.deliver_on_uncertain = True
        self.transcripts = {}
        self.cfg_obj = {"worker": {"agent": "claude-code"}}
        self.turns = {}

    def ready(self):
        return True

    def project(self, pid):
        return {"id": pid, "path": self.project_path, "config": self.cfg_obj}

    def set_config(self, pid, obj):
        self.cfg_obj = obj

    def orchestrators(self, pid=None):
        return [o for o in self.orchs if not pid or o["projectId"] == pid]

    def spawn(self, pid, name, prompt=None, kind="worker", agent=None):
        sid = "%s-%d" % (pid, len(self.spawned) + 1)
        self.spawned.append((sid, kind, name))
        if kind == "orchestrator":
            self.orchs.append({"id": sid, "projectId": pid, "isTerminated": False, "createdAt": str(len(self.spawned))})
        self.sessions_[sid] = {"id": sid, "projectId": pid, "role": kind, "displayName": name, "status": "idle",
                               "activity": "idle", "isTerminated": False, "branch": "ao/%s/root" % sid}
        self.transcripts[sid] = []
        return sid

    def session(self, sid, transcript=0, cap=1000):
        s = dict(self.sessions_.get(sid) or {"id": sid, "isTerminated": True})
        s.setdefault("kind", s.get("role"))
        res = {"session": s}
        if transcript:
            res["transcript"] = {"controller": "ready", "turns": self.turns.get(sid, []),
                                 "entries": [{"role": "user", "text": t} for t in self.transcripts.get(sid, [])]}
        return res

    def send(self, sid, body, client_message_id=None):
        mode, self.fail_next_send = self.fail_next_send, None
        self.attempts.append((sid, body, client_message_id))
        if mode == "failed":
            raise AOError("send 실패", rc=1)
        if mode == "uncertain":
            if self.deliver_on_uncertain:
                self.sent.append((sid, body, client_message_id))
                self.transcripts.setdefault(sid, []).append(body)
            raise AOError("시간 초과", uncertain=True)
        self.sent.append((sid, body, client_message_id))
        self.transcripts.setdefault(sid, []).append(body)
        return "sent"

    def recover_send(self, sid, client_message_id):
        if not any(c == client_message_id for _, _, c in self.sent):
            raise AOError("기록 없음", rc=1)
        return "recovered"

    def sessions(self, pid=None, include_terminated=False):
        return [s for s in self.sessions_.values() if not pid or s["projectId"] == pid]

    def reports(self, pid):
        return list(self.reports_)

    def worktree(self, project_path, branch):
        return self.project_path


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd] + list(args), check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aohub-test-")
        os.environ["AOHUB_HOME"] = os.path.join(self.tmp, "home")
        self.vault_dir = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault_dir, "wiki"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "init")
        self.cfg = dict(config.DEFAULTS, vault_dir=self.vault_dir, python=sys.executable)
        self.ao = FakeAO(self.repo)
        self.store = Store()
        self.b = Bridge(cfg=self.cfg, store=self.store, ao=self.ao, vault=Vault(self.cfg))
        self.b.confirm_seconds = 0
        import aohub.bridge as br
        self._sleep = br.time.sleep
        br.time.sleep = lambda s: None

    def tearDown(self):
        import aohub.bridge as br
        br.time.sleep = self._sleep
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AOHUB_HOME", None)

    def assign(self, **kw):
        args = dict(project_id="demo", title="subtract 추가", request="subtract 함수 추가",
                    done_criteria="시험 통과와 커밋", verify_cmd="git log -1")
        args.update(kw)
        return self.b.assign(**args)


class AssignTest(Base):
    def test_same_request_twice_makes_one_task_and_one_send(self):
        t1, c1, d1 = self.assign()
        t2, c2, d2 = self.assign()
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(t1["task_id"], t2["task_id"])
        self.assertEqual(d1, "confirmed")
        self.assertEqual(len([s for s in self.ao.spawned if s[1] == "orchestrator"]), 1)
        self.assertEqual(len(self.ao.sent), 1)
        self.assertIn("<!-- aohub:begin -->", self.ao.cfg_obj["orchestratorRules"])
        self.assertEqual(self.ao.cfg_obj["worker"], {"agent": "claude-code"})  # 다른 설정 보존

    def test_existing_supervisor_is_reused(self):
        self.ao.orchs.append({"id": "demo-sup", "projectId": "demo", "isTerminated": False, "createdAt": "0"})
        self.ao.sessions_["demo-sup"] = {"id": "demo-sup", "projectId": "demo", "role": "orchestrator", "isTerminated": False}
        t, _, _ = self.assign()
        self.assertEqual(t["supervisor_session"], "demo-sup")
        self.assertEqual(self.ao.spawned, [])

    def test_uncertain_send_is_checked_before_resend(self):
        self.ao.fail_next_send = "uncertain"
        t, _, d = self.assign()
        self.assertEqual(d, "confirmed")  # transcript 에서 도착을 확인했다
        self.assertEqual(len(self.ao.sent), 1)
        t2, _, d2 = self.assign()
        self.assertEqual(len(self.ao.sent), 1)  # 다시 보내지 않는다

    def test_uncertain_send_not_arrived_is_resent_once(self):
        self.ao.fail_next_send = "uncertain"
        self.ao.deliver_on_uncertain = False
        t, _, d = self.assign()
        self.assertEqual(d, "uncertain")
        self.assertEqual(self.store.task(t["task_id"])["status"], "created")
        t2, _, d2 = self.assign()
        self.assertEqual(d2, "confirmed")
        self.assertEqual(len(self.ao.attempts), 2)  # 데몬 기록이 없어서 한 번 더 보냄
        self.assertEqual(len(self.ao.sent), 1)
        self.assertEqual(self.ao.attempts[0][2], self.ao.attempts[1][2])  # 같은 client-message-id
        self.assertEqual(self.store.task(t["task_id"])["status"], "assigned")


class ReportTest(Base):
    def setUp(self):
        super().setUp()
        self.t, _, _ = self.assign()
        self.tid = self.t["task_id"]

    def test_question_flow_and_duplicate_report(self):
        r1 = self.b.sup_report(self.tid, "question", "0으로 나누면?", worker="demo-9", options=["ValueError", "None"])
        r2 = self.b.sup_report(self.tid, "question", "0으로 나누면?", worker="demo-9", options=["ValueError", "None"])
        self.assertEqual(r1["question_id"], r2["question_id"])
        self.assertFalse(r2["new"])
        self.assertEqual(len(self.store.questions("open")), 1)
        self.assertEqual(len([n for n in self.store.notices() if n["kind"] == "question"]), 1)
        self.assertEqual(self.store.task(self.tid)["status"], "needs_input")
        res = self.b.answer(r1["question_id"], "ValueError", reason="조용한 실패를 피함")
        self.assertEqual(res["delivery"], "confirmed")
        q = self.store.question(r1["question_id"])
        self.assertEqual(q["status"], "delivered")
        self.assertEqual(self.store.task(self.tid)["status"], "working")
        body = self.ao.sent[-1][1]
        self.assertIn("[답변] %s" % r1["question_id"], body)
        self.assertIn("권한 승인이 아닙니다", body)
        self.b.sup_report(self.tid, "question", "0으로 나누면?", worker="demo-9", options=["ValueError", "None"])
        self.assertEqual(self.store.task(self.tid)["status"], "working")  # 같은 질문 재수신에 상태가 되돌아가지 않음
        n_sent = len(self.ao.sent)
        self.b.answer(r1["question_id"], "ValueError", reason="조용한 실패를 피함")  # 재시도
        self.assertEqual(len(self.ao.sent), n_sent)
        with self.assertRaises(ValueError):
            self.b.answer(r1["question_id"], "None")

    def test_poll_dedupes_reports_and_verifies(self):
        self.ao.sessions_["demo-9"] = {"id": "demo-9", "projectId": "demo", "role": "worker",
                                       "displayName": self.tid + " subtract", "status": "idle", "activity": "idle",
                                       "isTerminated": False, "branch": "main"}
        self.ao.reports_ = [{"id": "rpt_1", "sessionId": "demo-9", "state": "done", "note": "끝", "repeatCount": 1}]
        self.b.poll_once(auto_verify=False)
        self.b.poll_once(auto_verify=False)
        evs = [e for e in self.store.events(self.tid) if e["kind"] == "report_done"]
        self.assertEqual(len(evs), 1)
        self.assertIn("demo-9", json.loads(self.store.task(self.tid)["worker_sessions"]))
        # 보고만으로는 완료가 아니다.
        self.assertNotEqual(self.store.task(self.tid)["status"], "verified_done")
        with open(os.path.join(self.repo, "b.txt"), "w") as f:
            f.write("b\n")
        git(self.repo, "switch", "-q", "-c", "feature")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "b")
        self.ao.sessions_["demo-9"]["branch"] = "feature"
        self.b.sup_report(self.tid, "done", "b.txt 추가, 커밋함", worker="demo-9")
        self.b.poll_once(auto_verify=True)
        t = self.store.task(self.tid)
        self.assertEqual(t["status"], "verified_done")
        self.assertEqual(t["verified"], 1)
        kinds = [n["kind"] for n in self.store.notices()]
        self.assertIn("verified", kinds)
        self.b.poll_once(auto_verify=True)
        self.assertEqual(len([n for n in self.store.notices() if n["kind"] == "verified"]), 1)

    def test_notice_sent_once_per_main_session_and_redelivered_after_restart(self):
        self.b.sup_report(self.tid, "question", "선택?", worker="w1")
        n = self.store.notices()[0]
        self.assertTrue(self.store.claim_notice_send(n["notice_id"], "main-1"))
        self.assertFalse(self.store.claim_notice_send(n["notice_id"], "main-1"))
        self.assertTrue(self.store.claim_notice_send(n["notice_id"], "main-2"))  # Main 재시작 뒤 다시 보냄
        self.store.ack_notice(n["notice_id"])
        self.assertFalse(self.store.claim_notice_send(n["notice_id"], "main-3"))


class VaultMainOnlyHookTest(unittest.TestCase):
    """Obsidian 위키는 허브 Main 만 쓴다. 보기와 일지(raw)는 막지 않는다."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "hooks"))
        import vault_main_only
        self.hook = vault_main_only
        self.tmp = tempfile.mkdtemp(prefix="aohub-vmo-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, "wiki"))
        os.environ["AOHUB_HOME"] = self.home
        self.addCleanup(os.environ.pop, "AOHUB_HOME", None)
        config.save(dict(config.DEFAULTS, vault_dir=self.vault))
        s = Store()
        s.attach_main("ao:hub-1")
        s.close()

    def check(self, path, sid="claude-x", env=None):
        inp = {"session_id": sid, "cwd": self.tmp, "tool_name": "Edit", "tool_input": {"file_path": path}}
        return self.hook.blocked(inp, self.home, env or {})

    def test_non_main_cannot_edit_wiki(self):
        self.assertIsNotNone(self.check(os.path.join(self.vault, "wiki", "index.md")))

    def test_manager_and_raw_and_other_files_allowed(self):
        wiki = os.path.join(self.vault, "wiki", "projects", "x.md")
        self.assertIsNone(self.check(wiki, env={"AO_SESSION_ID": "hub-1"}))   # Manager Agent
        self.assertIsNone(self.check(os.path.join(self.vault, "raw", "2026-11-04.md")))
        self.assertIsNone(self.check(os.path.join(self.tmp, "code.py")))

    def test_terminal_main_allowed(self):
        s = Store()
        s.attach_main("claude-main")
        s.close()
        self.assertIsNone(self.check(os.path.join(self.vault, "wiki", "index.md"), sid="claude-main"))
        self.assertIsNotNone(self.check(os.path.join(self.vault, "wiki", "index.md"), env={"AO_SESSION_ID": "hub-1"}))


class LauncherTest(unittest.TestCase):
    """고정 실행 파일은 깔린 판 중 가장 새 판을 가리킨다(옛 판이 돌아도 새 판으로)."""

    def test_points_to_newest_installed_version(self):
        import aohub.launcher as L
        tmp = tempfile.mkdtemp(prefix="aohub-launch-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for v in ("0.4.3", "0.4.10", "0.4.4"):
            os.makedirs(os.path.join(tmp, "cache", v, "scripts"))
            os.makedirs(os.path.join(tmp, "cache", v, ".claude-plugin"))
            open(os.path.join(tmp, "cache", v, "scripts", "aohub.py"), "w").close()
            json.dump({"version": v}, open(os.path.join(tmp, "cache", v, ".claude-plugin", "plugin.json"), "w"))
        old_root, old_home = L.ROOT, os.environ.get("AOHUB_HOME")
        os.environ["AOHUB_HOME"] = os.path.join(tmp, "home")
        try:
            L.ROOT = os.path.join(tmp, "cache", "0.4.3")  # 옛 판이 돌 때
            target = L.target_of_shim(L.ensure())
            self.assertTrue(target.endswith("0.4.10/scripts/aohub.py"), target)
        finally:
            L.ROOT = old_root
            if old_home is None:
                os.environ.pop("AOHUB_HOME", None)
            else:
                os.environ["AOHUB_HOME"] = old_home


class ManagerTest(Base):
    """Main 을 AO 프로젝트 hub 의 orchestrator(Manager Agent)로 쓸 때."""

    def test_manager_project_is_not_assignable(self):
        with self.assertRaises(ValueError):
            self.assign(project_id="hub")

    def test_notices_go_to_manager_by_ao_send_once(self):
        t, _, _ = self.assign()
        self.b.sup_report(t["task_id"], "question", "A 인가 B 인가?", worker="w1", options=["A", "B"])
        mgr = self.ao.spawn("hub", "Manager Agent", kind="orchestrator")
        self.store.attach_main("ao:" + mgr)
        sent = self.b.deliver_notices_ao(mgr)
        again = self.b.deliver_notices_ao(mgr)
        self.assertEqual(len(sent), 1)
        self.assertEqual(again, [])
        bodies = [b for s, b, _ in self.ao.sent if s == mgr]
        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0].startswith("[AO 질문] %s ·" % sent[0]))
        self.assertIn("bin/aohub.py", bodies[0])  # 판이 바뀌어도 그대로인 고정 실행 파일

    def test_manager_reset_replaces_session_and_keeps_state(self):
        self.ao.kill = lambda sid: self.ao.sessions_.__setitem__(sid, dict(self.ao.sessions_[sid], isTerminated=True)) or             [o.update(isTerminated=True) for o in self.ao.orchs if o["id"] == sid]
        t, _, _ = self.assign()
        self.b.sup_report(t["task_id"], "question", "질문?", worker="w1")
        first = self.ao.spawn("hub", "Manager Agent", kind="orchestrator")
        self.ao.sessions_[first]["activity"] = {"state": "idle"}
        self.b.store.attach_main("ao:" + first)
        self.b.deliver_notices_ao(first)
        self.b.setup_manager = lambda path, project_id=None: (self.store.attach_main("ao:new-1") or
                                                               {"session": "new-1", "steps": []})
        res = self.b.manager_reset()
        self.assertEqual(res["closed"], first)
        self.assertEqual(self.store.active_main()["session_id"], "ao:new-1")
        self.assertEqual(len(self.store.questions("open")), 1)       # 상태는 그대로
        self.ao.sessions_["new-1"] = {"id": "new-1", "projectId": "hub", "role": "orchestrator", "isTerminated": False}
        self.ao.transcripts["new-1"] = []
        self.assertEqual(len(self.b.deliver_notices_ao("new-1")), 1)  # 처리 안 된 알림은 새 Manager 에 다시

    def test_reset_with_note_writes_session_handoff(self):
        self.ao.kill = lambda sid: [o.update(isTerminated=True) for o in self.ao.orchs if o["id"] == sid]
        t, _, _ = self.assign()
        r = self.b.sup_report(t["task_id"], "question", "A 인가 B 인가?", worker="w1")
        self.b.answer(r["question_id"], "A", reason="간단함")
        first = self.ao.spawn("hub", "Manager Agent", kind="orchestrator")
        self.ao.sessions_[first]["activity"] = {"state": "idle"}
        self.store.attach_main("ao:" + first)
        self.b.setup_manager = lambda path, project_id=None: (self.store.attach_main("ao:new-1") or {"session": "new-1", "steps": []})
        res = self.b.manager_reset(note="사용자는 시험 저장소 병합을 미루기로 함. 다음엔 실제 저장소 등록.", title="첫 시험")
        path = res["session_note"]
        self.assertTrue(path.replace("\\", "/").split("/")[-2] == "세션", path)
        text = open(path, encoding="utf-8").read()
        self.assertIn('type: "ao-main-session"', text)
        self.assertIn("사용자는 시험 저장소 병합을 미루기로 함", text)
        self.assertIn("[[wiki/tasks/%s]]" % t["task_id"], text)
        self.assertIn("[[wiki/decisions/%s]]" % r["question_id"], text)
        row = self.store.db.execute("SELECT value FROM meta WHERE key='last_session_note'").fetchone()
        self.assertEqual(row["value"], path)

    def test_manager_reset_refuses_while_busy(self):
        busy = self.ao.spawn("hub", "Manager Agent", kind="orchestrator")
        self.ao.sessions_[busy]["activity"] = {"state": "active"}
        with self.assertRaises(ValueError):
            self.b.manager_reset()

    def test_manager_rules_go_to_manager_project(self):
        self.ao.project = lambda pid: {"id": pid, "path": self.repo, "config": {}}
        captured = {}
        self.ao.set_config = lambda pid, obj: captured.update({pid: obj})
        self.b.ensure_project_rules("hub")
        self.assertIn("Manager Agent", captured["hub"]["orchestratorRules"])
        self.assertNotIn("Supervisor 가 먼저 잡는다", captured["hub"]["orchestratorRules"])


class ReviewTest(Base):
    """검증 명령이 없는 일(문서, 조사)은 사용자 확인으로 끝낸다."""

    def test_no_verify_cmd_needs_user_review_then_accept(self):
        t, _, _ = self.assign(verify_cmd=None, title="설계 문서", request="설계 문서 작성", done_criteria="문서 초안")
        tid = t["task_id"]
        self.b.sup_report(tid, "done", "docs/design.md 작성", worker="w1")
        self.b.poll_once(auto_verify=True)
        self.b.poll_once(auto_verify=True)
        self.assertEqual(self.store.task(tid)["status"], "needs_review")
        reviews = [n for n in self.store.notices() if n["kind"] == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertIn("accept %s" % tid, reviews[0]["text"])
        self.assertEqual([b for s, b, _ in self.ao.sent if b.startswith("[알림]")], [])  # Supervisor 에 실패로 보내지 않음
        with self.assertRaises(ValueError):
            self.b.accept("T-없음")
        self.b.accept(tid, note="사용자가 문서 확인")
        t = self.store.task(tid)
        self.assertEqual((t["status"], t["verified"]), ("verified_done", 1))
        self.assertEqual([n for n in self.store.notices() if n["kind"] == "review"], [])
        note = open(os.path.join(self.vault_dir, "wiki", "tasks", tid + ".md"), encoding="utf-8").read()
        self.assertIn("사용자 확인: 사용자가 문서 확인", note)
        raw = "".join(open(os.path.join(self.vault_dir, "raw", f), encoding="utf-8").read()
                      for f in os.listdir(os.path.join(self.vault_dir, "raw")) if f.endswith(".md"))
        self.assertIn("사용자 확인으로 완료", raw)

    def test_accept_before_done_is_refused(self):
        t, _, _ = self.assign(verify_cmd=None)
        with self.assertRaises(ValueError):
            self.b.accept(t["task_id"])


class EscalationTest(Base):
    """Worker 수준 오류는 Supervisor 가 먼저 잡는다. Main 감시는 안전망(2026-11-04 사용자 결정)."""

    def setUp(self):
        super().setUp()
        self.t, _, _ = self.assign()
        self.tid = self.t["task_id"]
        self.sup = self.t["supervisor_session"]
        self.ao.sessions_["w1"] = {"id": "w1", "projectId": "demo", "role": "worker", "displayName": self.tid + " x",
                                   "status": "idle", "activity": "idle", "isTerminated": False, "branch": "main"}
        self.ao.turns["w1"] = [{"id": "turn-1", "state": "failed", "errorMessage": "model_not_found"}]

    def to_sup(self):
        return [b for s, b, _ in self.ao.sent if s == self.sup and b.startswith("[알림] %s Worker 실행 실패" % self.tid)]

    def test_rules_carry_error_criteria(self):
        self.assertIn("오류 처리 기준", self.ao.cfg_obj["orchestratorRules"])
        self.assertIn("worker-state", self.ao.sent[0][1])  # 배정 메시지에도 들어간다

    def test_worker_state_reveals_failed_turn(self):
        st = self.b.worker_state("w1")
        self.assertEqual(st["verdict"], "failed")
        self.assertEqual(st["error"], "model_not_found")

    def test_supervisor_catches_first_main_stays_quiet(self):
        self.b.poll_once(auto_verify=False)
        self.assertEqual(self.to_sup(), [])  # 유예 시간 안에는 Supervisor 에 보내지 않는다
        self.b.sup_report(self.tid, "progress", "w1 모델 오류, 대체 모델로 w2 를 다시 띄움", worker="w2")
        self.cfg["error_policy"] = {"supervisor_grace_seconds": 0}
        self.b.poll_once(auto_verify=False)
        self.assertEqual(self.to_sup(), [])
        self.assertEqual(self.store.notices(), [])
        self.assertTrue(any(e["kind"] == "caught_by_supervisor" for e in self.store.events(self.tid)))

    def test_no_reaction_alerts_supervisor_then_main(self):
        self.cfg["error_policy"] = {"supervisor_grace_seconds": 0}
        self.b.poll_once(auto_verify=False)
        self.b.poll_once(auto_verify=False)
        self.assertEqual(len(self.to_sup()), 1)  # 안전망: 한 번만
        self.assertEqual(self.store.notices(), [])
        self.cfg["escalate_seconds"] = 0
        self.b.poll_once(auto_verify=False)
        self.assertEqual([n["kind"] for n in self.store.notices()], ["blocked"])
        self.assertIn("Supervisor 미해결", self.store.notices()[0]["text"])

    def test_alert_then_supervisor_resolves(self):
        self.cfg["error_policy"] = {"supervisor_grace_seconds": 0}
        self.b.poll_once(auto_verify=False)
        self.b.sup_report(self.tid, "worker", "w2 를 다시 띄움", worker="w2")
        self.b.poll_once(auto_verify=False)
        self.assertEqual(self.store.notices(), [])
        self.assertTrue(any(e["kind"] == "escalation_resolved" for e in self.store.events(self.tid)))

    def test_no_supervisor_goes_to_main_directly(self):
        self.store.update_task(self.tid, supervisor_session=None)
        self.cfg["error_policy"] = {"supervisor_grace_seconds": 0}
        self.b.poll_once(auto_verify=False)
        self.assertEqual([n["kind"] for n in self.store.notices()], ["blocked"])


class HubHookTest(unittest.TestCase):
    """허브 폴더에서 열린 세션에 이름(sessionTitle)을 붙이는 SessionStart 훅."""

    def run_hook(self, cwd, extra=None, title=None):
        hub = tempfile.mkdtemp(prefix="aohub-hub-")
        self.addCleanup(shutil.rmtree, hub, True)
        inp = {"session_id": "s1", "cwd": hub if cwd == "hub" else cwd}
        if extra:
            inp.update(extra)
        env = dict(os.environ, CLAUDE_PLUGIN_OPTION_HUB_DIR=hub, CLAUDE_PROJECT_DIR=inp["cwd"],
                   AOHUB_HOME=os.path.join(hub, "no-aohub"))
        if title is not None:
            env["CLAUDE_PLUGIN_OPTION_HUB_TITLE"] = title
        p = subprocess.run([sys.executable, os.path.join(ROOT, "hooks", "ao_watch_start.py")], input=json.dumps(inp),
                           capture_output=True, encoding="utf-8", env=env)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def test_titles_hub_session(self):
        out = self.run_hook("hub")
        self.assertEqual(out["hookSpecificOutput"]["sessionTitle"], "hub")

    def test_custom_title(self):
        self.assertEqual(self.run_hook("hub", title="main")["hookSpecificOutput"]["sessionTitle"], "main")

    def test_keeps_existing_title_and_ignores_other_folders(self):
        self.assertIsNone(self.run_hook("hub", {"session_title": "내이름"}))
        self.assertIsNone(self.run_hook(tempfile.gettempdir()))


class VaultTest(Base):
    def test_notes_keep_user_memo_and_properties(self):
        t, _, _ = self.assign()
        tid = t["task_id"]
        r = self.b.sup_report(tid, "question", "어느 쪽?", worker="w1", options=["A", "B"])
        self.b.answer(r["question_id"], "A", reason="간단함")
        path = os.path.join(self.vault_dir, "wiki", "tasks", tid + ".md")
        text = open(path, encoding="utf-8").read()
        self.assertIn("type: \"ao-task\"", text)
        self.assertIn("## 메모", text)
        self.assertEqual(text.count("\n---\n"), 1)  # frontmatter 닫는 줄 하나뿐
        # 사용자가 메모와 속성을 더한다
        text = text.replace("---\n# ", "---\n# ", 1)
        text = text.replace("tags:", "my_prop: \"사용자 값\"\ntags:", 1)
        text += "\n사용자 메모 한 줄\n"
        open(path, "w", encoding="utf-8").write(text)
        self.b.sup_report(tid, "progress", "진행 중", worker="w1")
        self.b.sync_vault()
        new = open(path, encoding="utf-8").read()
        self.assertIn("사용자 메모 한 줄", new)
        self.assertIn('my_prop: "사용자 값"', new)
        self.assertIn("진행 중", new)
        # 같은 상태로 다시 그리면 아무것도 바뀌지 않는다
        self.assertEqual(self.b.sync_vault(), [])
        dec = os.path.join(self.vault_dir, "wiki", "decisions", r["question_id"] + ".md")
        self.assertIn("이유: 간단함", open(dec, encoding="utf-8").read())
        self.assertTrue(os.path.exists(os.path.join(self.vault_dir, "wiki", "AO 현황.base")))
        self.assertIn("aohub:begin ao-index", open(os.path.join(self.vault_dir, "wiki", "index.md"), encoding="utf-8").read())

    def test_raw_entries_once(self):
        t, _, _ = self.assign()
        r = self.b.sup_report(t["task_id"], "question", "질문?", worker="w1")
        self.b.answer(r["question_id"], "예", reason="근거")
        for _ in range(3):
            self.b.sync_vault()
        raw_dir = os.path.join(self.vault_dir, "raw")
        text = "".join(open(os.path.join(raw_dir, f), encoding="utf-8").read() for f in os.listdir(raw_dir) if f.endswith(".md"))
        self.assertEqual(text.count("<!-- wl:"), 2)  # 질문 1, 결정 1
        self.assertIn("[[wiki/tasks/%s]]" % t["task_id"], text)

    def test_existing_note_without_markers_is_appended_not_replaced(self):
        os.makedirs(os.path.join(self.vault_dir, "wiki", "projects"))
        p = os.path.join(self.vault_dir, "wiki", "projects", "demo.md")
        open(p, "w", encoding="utf-8").write("---\nowner: 사람\n---\n# demo\n\n사람이 쓴 본문\n")
        self.assign()
        text = open(p, encoding="utf-8").read()
        self.assertIn("사람이 쓴 본문", text)
        self.assertIn("owner: 사람", text)
        self.assertIn("aohub:begin ao-project", text)
        self.assertNotIn('type: "ao-project"', text)  # 기존 노트의 type 은 건드리지 않는다

    def test_helpers(self):
        t = replace_block("x\n", "a", "1")
        t = replace_block(t, "a", "2")
        self.assertEqual(t.count("aohub:begin a"), 1)
        self.assertIn("\n2\n", t)
        fm = merge_frontmatter("---\nk: 1\nlist:\n  - x\n---\nbody", {"k": 2, "n": [1]})
        self.assertIn("k: 2", fm)
        self.assertIn("list:\n  - x", fm)
        self.assertTrue(fm.endswith("body"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
