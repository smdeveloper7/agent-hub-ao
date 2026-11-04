"""설정과 상태 폴더.

상태 폴더는 플러그인 설치 폴더와 분리한다. 기본은 ~/.claude-hub-ao, 환경변수 AOHUB_HOME 으로 바꾼다.
설정 파일은 <상태 폴더>/config.json 이다. 없는 키는 기본값을 쓴다.
"""
import json
import os
import shutil
import sys

DEFAULTS = {
    # ao CLI 실행 파일. 비우면 PATH 의 ao, 그다음 설치 위치를 찾는다.
    "ao_bin": "",
    # Supervisor 가 연동 명령을 부를 때 쓸 Python 절대 경로. 비우면 지금 Python.
    "python": "",
    # Obsidian Vault 폴더. 비우면 기록하지 않는다.
    "vault_dir": "",
    # Vault 안의 하위 폴더 이름.
    "raw_dir": "raw",
    "wiki_dir": "wiki",
    # 일일 로그를 남길 공통 스크립트(worklog.ps1). 비우면 내장 기록기를 쓴다.
    "raw_script": "",
    # 일일 로그 기록자 이름(worklog.ps1 -Agent).
    "raw_agent": "Claude",
    # Supervisor 세션 이름 앞붙이.
    "supervisor_prefix": "sup-",
    # 감시 주기(초).
    "poll_seconds": 5,
    # 같은 알림을 같은 Main 세션에 다시 보내기 전 기다리는 시간(초). 0 이면 다시 보내지 않는다.
    "renotify_seconds": 0,
    # Worker 수준 오류를 Supervisor 에 넘긴 뒤 Main 에 올리기까지 기다리는 시간(초).
    "escalate_seconds": 600,
    # Supervisor 가 스스로 처리하는 오류 기준. Supervisor 규칙(orchestratorRules)과 배정 메시지에 들어간다.
    # 기준에 없는 오류, 재시도를 다 쓴 오류, 권한·설정·범위 변경은 Supervisor 가 blocked/question 으로 Main 에 올린다.
    "error_policy": {
        "worker_retries": 1,          # Worker 실행 실패(turn failed, 세션 종료) 때 같은 지시로 다시 띄우는 횟수
        "test_retries": 2,            # 검증·시험 실패 때 실패 출력을 붙여 다시 지시하는 횟수
        "fallback_models": {},        # 모델 미지원 오류 때 쓸 대체 모델. 예 {"codex": "gpt-5.6-sol"}
        "supervisor_grace_seconds": 180,  # Worker 실패를 Supervisor 가 먼저 잡도록 Main 감시가 기다리는 시간
    },
}


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("AOHUB_HOME") or "~/.claude-hub-ao"))


def config_path():
    return os.path.join(home(), "config.json")


def load():
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save(cfg):
    os.makedirs(home(), exist_ok=True)
    data = {k: cfg[k] for k in cfg if k in DEFAULTS}
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, config_path())


def python_bin(cfg):
    return (cfg.get("python") or sys.executable).replace("\\", "/")


def find_ao(cfg):
    """ao 실행 파일 경로. 찾지 못하면 None."""
    explicit = (cfg.get("ao_bin") or os.environ.get("AOHUB_AO_BIN") or "").strip()
    if explicit:
        return explicit
    found = shutil.which("ao")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates.append(os.path.join(local, "Programs", "agent-orchestrator", "resources", "daemon", "ao.exe"))
    else:
        candidates += [
            "/Applications/Agent Orchestrator.app/Contents/Resources/daemon/ao",
            os.path.expanduser("~/Applications/Agent Orchestrator.app/Contents/Resources/daemon/ao"),
            "/opt/Agent Orchestrator/resources/daemon/ao",
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None
