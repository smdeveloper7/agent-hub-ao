# AO 연동 (aohub)

Main Claude Code 세션(허브)이 [Agent Orchestrator](https://github.com/OrchestratorInc/agent-orchestrator)(AO)의 프로젝트별 Supervisor 에 일을 맡기고, 결과와 질문을 받아 사용자에게 보고하고, Obsidian Vault 에 기록합니다.

```
사용자 ⇄ Main Claude Code ──assign/answer/tell──▶ AO Supervisor(orchestrator) ──ao spawn/send──▶ Worker(Claude Code, Codex)
            ▲  inbox                                   │ sup-report(작업 ID)              │ ao report
            │                                          ▼                                  ▼
       aohub watch ◀── AO 상태·보고 다시 읽기 ── 상태 DB(~/.claude-hub-ao) ──▶ 기록 모듈 ──▶ Obsidian Vault
```

## 역할

| 쪽 | 맡는 것 | 하지 않는 것 |
|---|---|---|
| Main | 업무 목표, 완료 조건, 사용자 질문과 결정, 종합 보고, 최종 검증 확인 | Worker 직접 실행, 노트에 실행 상태를 믿고 판단 |
| AO | Supervisor·Worker 세션 실행 상태와 수명 주기, Worker 보고 전달 | 사용자 결정 |
| Supervisor | Worker 배정·재지시, Worker 수준 오류 처리, 사용자 판단 필요 시 질문 올리기 | 사용자 대신 결정, Vault·작업일지 기록 |
| aohub | 작업 ID, 이벤트·전달 기록, 중복 방지, Main 알림, 노트 갱신(한 곳) | AO 를 우회한 세션 제어 |

- AO 로 맡긴 작업은 AO 로만 띄우고 멈춥니다. 같은 작업을 `claude --bg` 세션으로 겹쳐 맡기지 않습니다.

### 오류는 누가 잡나

기준만 있으면 처리할 수 있는 오류는 Supervisor 가 잡고, 판단이 필요한 오류만 Main(사용자)에게 옵니다. Main 감시는 안전망입니다.

| 단계 | 누가 | 무엇을 |
|---|---|---|
| 1 | Supervisor | Worker 를 띄운 직후와 기다리는 동안 `aohub worker-state <worker>` 로 상태를 직접 봅니다(AO 의 `idle` 에 가려진 turn 실패 포함). `error_policy` 기준대로 처리하고 `progress` 로 알립니다: 실행 실패는 같은 지시로 `worker_retries` 회까지 다시 띄움, 모델 미지원은 `fallback_models` 로 다시 띄움, 시험·검증 실패는 실패 출력과 함께 `test_retries` 회까지 다시 지시 |
| 2 | Supervisor | 기준에 없는 오류, 재시도를 다 쓴 오류, 권한 승인, AO 설정·모델 기본값 변경, 범위·완료 조건 변경은 `blocked`(판단이면 `question`)로 Main 에 올립니다 |
| 3 | Main 감시(안전망) | Worker 실패를 기록만 합니다. `supervisor_grace_seconds`(기본 180초) 안에 Supervisor 보고가 있으면 `caught_by_supervisor` 로 닫고, 없으면 Supervisor 에 `[알림]` 을 한 번 보냅니다. Main 이 직접 찾은 검증 실패는 바로 Supervisor 에 `[알림]` 을 보냅니다 |
| 4 | Main 감시(안전망) | `[알림]` 뒤 `escalate_seconds`(기본 600초) 동안 Supervisor 처리 보고가 없거나 Supervisor 가 없으면 Main 에 알립니다 |

기준은 `config.json` 의 `error_policy` 에 두고, Supervisor 규칙(`orchestratorRules`)과 모든 배정 메시지에 들어갑니다. 규칙은 새로 띄운 Supervisor 부터, 배정 메시지 기준은 바로 적용됩니다.

## 설치와 실행

필요한 것: Claude Code 2.1.234 이상(native Windows 의 세션 inbox), Python 3.8 이상, Agent Orchestrator 데스크톱 앱(데몬과 `ao` CLI 포함).

1. AO 를 설치하고 데몬을 띄웁니다. 앱을 열거나, 앱 없이 `ao daemon` 을 실행합니다.
   Windows 설치본의 CLI 는 `%LOCALAPPDATA%\Programs\agent-orchestrator\resources\daemon\ao.exe` 입니다(PATH 에 없어도 aohub 가 찾습니다).
2. 저장소를 AO 프로젝트로 등록합니다. 원격이 없는 저장소는 기본 브랜치를 꼭 지정합니다(안 하면 Supervisor 가 `idle` 로 보이지만 시작하지 못합니다).

   ```
   ao project add --path <저장소> --id <id> --orchestrator-agent claude-code --worker-agent codex
   ao project set-config <id> --permission auto --worker-agent codex --orchestrator-agent claude-code --default-branch main
   ```

   `set-config` 는 설정을 통째로 바꿉니다. 플래그로 쓸 때는 필요한 값을 한 번에 다 주거나, `ao project get <id> --json` 의 config 를 고쳐 `--config-json` 으로 넣습니다.
   ChatGPT 계정 Codex 는 AO 기본 모델(`gpt-6.1-sol`)을 거부할 수 있습니다. 그때는 worker 모델을 지정합니다(`config.worker.agentConfig.model`, 예 `gpt-5.6-sol`).
3. aohub 설정을 만듭니다. 상태 폴더는 플러그인 폴더와 따로 `~/.claude-hub-ao` 입니다(`AOHUB_HOME` 으로 변경).

   ```
   python <플러그인>/scripts/aohub.py init --vault-dir <Obsidian Vault> [--raw-script <Vault>/scripts/worklog.ps1]
   python <플러그인>/scripts/aohub.py doctor
   python <플러그인>/scripts/aohub.py setup-project <id>      # orchestratorRules 에 연동 규칙(이후 띄우는 Supervisor 부터 적용)
   ```

4. 허브 폴더에서 Main 세션을 엽니다. 플러그인의 SessionStart 훅(`hooks/ao_watch_start.py`)이 세션 이름을 `hub`(설정 `hub_title`)로 붙이고 `aohub watch` 를 이 세션에 붙여 띄웁니다.
   훅을 쓰지 않으면 Main 안에서 Bash 백그라운드로 `aohub watch` 를 실행합니다. 감시 프로세스는 Main 세션의 inbox 주소와 토큰을 물려받아야 하므로 다른 터미널에서 띄우면 안 됩니다.
5. `/agent-hub-ao:ao` 스킬의 규칙대로 씁니다.

설정 파일 `~/.claude-hub-ao/config.json` 예:

```json
{
  "ao_bin": "C:/Users/<나>/AppData/Local/Programs/agent-orchestrator/resources/daemon/ao.exe",
  "python": "C:/Users/<나>/AppData/Local/Programs/Python/Python313/python.exe",
  "vault_dir": "C:/Users/<나>/projects/worklog",
  "raw_dir": "raw",
  "wiki_dir": "wiki",
  "raw_script": "C:/Users/<나>/projects/worklog/scripts/worklog.ps1",
  "raw_agent": "Claude",
  "supervisor_prefix": "sup-",
  "poll_seconds": 5,
  "renotify_seconds": 0,
  "escalate_seconds": 600,
  "error_policy": {
    "worker_retries": 1,
    "test_retries": 2,
    "fallback_models": {"codex": "gpt-5.6-sol"},
    "supervisor_grace_seconds": 180
  }
}
```

## Main 두 가지 모드

| 모드 | Main 이 사는 곳 | 알림 경로 | 감시 프로세스 |
|---|---|---|---|
| 허브 창 | 허브 폴더에서 연 `claude` | 세션 inbox(named pipe·Unix 소켓) | 그 세션에 붙어 SessionStart 훅이 띄움 |
| Manager Agent(권장) | AO 프로젝트 `hub` 의 orchestrator 세션 | `ao send --steer`(고정 핸들), transcript 도착 확인 | Manager 의 `resume` 이 떼어 띄움(`watch --ao-main`) |

Main 은 늘 하나입니다. 마지막에 `resume` 하거나 열린 쪽이 Main 을 이어받고, 이전 감시 프로세스는 스스로 멈춥니다.
Manager 프로젝트에는 `aohub assign` 으로 일을 맡기지 않습니다. `setup-manager` 가 프로젝트 등록, 규칙, 세션을 준비합니다.

규칙·배정·알림에 적는 aohub 명령은 `~/.claude-hub-ao/bin/aohub.py` 입니다. 깔린 플러그인 중 가장 새 판을 부르는 고정 실행 파일이라 업데이트 뒤에도 끊기지 않습니다.

## 명령

| 명령 | 쓰는 쪽 | 하는 일 |
|---|---|---|
| `init`, `doctor` | Main | 설정, 연결 점검(ao, 데몬, Vault, inbox, 감시 프로세스) |
| `projects` | Main | AO 프로젝트, 살아 있는 Supervisor, 연동 규칙 여부, 진행 중 작업 수 |
| `setup-project <id>` | Main | 프로젝트 `orchestratorRules` 에 연동 규칙 블록을 넣음(다른 설정 보존) |
| `assign` | Main | 작업 ID 발급, Supervisor 확보(프로젝트당 하나), `[배정]` 전달과 도착 확인 |
| `status [작업]` | Main | 기록 + AO 에 방금 다시 물은 세션 상태(`ao_live`) |
| `questions`, `answer <Q> --text --reason` | Main | 판단 대기 목록, 사용자 답을 해당 Supervisor 에 `[답변]` 으로 전달 |
| `tell <작업> --text` | Main | 같은 작업의 Supervisor 에 후속 지시 |
| `verify <작업>` | Main/감시 | Worker 작업 공간에서 검증 명령 실행, 커밋·변경 확인. 검증 명령이 없으면 `확인 필요` 로 둠 |
| `accept <작업> --note` | Main | 검증 명령 없는 작업을 사용자 확인으로 완료 |
| `notices`, `ack <N>` | Main | Main 에 보낸 알림, 처리 기록 |
| `watch` | Main 세션 안 | 주기적으로 AO 를 읽고 알림을 Main inbox 로 보냄. `--ao-main <세션>` 이면 Manager 에 ao send |
| `setup-manager --path --name` | 처음 한 번 | 허브 폴더를 AO 프로젝트로 등록하고 Manager Agent 세션과 감시 프로세스 준비 |
| `resume` | Main | 재시작 뒤 Main 등록, 진행 중 작업·질문·알림, 읽을 노트 |
| `poll`, `sync`, `daily` | Main | 한 번 읽기, 노트 다시 그리기, 오늘 정리와 `wiki/log.md` 한 줄 |
| `sup-report --task --kind worker\|progress\|question\|done\|blocked` | Supervisor | Main 으로 가는 유일한 통로. DB 에만 쓴다 |
| `worker-state <worker>` | Supervisor | Worker 실제 상태 판정(`failed`/`working`/`idle`/`terminated`), 마지막 turn 오류 |

## 전달과 중복 방지

- 배정·답변·지시·오류 알림은 `ao send --steer --client-message-id <고정 핸들>` 로 보냅니다. 핸들은 `작업:종류:대상` 처럼 결정적이라 재시도해도 같습니다.
- `ao spawn`/`ao send` 의 성공 출력은 '보냈다' 일 뿐입니다. Supervisor transcript 에서 표시(`[배정] T-…`, `[답변] Q-…`, `[지시] T-… #해시`)를 찾아야 `confirmed` 로 기록합니다.
- 결과가 불확실하면(시간 초과, 전송 중 프로세스 종료로 `pending` 이 남음) 다시 보내기 전에 `ao send --recover-only` 와 transcript 를 먼저 봅니다. AO 에 기록이 있으면 다시 보내지 않고, 없을 때만 같은 핸들로 한 번 보냅니다.
- 같은 요청(프로젝트·제목·요청·완료 조건·검증 명령)은 같은 작업 ID 로 묶입니다(`--key` 로 직접 지정 가능). Supervisor 는 DB 의 `spawning` 표시와 `ao orchestrator ls` 로 프로젝트당 하나만 띄웁니다.
- AO 보고(`ao-report:<id>:<state>:<repeat>`), Supervisor 보고(`sup:<작업>:<종류>:<해시>`), Worker turn 실패(`turn-failed:<세션>:<turn>`), 검증(`verify:<작업>:<HEAD>:<exit>`)은 이벤트 키로 한 번만 기록합니다. 알림도 이벤트 키당 하나입니다.
- 알림은 Main 세션마다 한 번 보냅니다. ack 하지 않은 알림은 다른 Main 세션이 등록되면 다시 갑니다.

## 완료 판정

전송 성공, 세션 idle, 프로세스 종료, Worker `done` 보고, Supervisor `done` 보고는 완료가 아닙니다.
Supervisor 가 `done` 을 올리면 감시 프로세스가 Worker 브랜치의 작업 트리를 `git worktree list` 로 찾아 검증 명령을 직접 돌리고,
exit 0 이고 기준 브랜치 뒤 커밋이 있을 때만 `verified_done` 으로 둡니다. 보고 원문은 노트의 "보고만 받은 것" 에 따로 둡니다.

검증 명령이 없는 작업(문서, 조사)은 `needs_review`(확인 필요)로 두고 Main 에 알립니다. Main 이 결과물을 사용자에게 보여 주고, 사용자가 확인하면 `aohub accept` 로 `verified_done` 이 됩니다. Supervisor 에 실패로 되돌리지 않습니다.

AO 의 `idle` 은 turn 실패도 포함합니다. 감시 프로세스는 idle Worker 의 마지막 turn 상태를 읽어 `failed` 를 잡습니다.

## Obsidian 기록

기록 모듈(`aohub/vault.py`) 하나가 상태 DB 를 다시 그려 노트를 만듭니다. Worker 와 Supervisor 는 노트를 쓰지 않습니다. 쓰기는 `vault.lock` 아래에서 합니다.

| 노트 | 내용 |
|---|---|
| `raw/YYYY-MM-DD.md` | 완료 검증, 질문 발생, 결정을 한 건씩 덧붙임. `raw_script` 가 있으면 공통 스크립트로, `<!-- wl:ao… -->` 표시로 한 번만 |
| `wiki/tasks/<작업 ID>.md` | Properties(작업·프로젝트·Supervisor·Worker·상태·확인 시각·완료 조건·결과물·커밋·검증 여부·결정 링크·일일 로그 링크), 요청, 검증(Main 확인), 보고만 받은 것, 진행 기록(확인/보고 구분) |
| `wiki/decisions/<질문 ID>.md` | 질문, 선택지, 경로(Worker→Supervisor→Main), 답, 이유, 전달 확인 |
| `wiki/projects/<프로젝트>.md` | AO 작업 표, 판단 대기, 최근 결정. 기존 노트면 자동 구역만 덧붙임 |
| `wiki/AO 현황.base` | Bases 보기: 프로젝트 현황, 판단 대기, 완료 작업, 결정 기록 |
| `wiki/index.md` | `ao-index` 자동 구역(보기와 프로젝트 링크) |
| `wiki/log.md` | `aohub daily` 가 한 줄 덧붙임 |

- 자동으로 고치는 곳은 맡은 Properties 키와 `<!-- aohub:begin 이름 -->` ~ `<!-- aohub:end 이름 -->` 사이뿐입니다. 사용자가 더한 Properties 와 본문 메모는 그대로 둡니다.
- 노트의 상태나 체크박스를 바꿔도 실행 승인이나 작업 재개가 일어나지 않습니다. 답은 Main 에서 받습니다.
- Obsidian 앱 제어(공식 CLI `Obsidian.com`)는 쓰지 않습니다. 로컬 Markdown 읽기·쓰기만으로 충분했습니다.

## 재시작·오류·질문 대기

| 상황 | 처리 |
|---|---|
| Main 재시작 | 새 세션이 열리면 훅이 감시 프로세스를 새 세션에 붙입니다. 새 세션이 Main 으로 등록되면 옛 감시 프로세스는 스스로 끝납니다. Main 은 `aohub resume` 으로 진행 중 작업(AO 재확인), 미처리 질문, ack 안 된 알림, 읽을 프로젝트·결정 노트를 받습니다 |
| 감시 프로세스 재시작 | 같은 Main 세션에 이미 보낸 알림은 다시 보내지 않습니다. 세션당 하나만 돕니다(`locks/watch-<세션>.lock`) |
| AO 데몬 꺼짐 | `assign` 은 거절합니다. 감시는 오류를 `poll_error` 이벤트로 남기고 계속 돕니다 |
| Supervisor 시작 실패 | spawn 이 `idle` 을 돌려줘도 컨트롤러가 `ready` 가 될 때까지 확인하고, 안 되면 오류로 알립니다 |
| Supervisor 종료 | Main 에 알림. 같은 작업을 다시 배정하면 새 Supervisor 를 띄웁니다 |
| Worker turn 실패 | Supervisor 가 `worker-state` 로 잡아 기준대로 처리. 못 잡으면 유예 뒤 Main 감시가 Supervisor 에 `[알림]`, 그래도 미해결이면 Main 에 |
| 검증 실패 | Main 감시가 Supervisor 에 `[알림]`(재지시는 Supervisor), 미해결이면 Main 에 |
| 질문 대기 | `wiki/decisions/<Q>.md` 가 `open`, Bases "판단 대기" 에 보입니다. Main 이 사용자에게 묻고 `answer` 로 전달합니다 |
| 권한 승인 대기 | `[답변]` 으로 풀 수 없습니다. 사용자가 AO 앱에서 해당 세션을 직접 승인합니다 |

## 알려진 제약

- inbox 주입 형식(`{"type":"auth"}` 다음 `{"type":"user",...}` 한 줄)은 공식 문서에 auth 줄만 나오고 메시지 줄은 Claude Code 2.1.318 의 로그 안내문에서 확인했습니다. 판이 바뀌면 다시 확인해야 합니다.
- inbox 쓰기 성공은 전달 통로에 들어갔다는 뜻입니다. Main 이 처리했는지는 `ack` 로만 압니다.
- Supervisor 의 셸이 Git Bash 면 `--text` 안의 Windows 경로가 바뀌어 들어올 수 있습니다(`C;C:\Program Files\Git\...`). 보고 원문은 검증 전 내용으로만 다루고, 경로·커밋은 Main 검증 값을 씁니다.
- Supervisor 와 Worker 도 사용자 전역 `~/.claude/CLAUDE.md` 를 읽습니다. 전역 규칙이 작업일지 기록을 요구하면 Supervisor 가 따로 기록할 수 있어, 연동 규칙에 "기록은 Main 이 한다" 를 넣었습니다(규칙은 새로 띄운 Supervisor 부터 적용).
- 감시는 5초 주기 조회입니다. AO SSE 이벤트는 쓰지 않습니다.
