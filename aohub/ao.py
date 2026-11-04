"""AO CLI 와 loopback API 를 부르는 얇은 클라이언트.

생성, 전송, 종료 같은 쓰기는 모두 `ao` CLI 로 한다. 읽기 전용 보고 목록(GET /api/v1/reports)만
CLI 명령이 없어 running.json 의 loopback 포트로 직접 읽는다.
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

from . import config


class AOError(RuntimeError):
    def __init__(self, msg, rc=None, out="", err="", uncertain=False):
        super().__init__(msg)
        self.rc, self.out, self.err, self.uncertain = rc, out, err, uncertain


class AO:
    def __init__(self, cfg=None, runner=None):
        self.cfg = cfg or config.load()
        self.bin = config.find_ao(self.cfg)
        self._runner = runner  # 시험용 대체 실행기

    # ---- 실행 ----
    def run(self, args, timeout=90, check=True):
        if self._runner:
            return self._runner(args, timeout)
        if not self.bin:
            raise AOError("ao 실행 파일을 찾지 못했습니다. config.json 의 ao_bin 을 채우세요.")
        env = dict(os.environ)
        # Main 세션 안에서 부를 때 AO 가 이 프로세스를 다른 세션으로 착각하지 않게 한다.
        for k in ("AO_SESSION_ID", "AO_PROJECT_ID"):
            env.pop(k, None)
        try:
            p = subprocess.run([self.bin] + list(args), capture_output=True, timeout=timeout, env=env,
                               encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as e:
            # 보냈는지 모른다. 부른 쪽이 기록과 세션을 다시 확인해야 한다.
            raise AOError("ao %s 시간 초과(%ss)" % (args[0], timeout), uncertain=True,
                          out=e.stdout or "", err=e.stderr or "")
        if check and p.returncode != 0:
            raise AOError("ao %s 실패(exit %s): %s" % (" ".join(args[:2]), p.returncode, (p.stderr or p.stdout).strip()[:800]),
                          rc=p.returncode, out=p.stdout, err=p.stderr)
        return p.returncode, p.stdout, p.stderr

    def run_json(self, args, timeout=90):
        _, out, _ = self.run(args, timeout)
        try:
            return json.loads(out)
        except ValueError:
            raise AOError("ao %s 의 JSON 출력을 읽지 못했습니다: %s" % (" ".join(args[:2]), out[:300]))

    # ---- 데몬 ----
    def status(self):
        rc, out, _ = self.run(["status", "--json"], check=False)
        try:
            return json.loads(out)
        except ValueError:
            return {"state": "unknown", "raw": out[:300], "rc": rc}

    def ready(self):
        st = self.status()
        return st.get("state") == "ready" or st.get("ready") == "ready"

    def _port(self):
        path = os.environ.get("AO_RUN_FILE") or os.path.expanduser("~/.ao/running.json")
        with open(path, encoding="utf-8") as f:
            return int(json.load(f)["port"])

    def api_get(self, path, params=None, timeout=15):
        url = "http://127.0.0.1:%d%s" % (self._port(), path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 프로젝트와 세션 ----
    def projects(self):
        data = self.run_json(["project", "ls", "--json"])
        return data.get("projects", data.get("data", data)) if isinstance(data, dict) else data

    def project(self, project_id):
        return self.run_json(["project", "get", project_id, "--json"]).get("project")

    def orchestrators(self, project_id=None):
        rows = self.run_json(["orchestrator", "ls", "--json"]).get("data", [])
        return [r for r in rows if not project_id or r.get("projectId") == project_id]

    def sessions(self, project_id=None, include_terminated=False):
        args = ["session", "ls", "--json", "-a"]
        if include_terminated:
            args.append("--include-terminated")
        if project_id:
            args += ["-p", project_id]
        return self.run_json(args).get("data", [])

    def session(self, session_id, transcript=0, cap=1000):
        args = ["session", "get", session_id, "--json"]
        if transcript:
            args += ["--transcript", str(transcript), "--cap", str(cap)]
        return self.run_json(args)

    def reports(self, project_id):
        return self.api_get("/api/v1/reports", {"projectId": project_id}).get("reports", [])

    def worktree(self, project_path, branch):
        """세션 브랜치를 체크아웃한 작업 트리 경로. AO 응답에 경로가 없어 git 으로 찾는다."""
        p = subprocess.run(["git", "-C", project_path, "worktree", "list", "--porcelain"], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=30)
        cur = None
        for line in p.stdout.splitlines():
            if line.startswith("worktree "):
                cur = line[9:].strip()
            elif line.startswith("branch ") and line.strip().endswith("refs/heads/" + branch):
                return cur
        return None

    # ---- 쓰기 ----
    def spawn(self, project_id, name, prompt=None, kind="worker", agent=None, timeout=180):
        args = ["spawn", "--project", project_id, "--name", name, "--kind", kind]
        if prompt:
            args += ["--prompt", prompt]
        if agent:
            args += ["--agent", agent]
        _, out, _ = self.run(args, timeout=timeout)
        m = re.search(r"spawned session (\S+)", out)
        if not m:
            raise AOError("spawn 결과에서 세션 id 를 찾지 못했습니다: " + out[:300], uncertain=True)
        return m.group(1)

    def send(self, session_id, message, client_message_id=None, timeout=90):
        """--steer 와 고정 핸들로 보낸다. 쉬는 세션이면 새 turn, 일하는 중이면 그 turn 에 더해진다.
        같은 핸들로 다시 부르면 데몬이 두 번 전달하지 않는다."""
        args = ["send", "--session", session_id, "--message", message, "--steer"]
        if client_message_id:
            args += ["--client-message-id", client_message_id]
        _, out, _ = self.run(args, timeout=timeout)
        return out.strip()

    def recover_send(self, session_id, client_message_id, timeout=60):
        """불확실했던 전송의 결과를 다시 보내지 않고 묻는다. 기록이 없으면 AOError."""
        _, out, _ = self.run(["send", "--session", session_id, "--steer", "--recover-only",
                              "--client-message-id", client_message_id], timeout=timeout)
        return out.strip()

    def set_config(self, project_id, cfg_obj):
        return self.run_json(["project", "set-config", project_id, "--config-json",
                              json.dumps(cfg_obj, ensure_ascii=False), "--json"])

    def kill(self, session_id):
        return self.run(["session", "kill", session_id])[1]


def transcript_texts(session_json):
    """session get --transcript --json 의 항목에서 사람이 읽을 글만 뽑는다."""
    tr = (session_json or {}).get("transcript") or {}
    texts = []
    for e in tr.get("entries", []):
        for key in ("text", "content", "message"):
            v = e.get(key)
            if isinstance(v, str):
                texts.append(v)
        d = e.get("detail") or {}
        if isinstance(d, dict):
            for key in ("text", "command"):
                v = d.get(key)
                if isinstance(v, str):
                    texts.append(v)
    return texts
