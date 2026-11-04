"""Main ↔ AO Supervisor 연동 동작.

역할 나눔
- AO: Supervisor(orchestrator)와 Worker 세션의 실행 상태와 수명 주기. 현재 상태는 늘 AO 에 다시 묻는다.
- 이 모듈: 작업 ID, 완료 조건, 질문과 결정, 이벤트와 전달 기록, Main 에 보낼 알림.

전달 원칙
- `ao spawn`/`ao send` 의 성공 출력은 '보냈다'일 뿐이다. 배정과 답변은 Supervisor transcript 에서
  표시 문자열(`[배정] T-…`, `[답변] Q-…`)을 찾아야 '도착'으로 기록한다.
- 보낸 결과가 불확실하면 다시 보내기 전에 transcript 를 먼저 본다. 이미 있으면 다시 보내지 않는다.
- Worker 의 done 보고나 세션 idle 은 완료가 아니다. Main 이 작업 공간에서 검증 명령을 돌려야 완료다.
"""
import hashlib
import json
import os
import subprocess
import time

from . import config, inbox, launcher
from .ao import AO, AOError
from .store import Store, now
from .vault import Vault

SUP_KINDS = ("worker", "progress", "question", "done", "blocked")


def aohub_cmd(cfg):
    """규칙·배정·알림에 적는 aohub 명령. 플러그인 판이 바뀌어도 그대로인 고정 실행 파일을 가리킨다."""
    return launcher.command(cfg)


def short_hash(*parts):
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def error_policy(cfg):
    pol = dict(config.DEFAULTS["error_policy"])
    pol.update(cfg.get("error_policy") or {})
    return pol


def error_rules(cfg):
    """Supervisor 가 스스로 잡고 처리하는 오류 기준. 기준에 없으면 Main 으로 올린다."""
    cmd = aohub_cmd(cfg)
    pol = error_policy(cfg)
    fb = pol.get("fallback_models") or {}
    fb_text = ", ".join("%s → `--model %s`" % (k, v) for k, v in fb.items()) or "정해진 대체 모델 없음(blocked 로 올림)"
    return [
        "### 오류 처리 기준 (Supervisor 가 먼저 잡는다)",
        "",
        "AO 는 turn 이 실패한 worker 도 `idle` 로 보여 주고, 첫 turn 에서 실패한 worker 는 `ao report` 를 하지 못합니다.",
        "그래서 worker 상태는 이 명령으로 직접 확인합니다: `%s worker-state <worker-session-id>` (판정 `failed`/`working`/`idle`/`terminated`)" % cmd,
        "",
        "- 확인 시점: worker 를 띄우고 1분 안에 한 번, worker 를 기다리는 동안 몇 분마다(백그라운드 반복 확인 가능), worker 가 idle 이 됐는데 보고가 없을 때.",
        "- 기준대로 처리하고 `sup-report --kind progress --text \"<무슨 오류를 어떻게 처리했는지>\"` 로 알립니다. 처리한 내용은 숨기지 않습니다.",
        "  - worker 실행 실패(turn failed, 세션 종료): 원인을 보고 `ao session kill` 뒤 같은 지시로 다시 띄웁니다. 작업당 %d회까지." % pol["worker_retries"],
        "  - 모델 미지원 오류(model_not_found 등): 대체 모델로 다시 띄웁니다. 대체 모델: %s." % fb_text,
        "  - 시험·검증 실패(worker 보고 또는 Main 의 `[알림] … 검증 실패`): 실패 출력을 붙여 같은 worker 에 다시 지시합니다. 작업당 %d회까지." % pol["test_retries"],
        "- 아래는 직접 처리하지 않고 `--kind blocked`(판단이 필요하면 `--kind question`)로 Main 에 올립니다:",
        "  재시도를 다 쓴 오류, 기준에 없는 오류, 권한 승인 대기, AO 프로젝트 설정·모델 기본값 변경, 작업 범위·완료 조건 변경, 원인을 모르는 반복 실패.",
        "- Main 감시 프로세스는 안전망입니다. worker 실패 뒤 %d초 안에 이 Supervisor 의 보고가 없으면 `[알림]` 을 보내고, 그 뒤에도 처리 보고가 없으면 Main(사용자)에게 올립니다." % pol["supervisor_grace_seconds"],
    ]


GIT_MESSAGE_RULE = ("- 커밋·PR 문구는 agent-hub-ao 의 git-message 규칙을 따릅니다: 제목 한 줄(72칸 이하, 마침표 없음, 무엇을 바꿨는지), "
                    "빈 줄, `- ` 불릿 본문(사실 하나씩, 명사형 종결). Codex worker 처럼 Claude 훅이 안 걸리는 worker 에게도 이 규칙을 전달합니다.")


def supervisor_rules(cfg):
    cmd = aohub_cmd(cfg)
    return "\n".join([
        "## Main 허브 연동 (aohub)",
        "",
        "이 프로젝트의 사람 쪽 창구는 Main Claude Code 세션(허브)입니다. Main 은 `[배정] T-YYYYMMDD-NN` 메시지로 일을 맡기고,",
        "사용자 답은 `[답변] Q-YYYYMMDD-NN` 메시지로 보냅니다. Main 에게는 아래 명령으로만 알립니다(셸에서 실행).",
        "",
        "- worker 를 띄우거나 기존 worker 에 일을 넘기면 바로: `%s sup-report --task <T> --kind worker --worker <worker-session-id> --text \"<한 줄>\"`" % cmd,
        "- worker 이름(`--name`)은 작업 ID 로 시작합니다. 예: `T-20261104-01 subtract 추가`.",
        "- 작업 범위 안에서 정할 수 없는 사용자 판단(worker 의 needs-input 포함)은 직접 정하지 말고:",
        "  `%s sup-report --task <T> --kind question --worker <worker-session-id> --text \"<질문>\" --option \"<선택지>\" --option \"<선택지>\"`" % cmd,
        "  출력된 Q-ID 를 worker 에게 알리고 답을 기다리게 합니다. `[답변]` 이 오면 그 내용을 해당 worker 에 `ao send` 로 넘깁니다.",
        "- worker 가 done 을 보고하고 직접 확인했으면: `%s sup-report --task <T> --kind done --worker <worker-session-id> --text \"<바뀐 것, 커밋, 시험 결과>\"`" % cmd,
        "- 진행 상황은 `--kind progress`, 막히면 `--kind blocked` 로 알립니다. 같은 내용을 반복해서 보내지 않습니다.",
        "- `--text` 에 Windows 경로를 넣지 않습니다(Git Bash 가 경로를 바꿉니다). 작업 공간 경로는 Main 이 직접 찾습니다.",
        "- 작업일지(worklog), Obsidian 노트, 허브 STATUS 기록은 Main 이 합니다. 이 세션과 worker 는 기록하지 않습니다.",
        "- `[답변]` 은 업무 결정입니다. 도구 실행 권한 승인이 아닙니다. 권한 승인이 필요하면 blocked 로 알립니다.",
        "- push, PR, 병합, 배포는 배정 메시지가 허락할 때만 합니다.",
        GIT_MESSAGE_RULE,
        "",
    ] + error_rules(cfg))


def manager_rules(cfg):
    """Main 을 AO 프로젝트(manager_project)의 orchestrator 로 쓸 때 그 프로젝트 orchestratorRules 에 넣는 규칙."""
    cmd = aohub_cmd(cfg)
    return "\n".join([
        "## Manager Agent (Main 허브)",
        "",
        "이 세션은 사용자가 대화하는 Main(허브)입니다. 이름은 %s 입니다. 위의 AO 기본 지시는 '이 프로젝트 안' 기준입니다." % cfg.get("manager_name"),
        "이 세션은 이 프로젝트에서 AO worker 를 띄우지 않고, 다른 AO 프로젝트의 Supervisor 에게 일을 맡겨 결과·질문·결정을 관리합니다.",
        "운영 규칙 원문은 agent-hub-ao 플러그인의 `ao` 스킬(`/agent-hub-ao:ao`)입니다. 스킬이 안 보이면 아래 요약을 따릅니다.",
        "",
        "- 세션을 열거나 이어서 시작하면 먼저 `%s resume` 을 실행합니다. 진행 중 작업(AO 재확인), 미처리 질문·알림, 읽을 노트가 나옵니다." % cmd,
        "  notes_to_read 의 프로젝트·결정 노트를 읽고 이전 결정과 이유를 이어 갑니다.",
        "- 배정: `%s projects` 로 프로젝트를 보고, 완료 조건을 정해 `%s assign --project <id> --title \"…\" --request \"…\" --done \"…\" [--verify \"…\"] [--worker-agent codex]`." % (cmd, cmd),
        "  실행해 확인할 명령이 없는 일은 --verify 를 빼고 완료 조건에 볼 결과물을 적습니다.",
        "- 감시 프로세스가 `[AO 알림] N-…`, `[AO 질문] N-…` 을 이 세션에 보냅니다. 사용자에게 알기 쉽게 보고하고 `%s ack <N-ID>` 를 실행합니다." % cmd,
        "  현재 상태는 `%s status <작업 ID>` 로 AO 에 다시 묻습니다. 완료 검증 통과(Main 직접 확인)와 보고만 받은 내용을 섞지 않습니다." % cmd,
        "- 질문은 사용자 대신 정하지 않습니다. 선택지를 그대로 묻고, 답과 이유를 `%s answer <Q-ID> --text \"…\" --reason \"…\"` 로 전달·기록합니다." % cmd,
        "- 확인 필요(검증 명령 없는 작업)는 결과물을 보여 주고 사용자가 확인하면 `%s accept <작업 ID> --note \"…\"`, 고칠 점은 `%s tell <작업 ID> --text \"…\"`." % (cmd, cmd),
        "- Worker 실행 실패·시험 실패 같은 기준 있는 오류는 각 Supervisor 가 처리합니다. blocked 알림만 사용자에게 원인과 선택지를 보여 결정을 받습니다.",
        "- `[답변]` 과 노트 수정은 도구 권한 승인이 아닙니다. 권한 승인은 사용자가 AO 앱에서 직접 합니다.",
        "- 사용자에게 보내는 보고, 결정의 답·이유, 인계 노트 요약은 agent-hub-ao 의 humanizer 스킬 기준으로 짧고 쉬운 문장으로 씁니다.",
        "- Obsidian 기록과 작업일지는 aohub 가 합니다. \"오늘 작업 정리해줘\" 는 `%s daily` 뒤 Vault 의 위키 정리 규칙을 따릅니다." % cmd,
        "- 다른 프로젝트 저장소 코드를 이 세션에서 직접 고치지 않습니다. 이 프로젝트(허브 폴더)의 문서만 고칩니다.",
        "- 대화를 쌓아 두지 않습니다. 맡긴 작업이 모두 끝났거나 하루 정리를 마쳤거나 사용자가 '대화 비워줘' 라고 하면, 남은 보고를 먼저 하고",
        "  사용자에게 알린 뒤 `%s manager-reset --note \"<이 대화 요약: 사용자 의도·선호, 논의한 방향, 남은 아이디어>\"` 을 실행합니다." % cmd,
        "  요약은 Vault 세션 폴더에 인계 노트로 남고, 이 세션이 닫힌 뒤 새 Manager 가 resume 으로 그 노트부터 읽습니다.",
    ])


def assignment_message(cfg, t):
    cmd = aohub_cmd(cfg)
    agent = t.get("worker_agent")
    lines = [
        "[배정] %s" % t["task_id"],
        "제목: %s" % t["title"],
        "할 일: %s" % t["request"].strip(),
        "완료 조건: %s" % t["done_criteria"].strip(),
    ]
    if t.get("verify_cmd"):
        lines.append("검증 명령(Main 이 worker 작업 공간에서 다시 실행): `%s`" % t["verify_cmd"])
    lines += [
        "worker: %s" % (("`--agent %s` 로 띄우세요" % agent) if agent else "프로젝트 기본 agent"),
        "범위 밖: push, PR/MR, 병합, 배포, 작업일지·Obsidian 기록(Main 이 함)",
        "커밋 문구: 제목 한 줄(무엇을 바꿨는지, 마침표 없음), 빈 줄, `- ` 불릿 본문(명사형 종결). worker 에게도 전달",
        "",
        "보고 방법(셸 명령, Main 에게 가는 유일한 통로):",
        "- worker 를 띄운 직후: %s sup-report --task %s --kind worker --worker <worker-session-id> --text \"<한 줄>\"" % (cmd, t["task_id"]),
        "- 사용자 판단이 필요할 때: %s sup-report --task %s --kind question --worker <worker-session-id> --text \"<질문>\" --option \"<선택지>\"" % (cmd, t["task_id"]),
        "- 끝났을 때: %s sup-report --task %s --kind done --worker <worker-session-id> --text \"<바뀐 것, 커밋, 시험 결과>\"" % (cmd, t["task_id"]),
        "worker 이름(--name)은 `%s` 로 시작하게 하세요." % t["task_id"],
        "",
    ] + error_rules(cfg)
    return "\n".join(lines)


class Bridge:
    def __init__(self, cfg=None, store=None, ao=None, vault=None):
        self.cfg = cfg or config.load()
        self.store = store or Store()
        self.ao = ao or AO(self.cfg)
        self.vault = vault or Vault(self.cfg)
        self.confirm_seconds = 45

    # ------------------------------------------------------------ Supervisor
    def is_manager_project(self, project_id):
        return bool(project_id) and project_id == self.cfg.get("manager_project")

    def ensure_project_rules(self, project_id):
        """프로젝트 설정의 orchestratorRules 에 연동 규칙을 넣는다. 다른 설정은 그대로 둔다(set-config 는 통째 교체).
        Manager 프로젝트에는 Supervisor 규칙 대신 Manager 규칙을 넣는다."""
        proj = self.ao.project(project_id)
        cfg_obj = dict(proj.get("config") or {})
        rules = cfg_obj.get("orchestratorRules") or ""
        block = manager_rules(self.cfg) if self.is_manager_project(project_id) else supervisor_rules(self.cfg)
        start, end = "<!-- aohub:begin -->", "<!-- aohub:end -->"
        new_block = "%s\n%s\n%s" % (start, block, end)
        if start in rules and end in rules:
            i, j = rules.index(start), rules.index(end) + len(end)
            new_rules = rules[:i] + new_block + rules[j:]
        else:
            new_rules = (rules.rstrip() + "\n\n" if rules.strip() else "") + new_block
        if new_rules == rules:
            return False
        cfg_obj["orchestratorRules"] = new_rules
        self.ao.set_config(project_id, cfg_obj)
        return True

    def _live_orchestrator(self, project_id):
        alive = [o for o in self.ao.orchestrators(project_id) if not o.get("isTerminated")]
        alive.sort(key=lambda o: o.get("createdAt") or "")
        return alive[-1] if alive else None

    def ensure_supervisor(self, project_id, wait_seconds=60):
        """살아 있는 Supervisor 를 돌려준다. 없을 때만 띄운다. 재시도해도 두 개 생기지 않게 한다."""
        live = self._live_orchestrator(project_id)
        if live:
            self.store.set_supervisor(project_id, live["id"])
            return live["id"], False
        if not self.store.claim_supervisor_spawn(project_id):
            # 다른 프로세스가 띄우는 중이다. 기다렸다가 다시 본다.
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                time.sleep(3)
                live = self._live_orchestrator(project_id)
                if live:
                    self.store.set_supervisor(project_id, live["id"])
                    return live["id"], False
            raise AOError("다른 프로세스가 %s Supervisor 를 띄우는 중인데 끝나지 않았습니다." % project_id, uncertain=True)
        self.ensure_project_rules(project_id)
        name = (self.cfg.get("supervisor_prefix") or "sup-") + project_id
        try:
            sid = self.ao.spawn(project_id, name, kind="orchestrator")
        except AOError as e:
            if not e.uncertain:
                self.store.set_supervisor(project_id, None, "failed")
                raise
            live = self._live_orchestrator(project_id)  # 만들어졌는지 확인하고 받아들인다
            if not live:
                self.store.set_supervisor(project_id, None, "failed")
                raise
            sid = live["id"]
        self.store.set_supervisor(project_id, sid)
        self.store.record_event("sup-spawn:%s:%s" % (project_id, sid), "main", "supervisor_spawned",
                                project_id=project_id, summary="Supervisor %s 생성" % sid)
        return sid, True

    def _wait_controller(self, sid, timeout=90):
        """spawn 이 'idle' 로 끝나도 chat 시작이 실패할 수 있다(예: 기본 브랜치 미확정). 컨트롤러 준비를 확인한다."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            s = self.ao.session(sid, transcript=1, cap=80)
            if (s.get("session") or {}).get("isTerminated"):
                raise AOError("Supervisor %s 가 종료됐습니다." % sid)
            last = (s.get("transcript") or {}).get("controller")
            if last == "ready":
                return True
            time.sleep(3)
        raise AOError("Supervisor %s 컨트롤러가 준비되지 않았습니다(마지막 상태 %s). ao 데몬 로그를 확인하세요." % (sid, last),
                      uncertain=True)

    def transcript_has(self, sid, marker, entries=60):
        s = self.ao.session(sid, transcript=entries, cap=300)
        for e in (s.get("transcript") or {}).get("entries", []):
            if e.get("role") == "user" and marker in (e.get("text") or ""):
                return True
        return False

    def deliver(self, delivery_id, sid, marker, body, task_id=None, kind="send", confirm_seconds=None):
        """AO 세션에 한 번만 전달한다. 반환: confirmed | sent(도착 확인 전)."""
        d = self.store.delivery(delivery_id)
        if d and d["status"] == "confirmed" and d["target_session"] == sid:
            return "confirmed"
        if d and d["target_session"] == sid and d["status"] in ("pending", "sent", "uncertain"):
            # 앞서 보냈거나 보냈는지 모른다. 다시 보내기 전에 AO 전달 기록과 transcript 를 먼저 본다.
            try:
                self.ao.recover_send(sid, delivery_id)
                recovered = True
            except AOError:
                recovered = False
            if self._confirm(sid, marker, 5 if not recovered else self.confirm_seconds):
                self.store.put_delivery(delivery_id, status="confirmed")
                return "confirmed"
            if recovered:
                # 데몬은 전달을 기록했는데 transcript 에 아직 없다. 다시 보내지 않는다.
                self.store.put_delivery(delivery_id, status="sent")
                return "sent"
        self.store.put_delivery(delivery_id, task_id=task_id, target_session=sid, kind=kind, marker=marker, body=body,
                                status="pending")
        try:
            self.ao.send(sid, body, client_message_id=delivery_id)
            self.store.put_delivery(delivery_id, status="sent",
                                    attempts=(self.store.delivery(delivery_id)["attempts"] or 0) + 1)
        except AOError as e:
            status = "uncertain" if e.uncertain else "failed"
            self.store.put_delivery(delivery_id, status=status, last_error=str(e)[:500],
                                    attempts=(self.store.delivery(delivery_id)["attempts"] or 0) + 1)
            if status == "failed":
                raise
        if self._confirm(sid, marker, self.confirm_seconds if confirm_seconds is None else confirm_seconds):
            self.store.put_delivery(delivery_id, status="confirmed")
            return "confirmed"
        return self.store.delivery(delivery_id)["status"]

    def _confirm(self, sid, marker, seconds):
        deadline = time.time() + seconds
        while True:
            try:
                if self.transcript_has(sid, marker):
                    return True
            except AOError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(3)

    # ------------------------------------------------------------ 배정
    def assign(self, project_id, title, request, done_criteria, verify_cmd=None, worker_agent=None, key=None):
        if not self.ao.ready():
            raise AOError("AO 데몬이 준비되지 않았습니다. 데스크톱 앱을 열거나 `ao daemon` 을 실행하세요.")
        if self.is_manager_project(project_id):
            raise ValueError("%s 는 Manager(Main) 프로젝트입니다. 일은 다른 AO 프로젝트에 맡기세요." % project_id)
        request_key = key or short_hash(project_id, title, request, done_criteria, verify_cmd or "")
        t, created = self.store.create_task(request_key, project_id=project_id, title=title, request=request,
                                            done_criteria=done_criteria, verify_cmd=verify_cmd,
                                            worker_agent=worker_agent)
        tid = t["task_id"]
        if created:
            self.store.record_event("task:%s:created" % tid, "main", "created", tid, project_id,
                                    "사용자 요청으로 작업 생성: " + title)
        if t["status"] not in ("created", "assigned"):
            return t, created, "existing"
        sid, spawned = self.ensure_supervisor(project_id)
        self._wait_controller(sid)
        self.store.update_task(tid, supervisor_session=sid)
        t = self.store.task(tid)
        result = self.deliver("%s:assign:%s" % (tid, sid), sid, "[배정] %s" % tid, assignment_message(self.cfg, t),
                              task_id=tid, kind="assign")
        if result == "confirmed":
            self.store.update_task(tid, status="assigned")
            self.store.record_event("task:%s:assigned:%s" % (tid, sid), "main", "assigned", tid, project_id,
                                    "Supervisor %s transcript 에서 배정 도착 확인" % sid)
        self.sync_vault()
        return self.store.task(tid), created, result

    # ------------------------------------------------------------ Supervisor 보고
    def sup_report(self, task_id, kind, text, worker=None, options=None, event_id=None, reporter=None):
        if kind not in SUP_KINDS:
            raise ValueError("kind 는 %s 중 하나" % ", ".join(SUP_KINDS))
        t = self.store.task(task_id)
        if not t:
            raise ValueError("모르는 작업 ID: %s" % task_id)
        reporter = reporter or os.environ.get("AO_SESSION_ID") or t.get("supervisor_session") or "supervisor"
        ekey = "sup:%s:%s:%s" % (task_id, kind, event_id or short_hash(worker or "", text))
        first = self.store.record_event(ekey, "supervisor:%s" % reporter, kind, task_id, t["project_id"], text,
                                        {"worker": worker, "options": options})
        out = {"task_id": task_id, "event": ekey, "new": first}
        if worker:
            self.store.add_worker(task_id, worker)
        if kind == "worker":
            if t["status"] in ("created", "assigned"):
                self.store.update_task(task_id, status="working")
        elif kind == "question":
            q, qnew = self.store.add_question(ekey, task_id, t["project_id"], text, asked_by=reporter,
                                              worker_session=worker, options=options)
            out["question_id"] = q["question_id"]
            out["new"] = qnew
            if qnew and t["status"] not in ("verified_done", "cancelled"):
                # 같은 질문을 다시 받으면 상태를 되돌리지 않는다(이미 답했거나 끝난 작업)
                self.store.update_task(task_id, status="needs_input")
            if qnew:
                self.store.record_event("q:%s:raised" % q["question_id"], "supervisor:%s" % reporter, "question",
                                        task_id, t["project_id"], text, {"question_id": q["question_id"]})
            self.store.add_notice("notice:" + ekey, "question", self._question_notice(q, t), task_id)
        elif kind == "done":
            prev = t.get("reported_summary") or ""
            line = "- %s Supervisor `%s`%s: %s" % (now()[:16].replace("T", " "), reporter,
                                                   (" (Worker `%s`)" % worker) if worker else "", text)
            if line not in prev:
                self.store.update_task(task_id, reported_summary=(prev + "\n" + line).strip(), status="reported_done")
            out["verify"] = "queued"
        elif kind == "blocked":
            self.store.update_task(task_id, status="blocked")
            self.store.add_notice("notice:" + ekey, "blocked",
                                  self._notice_head("막힘", t) + "\nSupervisor 보고: %s" % text, task_id)
        elif kind == "progress" and first:
            pass
        if kind == "done":
            # 검증은 Main 쪽(감시 프로세스 또는 Main 명령)에서 한다. Supervisor 세션 안에서 돌리지 않는다.
            pass
        self.sync_vault(quiet=True)
        return out

    # ------------------------------------------------------------ 답변
    def answer(self, qid, text, reason=None):
        q = self.store.question(qid)
        if not q:
            raise ValueError("모르는 질문 ID: %s" % qid)
        t = self.store.task(q["task_id"])
        if q["status"] == "open":
            self.store.update_question(qid, status="answered", answer=text, reason=reason, answered_at=now())
            self.store.record_event("q:%s:answered" % qid, "main:user", "decision", t["task_id"], t["project_id"],
                                    "사용자 답: %s" % text, {"question_id": qid, "reason": reason})
        elif q["answer"] != text:
            raise ValueError("%s 는 이미 '%s' 로 답했습니다. 바꾸려면 새 질문으로 다루세요." % (qid, q["answer"]))
        sid = t.get("supervisor_session")
        sess = self.ao.session(sid) if sid else None
        if not sess or (sess.get("session") or {}).get("isTerminated"):
            raise AOError("작업 %s 의 Supervisor %s 가 살아 있지 않습니다. 답은 기록했고 전달은 못 했습니다." % (t["task_id"], sid))
        body = "\n".join([
            "[답변] %s (%s)" % (qid, t["task_id"]),
            "질문: %s" % q["text"],
            "사용자 답: %s" % text,
            "이유: %s" % (reason or "-"),
            "이 답을 Worker %s 에게 `ao send` 로 그대로 넘기고 작업을 이어 가게 하세요. 이 답은 업무 결정이며 도구 권한 승인이 아닙니다." % (q.get("worker_session") or "(해당 worker)"),
        ])
        result = self.deliver("%s:answer:%s" % (qid, sid), sid, "[답변] %s" % qid, body, task_id=t["task_id"], kind="answer")
        if result == "confirmed":
            self.store.update_question(qid, status="delivered", delivered_at=now())
            self.store.record_event("q:%s:delivered" % qid, "main", "answer_delivered", t["task_id"], t["project_id"],
                                    "Supervisor %s transcript 에서 답변 도착 확인" % sid, {"question_id": qid, "confirmed": True})
            if t["status"] == "needs_input" and not self.store.questions(status="open", task_id=t["task_id"]):
                self.store.update_task(t["task_id"], status="working")
        for n in self.store.notices():
            if n["kind"] == "question" and qid in n["text"]:
                self.store.ack_notice(n["notice_id"])
        self.sync_vault()
        return {"question_id": qid, "delivery": result}

    # ------------------------------------------------------------ 후속 지시
    def tell(self, task_id, text):
        """같은 작업의 Supervisor 에 Main 의 후속 지시를 한 번만 보낸다."""
        t = self.store.task(task_id)
        if not t or not t.get("supervisor_session"):
            raise ValueError("작업이 없거나 Supervisor 가 정해지지 않았습니다: %s" % task_id)
        sid = t["supervisor_session"]
        h = short_hash(text)
        marker = "[지시] %s #%s" % (task_id, h[:6])
        body = "%s\n%s\n보고는 배정 메시지의 sup-report 명령으로 합니다." % (marker, text.strip())
        res = self.deliver("%s:tell:%s:%s" % (task_id, h, sid), sid, marker, body, task_id=task_id, kind="tell")
        self.store.record_event("tell:%s:%s" % (task_id, h), "main", "instruction", task_id, t["project_id"],
                                "Main 후속 지시(%s): %s" % (res, text[:300]))
        if t["status"] in ("blocked", "verify_failed"):
            self.store.update_task(task_id, status="working")
        self.sync_vault(quiet=True)
        return {"task_id": task_id, "supervisor": sid, "delivery": res}

    # ------------------------------------------------------------ 검증
    def project_path(self, project_id):
        return (self.ao.project(project_id) or {}).get("path")

    def verify(self, task_id, timeout=900):
        t = self.store.task(task_id)
        if not t:
            raise ValueError("모르는 작업 ID: %s" % task_id)
        if not t.get("verify_cmd"):
            self.needs_review(t)
            return {"task_id": task_id, "status": "needs_review", "note": "검증 명령이 없어 사용자 확인으로 끝냅니다(accept)"}
        ppath = self.project_path(t["project_id"])
        workers = json.loads(t.get("worker_sessions") or "[]")
        if not workers:
            raise AOError("작업 %s 에 연결된 worker 가 없습니다." % task_id)
        candidates = []
        for w in workers:
            s = (self.ao.session(w).get("session") or {})
            branch = s.get("branch")
            wt = self.ao.worktree(ppath, branch) if branch else None
            if wt:
                candidates.append((w, s, wt))
        if not candidates:
            raise AOError("worker 작업 공간을 찾지 못했습니다: %s" % ", ".join(workers))
        base = self._base_ref(ppath)
        best = None
        for w, s, wt in candidates:
            commits = self._git(wt, "log", "--format=%h %s", "%s..HEAD" % base).splitlines()
            if best is None or len(commits) > len(best[3]):
                best = (w, s, wt, commits)
        w, s, wt, commits = best
        res = {"at": now(), "worker": w, "cwd": wt, "cmd": t.get("verify_cmd"), "base": base, "commits": commits,
               "head": self._git(wt, "rev-parse", "HEAD").strip(),
               "diffstat": self._git(wt, "diff", "--shortstat", "%s...HEAD" % base).strip(),
               "dirty": self._git(wt, "status", "--porcelain").strip()}
        if t.get("verify_cmd"):
            try:
                p = subprocess.run(t["verify_cmd"], shell=True, cwd=wt, capture_output=True, timeout=timeout)
                outp = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
                res.update(exit_code=p.returncode, output_tail=outp[-3000:])
            except subprocess.TimeoutExpired:
                res.update(exit_code="timeout", output_tail="시간 초과 %ss" % timeout)
        else:
            res.update(exit_code=None, output_tail="검증 명령 없음")
        res["passed"] = res.get("exit_code") == 0 and bool(commits)
        kind = "verified" if res["passed"] else "verify_failed"
        self.store.update_task(task_id, verification=json.dumps(res, ensure_ascii=False), verified=1 if res["passed"] else 0,
                               workspace=wt, commit_sha=res["head"],
                               status="verified_done" if res["passed"] else "verify_failed", checked_at=now())
        ekey = "verify:%s:%s:%s" % (task_id, res["head"], res.get("exit_code"))
        self.store.record_event(ekey, "main:verify", kind, task_id, t["project_id"],
                                "Main 이 %s 에서 `%s` 실행 → exit %s, 커밋 %d개" % (wt, res["cmd"], res.get("exit_code"), len(commits)),
                                {"cmd": res["cmd"], "exit_code": res.get("exit_code"), "head": res["head"]})
        t = self.store.task(task_id)
        body = [self._notice_head("완료 검증 통과" if res["passed"] else "완료 보고 받았으나 검증 실패", t),
                "Main 직접 확인: `%s` → exit %s (작업 공간 %s, HEAD %s)" % (res["cmd"], res.get("exit_code"), wt, res["head"][:10]),
                "기준(%s) 뒤 커밋: %s" % (base, "; ".join(commits) or "없음"),
                "변경: %s" % (res["diffstat"] or "없음")]
        if t.get("reported_summary"):
            body.append("보고(검증 전 내용): " + t["reported_summary"].splitlines()[-1][:600])
        if res["passed"]:
            self.store.add_notice("notice:" + ekey, kind, "\n".join(body), task_id)
        else:
            # 검증 실패는 Worker 수준 오류다. Supervisor 가 먼저 고치게 하고, 못 고치면 Main 에 올라온다.
            self.escalate_to_supervisor(t, ekey, "검증 실패", "\n".join(body[1:]) + "\n출력 끝부분:\n" +
                                        (res.get("output_tail") or "")[-800:])
        self.sync_vault()
        return res

    def _base_ref(self, ppath):
        for ref in ("main", "master"):
            if subprocess.run(["git", "-C", ppath, "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0:
                return ref
        return "HEAD~1"

    @staticmethod
    def _git(cwd, *args):
        p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, timeout=60)
        return (p.stdout or b"").decode("utf-8", "replace")

    # ------------------------------------------------------------ 알림 문구
    def _notice_head(self, what, t):
        return "[AO 알림] %s · %s (%s) · %s" % ("{nid}", t["task_id"], t["project_id"], what) + "\n제목: %s" % t["title"]

    def _question_notice(self, q, t):
        opts = json.loads(q["options"]) if q.get("options") else []
        lines = ["[AO 질문] %s · %s · %s (%s)" % ("{nid}", q["question_id"], t["task_id"], t["project_id"]),
                 "제목: %s" % t["title"],
                 "질문(Worker %s → Supervisor %s): %s" % (q.get("worker_session") or "-", q.get("asked_by") or "-", q["text"])]
        if opts:
            lines.append("선택지: " + " / ".join(opts))
        lines.append("처리: 사용자 대신 정하지 말고 사용자에게 그대로 묻습니다. 답을 받으면 "
                     "`%s answer %s --text \"<답>\" --reason \"<이유>\"`." % (aohub_cmd(self.cfg), q["question_id"]))
        return "\n".join(lines)

    def render_notice(self, n):
        text = n["text"].replace("{nid}", n["notice_id"])
        cmd = aohub_cmd(self.cfg)
        tail = "\n이 알림을 사용자에게 보고한 뒤 `%s ack %s` 를 실행하세요. 현재 상태는 `%s status %s` 로 AO 에 다시 확인합니다." % (
            cmd, n["notice_id"], cmd, n.get("task_id") or "")
        return text + tail

    # ------------------------------------------------------------ 감시
    def poll_once(self, auto_verify=True):
        """AO 상태를 한 번 읽어 기록한다. 새로 기록한 이벤트 수를 돌려준다."""
        new = 0
        open_tasks = self.store.tasks(open_only=True)
        by_project = {}
        for t in open_tasks:
            by_project.setdefault(t["project_id"], []).append(t)
        for pid, tasks in by_project.items():
            try:
                sessions = {s["id"]: s for s in self.ao.sessions(pid, include_terminated=True)}
                reports = self.ao.reports(pid)
            except (AOError, OSError, ValueError) as e:
                self.store.record_event("poll-error:%s:%s" % (pid, now()[:16]), "main:watch", "poll_error",
                                        project_id=pid, summary=str(e)[:300])
                continue
            for t in tasks:
                tid = t["task_id"]
                for s in sessions.values():  # 이름이 작업 ID 로 시작하는 worker 도 묶는다
                    if s.get("role") == "worker" and (s.get("displayName") or "").startswith(tid):
                        self.store.add_worker(tid, s["id"])
                t = self.store.task(tid)
                workers = json.loads(t.get("worker_sessions") or "[]")
                state = {}
                for w in workers:
                    s = sessions.get(w)
                    if s:
                        state[w] = {"status": s.get("status"), "activity": s.get("activity"),
                                    "terminated": s.get("isTerminated")}
                sup = t.get("supervisor_session")
                if sup and sup in sessions:
                    state[sup] = {"status": sessions[sup].get("status"), "activity": sessions[sup].get("activity"),
                                  "terminated": sessions[sup].get("isTerminated"), "role": "supervisor"}
                    if sessions[sup].get("isTerminated"):
                        if self.store.record_event("sup-terminated:%s:%s" % (tid, sup), "ao", "supervisor_terminated",
                                                   tid, pid, "Supervisor %s 종료" % sup):
                            new += 1
                            self.store.add_notice("notice:sup-terminated:%s:%s" % (tid, sup), "blocked",
                                                  self._notice_head("Supervisor 종료", t) +
                                                  "\nSupervisor %s 가 끝났습니다. 작업을 이어 가려면 다시 배정하세요." % sup, tid)
                for w in workers:  # idle 로 보여도 turn 이 실패했을 수 있다(예: 지원 안 되는 모델)
                    s = sessions.get(w) or {}
                    if s.get("isTerminated") or s.get("activity") == "active":
                        continue
                    try:
                        turns = ((self.ao.session(w, transcript=1, cap=200).get("transcript") or {}).get("turns") or [])
                    except AOError:
                        continue
                    for turn in turns:
                        if turn.get("state") != "failed":
                            continue
                        msg = (turn.get("errorMessage") or "")[:400]
                        ekey = "turn-failed:%s:%s" % (w, turn.get("id"))
                        if self.store.record_event(ekey, "ao:%s" % w, "worker_failed",
                                                   tid, pid, "Worker %s turn 실패: %s" % (w, msg)):
                            new += 1
                            # Supervisor 가 먼저 잡도록 기록만 해 둔다. 유예 시간 안에 반응이 없으면 check_failures 가 알린다.
                            with self.store.tx() as db:
                                db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (
                                    "fail:%s" % ekey, json.dumps({
                                        "task_id": tid, "at": now(), "what": "Worker 실행 실패", "ekey": ekey,
                                        "detail": "Worker %s 의 turn 이 실패했습니다(세션은 idle 로 보임): %s" % (w, msg)},
                                        ensure_ascii=False)))
                    state.setdefault(w, {})["last_turn"] = turns[-1].get("state") if turns else None
                self.store.update_task(tid, ao_state=json.dumps(state, ensure_ascii=False), checked_at=now())
                for r in reports:
                    if r.get("sessionId") not in workers:
                        continue
                    key = "ao-report:%s:%s:%s" % (r.get("id"), r.get("state"), r.get("repeatCount"))
                    if self.store.record_event(key, "worker:%s" % r.get("sessionId"), "report_%s" % r.get("state"),
                                               tid, pid, (r.get("note") or "")[:1000], r):
                        new += 1
                self.check_failures(self.store.task(tid))
                self.check_escalations(self.store.task(tid))
                t = self.store.task(tid)
                if auto_verify and t["status"] == "reported_done" and not t.get("verify_cmd"):
                    self.needs_review(t)  # 실행해 확인할 명령이 없는 일(문서, 조사 등): 사용자 확인으로 끝낸다
                    new += 1
                elif auto_verify and t["status"] == "reported_done":
                    try:
                        self.verify(tid)
                        new += 1
                    except (AOError, OSError) as e:
                        self.store.record_event("verify-error:%s:%s" % (tid, short_hash(str(e))), "main:verify",
                                                "verify_error", tid, pid, str(e)[:300])
        self.sync_vault(quiet=True)
        return new

    # ------------------------------------------------------------ 검증 명령이 없는 일: 사용자 확인
    def needs_review(self, t):
        """완료 보고는 받았지만 Main 이 실행해 확인할 명령이 없다. 결과물을 사용자에게 보여 주고 accept 로 끝낸다."""
        tid = t["task_id"]
        self.store.update_task(tid, status="needs_review", checked_at=now())
        last = (t.get("reported_summary") or "").splitlines()[-1:] or ["(보고 내용 없음)"]
        ekey = "review:%s:%s" % (tid, short_hash(t.get("reported_summary") or ""))
        self.store.record_event(ekey, "main:verify", "needs_review", tid, t["project_id"],
                                "검증 명령이 없어 사용자 확인 필요")
        self.store.add_notice("notice:" + ekey, "review", "\n".join([
            self._notice_head("완료 보고, 사용자 확인 필요", t),
            "완료 조건: %s" % t["done_criteria"],
            "보고(검증 전 내용): %s" % last[0][:600],
            "실행해 확인할 검증 명령이 없는 작업입니다. 결과물(문서, 파일, 링크)을 열어 사용자에게 보여 주고 확인을 받으세요.",
            "확인되면 `%s accept %s --note \"<사용자가 확인한 내용>\"`, 고칠 점이 있으면 `%s tell %s --text \"<고칠 점>\"`." % (
                aohub_cmd(self.cfg), tid, aohub_cmd(self.cfg), tid),
        ]), tid)

    def accept(self, task_id, note=None):
        """사용자가 결과를 확인했다. 검증 명령 없는 작업을 완료로 닫는다."""
        t = self.store.task(task_id)
        if not t:
            raise ValueError("모르는 작업 ID: %s" % task_id)
        if t["status"] == "verified_done":
            return {"task_id": task_id, "status": "verified_done", "note": "이미 완료"}
        if t["status"] not in ("needs_review", "reported_done", "verify_failed"):
            raise ValueError("%s 는 아직 완료 보고 전입니다(상태 %s)." % (task_id, t["status"]))
        res = {"at": now(), "manual": True, "cmd": None, "passed": True,
               "note": note or "사용자 확인", "exit_code": None}
        self.store.update_task(task_id, status="verified_done", verified=1, checked_at=now(),
                               verification=json.dumps(res, ensure_ascii=False))
        self.store.record_event("accept:%s" % task_id, "main:user", "accepted", task_id, t["project_id"],
                                "사용자 확인으로 완료: %s" % (note or "-"), {"note": note})
        for n in self.store.notices():
            if n.get("task_id") == task_id and n["kind"] == "review":
                self.store.ack_notice(n["notice_id"])
        self.sync_vault()
        return {"task_id": task_id, "status": "verified_done"}

    # ------------------------------------------------------------ Worker 수준 오류: Supervisor 먼저
    def escalate_to_supervisor(self, t, ekey, what, detail):
        """Worker 수준 오류는 해당 Supervisor 에 먼저 알린다. Supervisor 가 없거나 전달이 안 되면 바로 Main 에 알린다.
        Supervisor 가 escalate_seconds 안에 움직이지 않으면 check_escalations 가 Main 에 올린다."""
        tid, sid = t["task_id"], t.get("supervisor_session")
        body = "\n".join([
            "[알림] %s %s" % (tid, what),
            detail,
            "이 작업의 Supervisor 로서 직접 처리하세요(원인 확인, worker 정리·다시 띄우기·다시 지시).",
            "다시 띄운 worker 는 sup-report --kind worker 로, 진행은 --kind progress 로 알립니다.",
            "AO 설정·모델·권한처럼 프로젝트 밖의 결정이 필요하거나 해결할 수 없으면 sup-report --kind blocked 로 올리세요.",
        ])
        result = None
        if sid:
            try:
                result = self.deliver("%s:esc:%s:%s" % (tid, short_hash(ekey), sid), sid, "[알림] %s %s" % (tid, what),
                                      body, task_id=tid, kind="escalation", confirm_seconds=10)
            except AOError as e:
                result = "failed: %s" % str(e)[:200]
        if result in ("confirmed", "sent"):
            with self.store.tx() as db:
                db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)",
                           ("esc:%s" % ekey, json.dumps({"task_id": tid, "at": now(), "what": what, "detail": detail[:600]},
                                                       ensure_ascii=False)))
            self.store.record_event("esc:%s" % ekey, "main:watch", "escalated_to_supervisor", tid, t["project_id"],
                                    "Supervisor %s 에 먼저 알림(%s): %s" % (sid, result, what))
        else:
            self.store.add_notice("notice:" + ekey, "blocked", self._notice_head(what, t) + "\n" + detail +
                                  "\nSupervisor 에 알리지 못했습니다(%s)." % (result or "Supervisor 없음"), tid)

    def _supervisor_moved(self, task_id, since):
        return [e for e in self.store.events(task_id=task_id, since=since)
                if e["source"].startswith("supervisor:") and e["kind"] in ("worker", "progress", "done", "question", "blocked")]

    def check_failures(self, t):
        """Worker 실패를 Supervisor 가 스스로 잡았는지 본다. 유예 시간이 지나도록 반응이 없을 때만 Supervisor 에 알린다."""
        import datetime as _dt
        grace = float(error_policy(self.cfg).get("supervisor_grace_seconds") or 0)
        for row in self.store.db.execute("SELECT key, value FROM meta WHERE key LIKE 'fail:%'").fetchall():
            info = json.loads(row["value"])
            if info.get("task_id") != t["task_id"] or info.get("closed"):
                continue
            moved = self._supervisor_moved(t["task_id"], info["at"])
            closed = None
            if moved or t["status"] in ("reported_done", "verified_done", "cancelled"):
                closed = "caught_by_supervisor"
                self.store.record_event("fail-closed:%s" % row["key"], "main:watch", "caught_by_supervisor",
                                        t["task_id"], t["project_id"],
                                        "%s 를 Supervisor 가 직접 처리함: %s" % (info["what"], (moved[0]["summary"] if moved else t["status"])[:200]))
            else:
                age = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(info["at"])).total_seconds()
                if age >= grace:
                    closed = "escalated"
                    self.escalate_to_supervisor(t, info["ekey"], info["what"], info["detail"] +
                                                "\n(Main 감시가 %d초 동안 Supervisor 처리 보고를 보지 못해 보내는 알림입니다)" % int(grace))
            if closed:
                info["closed"] = closed
                with self.store.tx() as db:
                    db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(info, ensure_ascii=False), row["key"]))

    def worker_state(self, session_id):
        """Supervisor 가 worker 상태를 직접 확인하는 명령. AO 의 idle 에 가려진 turn 실패를 드러낸다."""
        s = self.ao.session(session_id, transcript=1, cap=300)
        sess = s.get("session") or {}
        turns = (s.get("transcript") or {}).get("turns") or []
        last = turns[-1] if turns else {}
        activity = (sess.get("activity") or {}).get("state") if isinstance(sess.get("activity"), dict) else sess.get("activity")
        if sess.get("isTerminated"):
            verdict = "terminated"
        elif last.get("state") == "failed":
            verdict = "failed"
        elif activity == "active" or last.get("state") in ("running", "queued", "streaming"):
            verdict = "working"
        else:
            verdict = "idle"
        return {"session": session_id, "verdict": verdict, "status": sess.get("status"), "activity": activity,
                "branch": sess.get("branch"), "commits": (sess.get("branchState") or {}).get("commits"),
                "last_turn": last.get("state"), "error": (last.get("errorMessage") or "")[:600] or None}

    def check_escalations(self, t):
        """Supervisor 에 넘긴 오류가 풀렸는지 본다. escalate_seconds 가 지나도록 Supervisor 보고가 없으면 Main 에 올린다."""
        limit = self.cfg.get("escalate_seconds")
        limit = 600.0 if limit is None else float(limit)
        rows = self.store.db.execute("SELECT key, value FROM meta WHERE key LIKE 'esc:%'").fetchall()
        for row in rows:
            info = json.loads(row["value"])
            if info.get("task_id") != t["task_id"] or info.get("closed"):
                continue
            since = info["at"]
            moved = [e for e in self.store.events(task_id=t["task_id"], since=since)
                     if e["source"].startswith("supervisor:") and e["kind"] in ("worker", "progress", "done", "question")]
            closed = None
            if moved or t["status"] in ("reported_done", "verified_done", "cancelled"):
                closed = "resolved"
            elif t["status"] == "blocked":
                closed = "blocked"  # Supervisor 가 blocked 로 올렸다. 그 보고가 이미 Main 알림을 만들었다
            else:
                import datetime as _dt
                age = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(since)).total_seconds()
                if age >= limit:
                    closed = "timeout"
                    self.store.add_notice("notice:esc-timeout:" + row["key"], "blocked",
                                          self._notice_head(info["what"] + " (Supervisor 미해결)", t) + "\n" + info["detail"] +
                                          "\nSupervisor %s 에 %s 에 알렸지만 %d분 동안 처리 보고가 없습니다." % (
                                              t.get("supervisor_session"), since[11:16], int(limit // 60)), t["task_id"])
            if closed:
                info["closed"] = closed
                with self.store.tx() as db:
                    db.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(info, ensure_ascii=False), row["key"]))
                self.store.record_event("esc-closed:%s" % row["key"], "main:watch", "escalation_" + closed,
                                        t["task_id"], t["project_id"], "%s → %s" % (info["what"], closed))

    # ------------------------------------------------------------ Manager Agent(Main 을 AO 세션으로)
    def setup_manager(self, path, name=None, project_id=None, agent="claude-code"):
        """허브 폴더를 AO 프로젝트로 등록하고 Manager 규칙을 넣고, 살아 있는 Manager 세션이 없으면 띄운다. 다시 불러도 결과가 같다."""
        pid = project_id or self.cfg.get("manager_project") or "hub"
        name = name or self.cfg.get("manager_name") or "Manager Agent"
        path = os.path.abspath(os.path.expanduser(path))
        if subprocess.run(["git", "-C", path, "rev-parse", "--git-dir"], capture_output=True).returncode != 0:
            subprocess.run(["git", "-C", path, "init", "-q", "-b", "main"], check=True)
        if subprocess.run(["git", "-C", path, "rev-parse", "--verify", "-q", "HEAD"], capture_output=True).returncode != 0:
            subprocess.run(["git", "-C", path, "add", "-A"], check=True)
            subprocess.run(["git", "-C", path, "-c", "user.name=aohub", "-c", "user.email=aohub@localhost",
                            "commit", "-q", "--allow-empty", "-m", "허브 폴더 시작"], check=True)
        steps = []
        if not any(p.get("id") == pid for p in self.ao.projects()):
            self.ao.run(["project", "add", "--path", path, "--id", pid, "--name", name,
                         "--orchestrator-agent", agent, "--worker-agent", agent])
            steps.append("AO 프로젝트 %s(%s) 등록" % (pid, name))
        proj = self.ao.project(pid)
        cfg_obj = dict(proj.get("config") or {})
        want = {"defaultBranch": "main"}
        changed = False
        for k, v in want.items():
            if cfg_obj.get(k) != v:
                cfg_obj[k] = v
                changed = True
        ac = dict(cfg_obj.get("agentConfig") or {})
        if not ac.get("permissions"):
            ac["permissions"] = "auto"
            cfg_obj["agentConfig"] = ac
            changed = True
        orch = dict(cfg_obj.get("orchestrator") or {})
        if orch.get("agent") != agent:
            orch["agent"] = agent
            cfg_obj["orchestrator"] = orch
            changed = True
        if changed:
            self.ao.set_config(pid, cfg_obj)
            steps.append("프로젝트 설정(기본 브랜치 main, 권한 auto, orchestrator %s)" % agent)
        if self.ensure_project_rules(pid):
            steps.append("Manager 규칙(orchestratorRules) 갱신")
        live = self._live_orchestrator(pid)
        if live:
            sid = live["id"]
            steps.append("살아 있는 Manager 세션 %s 사용" % sid)
        else:
            sid = self.ao.spawn(pid, name, kind="orchestrator",
                                prompt="%s resume 을 실행해 지금 상태를 확인하고, 사용자에게 Manager Agent 로 준비됐다고 짧게 알려 주세요." % aohub_cmd(self.cfg))
            self._wait_controller(sid)
            steps.append("Manager 세션 %s 생성" % sid)
        self.store.attach_main("ao:" + sid)
        return {"project": pid, "name": name, "session": sid, "steps": steps}

    def write_session_note(self, summary, title=None):
        """지금 Main 대화의 인계 노트를 남기고 경로를 기록한다(resume 이 다음 Main 에게 먼저 보여 준다)."""
        main = self.store.active_main()
        if not main:
            raise ValueError("등록된 Main 이 없습니다.")
        path = self.vault.session_note(self.store, main, summary, title)
        if path:
            with self.store.tx() as db:
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("last_session_note", path))
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                           ("session_note:" + main["session_id"], path))
            self.store.record_event("session-note:%s" % main["session_id"], "main", "session_note",
                                    summary="Main 대화 인계 노트: %s" % path)
        return path

    def manager_reset(self, force=False, note=None, title=None):
        """Manager 세션을 비운다: 지금 세션을 닫고 새 Manager 를 띄운다. 업무 상태는 DB·Vault 에 있어 새 Manager 가 resume 으로 이어 간다."""
        pid = self.cfg.get("manager_project") or "hub"
        live = self._live_orchestrator(pid)
        old = live["id"] if live else None
        if old and not force:
            act = ((self.ao.session(old).get("session") or {}).get("activity") or {})
            if (act.get("state") if isinstance(act, dict) else act) == "active":
                raise ValueError("Manager %s 가 일하는 중입니다. 끝난 뒤 다시 하거나 --force 를 주세요." % old)
        path = (self.ao.project(pid) or {}).get("path")
        if not path:
            raise ValueError("Manager 프로젝트 %s 가 없습니다. 먼저 setup-manager 를 실행하세요." % pid)
        note_path = self.write_session_note(note, title) if note else None
        if old:
            self.ao.kill(old)
            self.store.record_event("manager-reset:%s" % old, "main", "manager_reset", project_id=pid,
                                    summary="Manager 세션 %s 를 닫고 새로 시작(대화 비우기)" % old)
        res = self.setup_manager(path, project_id=pid)
        res["closed"] = old
        res["session_note"] = note_path
        return res

    def deliver_notices_ao(self, ao_session):
        """Main 이 AO 세션일 때: 알림을 ao send(고정 핸들)로 보내고 transcript 도착을 확인한다."""
        main_id = "ao:" + ao_session
        sent = []
        for n in self.store.notices():
            if not self.store.claim_notice_send(n["notice_id"], main_id, self.cfg.get("renotify_seconds") or 0):
                continue
            try:
                res = self.deliver("notice:%s:%s" % (n["notice_id"], ao_session), ao_session, "%s ·" % n["notice_id"],
                                   self.render_notice(n), task_id=n.get("task_id"), kind="notice", confirm_seconds=20)
            except AOError as e:
                self.store.notice_send_failed(n["notice_id"], e)
                continue
            sent.append(n["notice_id"])
            self.store.record_event("notice-sent:%s:%s" % (n["notice_id"], main_id), "main:watch", "notice_sent",
                                    n.get("task_id"), None, "Manager %s 에 ao send 로 %s 전달(%s)" % (ao_session, n["notice_id"], res))
        return sent

    def deliver_notices(self, session_id):
        sent = []
        for n in self.store.notices():
            if not self.store.claim_notice_send(n["notice_id"], session_id, self.cfg.get("renotify_seconds") or 0):
                continue
            try:
                inbox.post(self.render_notice(n))
                sent.append(n["notice_id"])
                self.store.record_event("notice-sent:%s:%s" % (n["notice_id"], session_id), "main:watch", "notice_sent",
                                        n.get("task_id"), None, "Main %s inbox 에 %s 전달" % (session_id, n["notice_id"]))
            except inbox.InboxUnavailable as e:
                self.store.notice_send_failed(n["notice_id"], e)
                raise
        return sent

    def sync_vault(self, quiet=False):
        try:
            return self.vault.sync(self.store)
        except Exception as e:  # 기록 실패가 연동을 멈추지 않게 한다
            self.store.record_event("vault-error:%s" % short_hash(str(e), now()[:16]), "main:vault", "vault_error",
                                    summary=str(e)[:300])
            if not quiet:
                raise
            return []
