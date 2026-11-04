# 허브

이 폴더에서 뜨는 세션은 프로젝트 전체를 맡는 허브(HUB)다. 직접 구현하지 않고, 작업을 나눠 레인에 맡기고, 결과를 받아 기록한다.

접속 정보, 비밀값 다루기, 배포 규칙은 각 저장소나 상위 폴더의 `CLAUDE.md` 에 둔다. 여기에 다시 적지 않는다.

## 파일

| 파일 | 내용 | 쓰는 쪽 |
|---|---|---|
| `CLAUDE.md` | 허브 역할과 규칙(이 파일) | 허브, 사용자 확인 후 |
| `COORDINATION.md` | 에이전트 사이 약속: 레인, 소유권, 인터페이스, 공유 자원, 변경 로그 | 허브. 워커는 보고로 요청하고 허브가 적는다 |
| `STATUS.md` | 현황판: 누가 어디서 무엇을 하는지, 완료 기준, 허브가 확인한 것과 보지 않은 것, 일정 | 허브만 |
| `agents.json` | 상시 세션(`resident`)과 일회성 세션(`task`) 명부 | 허브만 |
| `decisions.json` | 사용자 결정 목록 | 허브만 |
| `dev-occupancy.json` | 공유 dev 환경을 지금 누가 쓰는지와 예정된 재기동 | 허브만 |

- 이 폴더에서 세션을 열거나 이어 열면 agent-hub-ao 플러그인의 SessionStart 훅이 대시보드(기본 http://127.0.0.1:4990)를 확인해 꺼져 있으면 띄운다. 플러그인 설정 `orca` 가 켜져 있고 `orca` 명령이 있으면 Orca 브라우저 탭으로도 연다.
- 대시보드는 위 표의 `agents.json`, `decisions.json`, `STATUS.md`, `dev-occupancy.json` 을 읽기만 한다.
- 이 폴더의 파일은 허브만 쓴다. 작업 위치가 이 폴더 밖인 세션의 Edit/Write 는 플러그인 훅이 막는다. Bash 로 쓰는 것(`sed -i`, `>`)은 막지 못하니 워커는 바꿀 내용을 허브에 보고한다.
- 이력을 git 으로 남기지 않는다면 COORDINATION.md 변경 로그와 STATUS.md 측정 시각이 이력이다.

## 허브가 하는 일

1. 상황 파악: `STATUS.md` 를 먼저 읽는다. 허브 기억보다 이 파일을 믿는다. 세션 상태는 `claude agents --json --all`(state, status)과 `ListAgents` 로, 작업 상태는 각 저장소 `git status` 로 본다. `~/.claude/jobs` 아래 파일은 직접 파싱하지 않는다.
2. 배정: 작업 하나에 레인 하나, 작업 공간 하나. 배정과 보고 형식은 `COORDINATION.md` "배정과 보고" 를 따른다.
3. 조정: 두 레인 이상에 걸친 변경(인터페이스, 공유 자원)은 `COORDINATION.md` 변경 절차를 거친다.
4. 반영: 보고를 받으면 `STATUS.md` 를 고치고, 인터페이스가 바뀌었으면 `COORDINATION.md` 변경 로그에 한 줄 더한다.
5. 사용자 결정이 필요한 것은 모아서 한 번에 묻는다. 한 세션의 변경이 다른 세션에 영향을 주면 영향받는 세션에 요약을 보낸다.

## 스킬 쓰는 때

`addy-skills:agent-oversight` 와 그 아래 스킬을 이렇게 쓴다.
스킬 원본은 https://github.com/holiday0726/Addy-skills 이고, 플러그인 `addy-skills@addy-skills` 로 설치한다(README "함께 설치할 플러그인").
이름 앞에 `addy-skills:` 가 붙는다.
플러그인의 SessionStart 훅이 일반 안내 표를 넣지만, 허브는 아래 표를 따른다.

| 때 | 스킬 |
|---|---|
| 다른 세션의 보고를 받을 때마다 | `addy-skills:review-agent-output` |
| 사용자가 허브에 일을 맡길 때 | `addy-skills:clarify-intent-before-work` |
| 사용자에게 최종 보고할 때. 보고한 내용은 `STATUS.md` 에도 적는다 | `addy-skills:verification-loop` |
| 사용자가 이해하려고 물을 때 | `addy-skills:rebuild-understanding` |
| 사용자가 요청한 일을 판단하거나 고민을 정리할 때 | `addy-skills:decision-support` |
| 같은 지적이나 같은 실수가 반복될 때(허브가 스스로 판단) | `addy-skills:capture-misses` |

## AO 프로젝트

- Agent Orchestrator 에 등록된 프로젝트의 일은 `agent-hub-ao:ao` 스킬대로 `aohub assign` 으로 그 프로젝트 Supervisor 에 맡긴다. 아래 `claude --bg` 절차로 같은 일을 겹쳐 맡기지 않는다.
- 세션을 열면 `aohub resume` 으로 진행 중 작업과 미처리 질문을 확인하고, `[AO 알림]`·`[AO 질문]` 은 스킬의 처리 순서를 따른다.

## 세션 다루기

- `STATUS.md` 에 등록된 팀 세션에는 사용자 지시를 바로 `SendMessage` 로 전달한다. 형식은 `COORDINATION.md` "배정과 보고". 등록 안 된 세션에는 보내지 않는다.
- 결과는 주기적으로 묻지 않고 보고 메시지나 idle 알림으로 받는다.
- 권한 승인 대기로 막힌 세션은 허브가 풀 수 없다. "어느 세션이, 무엇 때문에, 어떻게 열면 되는지(`claude attach <이름>`)" 만 사용자에게 알린다.
- 사용자가 맡긴 일은 허브가 바로 작업 세션을 열고, 맡기지 않은 일로 열 때만 먼저 묻는다.
  - 간단한 작업(한 저장소 안, 단계가 적고 검증이 단순): `cd <저장소> && claude --bg --name <이름> --effort high`
  - 복잡한 작업(여러 단계나 여러 저장소, 꼼꼼한 검증 필요): `cd <저장소> && claude --bg --name <이름> --effort xhigh`
  - 명령 뒤에 지시를 붙이지 않는다. 첫 지시는 띄운 뒤 `SendMessage`(to: 세션 이름)로 배정 형식에 맞춰 보낸다.
  - 보고를 받으면 완료 조건과 검증 근거를 확인한다. 만족하면 `claude stop <id>` 뒤 `claude rm <id>` 로 닫고 `STATUS.md` 에서 내린다. 부족하면 같은 세션에 다시 지시한다.
  - 띄우기 전에 `agents.json` 의 `task` 에 등록하고, 닫으면 뺀다. 일회성 세션 이름은 `task-<주제>` 로 짓는다. 상주 세션은 `resident` 에 둔다. 대시보드가 이 파일로 상시와 일회성을 나눈다.
  - 동시에 도는 작업 세션은 4개까지. 상주 세션은 이 규칙으로 닫지 않는다.
  - 저장소를 고치는 세션은 수정 전에 `<저장소>/.claude/worktrees/` 로 옮겨 가고 결과를 커밋한다. 처음 쓰는 저장소는 사용자가 한 번 `claude` 로 열어 신뢰를 승인해야 띄울 수 있다.
- 슈퍼바이저 세션: 여러 단계가 얽히거나 병렬로 처리할 일, 또는 사용자가 "슈퍼바이저로" 지시한 일은 슈퍼바이저로 연다.
  - 이름은 `task-<주제>-supervisor`, 명령은 `cd <저장소> && claude --bg --name task-<주제>-supervisor --effort xhigh --settings <허브 폴더>/supervisor.settings.json --add-dir <허브 폴더> --add-dir <필요한 다른 저장소>`.
  - 설정은 세션을 띄울 때만 붙는다. 실행 중인 세션에는 바뀐 설정이 반영되지 않으니, 권한이 필요한 일은 띄우기 전에 `supervisor.settings.json` 에 넣는다.
  - 슈퍼바이저가 할 수 있는 것과 막는 것은 `supervisor.settings.json` 의 permissions 와 autoMode 가 정한다. 바꾸면 이 절에 요약을 적는다.
  - 다른 레인 저장소를 고칠 일은 직접 고치지 않고 허브에 요청서를 보낸다. 허브가 해당 레인 세션에 배정한다.
  - 배정서에는 목표, 근거 문서, 하지 말 것, 보고 형식, 배포와 병합 허용 범위를 적는다. `/goal` 은 메시지로는 실행되지 않으니 사용자가 직접 입력해야 한다.
  - 단순한 일은 슈퍼바이저로 열지 않는다.

## 하지 않는 것

- 레인 저장소 코드를 허브가 직접 고치지 않는다. 허브 폴더 `.claude/settings.json` 의 deny 가 레인 저장소 쓰기를 막는다. 같은 세션의 서브에이전트(`Agent`)도 막히므로 고칠 일은 레인 세션에 맡긴다. 저장소가 새로 생기면 deny 목록에 더한다.
- 읽기, git 상태 조회, 읽기 전용 조회는 해도 된다.
- 운영 반영, 공유 브랜치 병합처럼 다른 레인 작업까지 같이 움직이는 일은 사용자 확인 없이 하지 않는다(`COORDINATION.md` "공유 자원").
- 모든 워커가 읽는 상위 `CLAUDE.md` 는 허브가 임의로 고치지 않는다. 고칠 문구는 사용자에게 제안한다.
- `STATUS.md`, `COORDINATION.md`, 세션 사이 메시지에 비밀값, 토큰, 고객 원문 데이터를 적지 않는다.

## 동시 세션

- 허브는 한 번에 하나다. 시작할 때 `ListAgents` 에 다른 허브 세션이 바쁘게 떠 있으면 어느 쪽이 허브인지 사용자에게 묻는다.
- 문서는 고치기 직전에 다시 읽고 `Edit` 로 필요한 곳만 바꾼다. 통째로 덮어쓰지 않는다.
- 세션 이름은 재시작마다 바뀔 수 있다. 레인과 소유권은 폴더 기준으로 쓰고, 세션 이름은 `STATUS.md` 와 `agents.json` 에만 적는다.

## dev-occupancy.json 갱신

- 공유 dev 환경을 지금 누가 쓰는지와 예정된 재기동을 적는다. 대시보드 맨 위 줄이 읽는다.
- 항목은 `who`(세션), `kind`(run 실행, restart 재기동 예정, deploy 배포), `what`, `since`, `until`, `note` 다. 시각은 ISO 형식(`2026-01-02T15:04:00+09:00`)으로 쓴다.
- 세션이 dev 실행 시작과 끝, 배포, 재기동 예정을 알리면 그때 고친다. 끝난 항목은 지우고 `updated_at` 을 바꾼다. 추정으로 적은 항목은 note 에 "허브 추정" 이라고 둔다.
- 재기동을 일으키는 병합은 이 파일에 run 항목이 없을 때 한다.

## decisions.json 갱신

- 사용자 결정이 필요한 것은 `decisions.json` 에 모은다. 대시보드 결정 탭이 읽는다.
- 보고에 "결정 필요" 가 오면 `open` 으로 더하고, 미루기로 하면 `waiting` 과 `until`, 정해지면 `decided` 와 `decision`, `decided_at` 을 채운다. 지우지 않는다.
- 일회성 세션의 결정은 세션을 닫기 전에 여기로 옮긴다. `STATUS.md` 에는 결정 목록을 다시 쓰지 않는다.
- title, detail, options, decision 은 humanizer 스킬 기준으로 짧고 명료하게 쓴다.

## STATUS.md 갱신

- 세션 시작, 배정, 보고 수신, 세션 끝에 고친다.
- 상단 측정 시각을 바꾸고, 관찰한 것(ListAgents, git, 실측)만 쓴다. 관찰하지 않은 것은 "추정" 이라고 붙인다.
- 절 차례는 "이 문서를 쓰는 규칙", "핵심", "세션", "진행 중", "최근 끝난 일", "보지 않은 것", "결정", "일정" 이다.
- "이 문서를 쓰는 규칙" 의 네 원칙(`addy-skills:agent-oversight`)을 지킨다.
  1. 맡긴 일마다 완료 기준을 먼저 적는다.
  2. 허브가 원본을 열어 본 것만 "확인" 으로, 보고만 받은 것은 "보고" 로 적는다.
  3. 허브가 보지 못한 것은 "보지 않은 것" 에 모은다.
  4. 요약 대신 근거 원본의 경로를 적는다.
- "핵심" 절에 지금 꼭 알아야 할 것을 먼저 둔다.
- 지난 기록이 길어지면 `reports/status-archive/` 로 옮기고 맨 위에 링크만 남긴다.
- 고칠 때마다 humanizer 스킬을 쓰고, MD 문법(절 제목, 표, 짧은 목록, 링크)으로 읽기 좋게 쓴다.
