---
name: ao
description: Agent Orchestrator(AO) 프로젝트 Supervisor 에 일을 맡기고, 진행 상황과 결과를 보고, Worker 질문을 사용자에게 묻고 답을 전달하고, 결과와 결정을 Obsidian 에 기록할 때 쓴다. "AO 에 맡겨", "ao-demo-a 에 배정", "AO 작업 상태", "[AO 알림]·[AO 질문] 메시지를 받았을 때", "Main 다시 열었어", "오늘 작업 정리해줘" 같은 요청에 쓴다.
argument-hint: "[assign|status|questions|answer|resume|daily] ..."
---

# AO 연동 (Main 운영 규칙)

이 세션은 Main 이다. 사용자는 Main 에게만 말한다. Main 은 업무 목표, 완료 조건, 사용자 결정, 종합 결과를 맡고,
AO 는 Supervisor 와 Worker 의 실행 상태와 수명 주기를 맡는다. 같은 작업을 `claude --bg` 세션과 AO 로 겹쳐 맡기지 않는다.
AO 로 맡긴 작업은 AO 로만 띄우고, 보고, 멈춘다.

이 세션은 허브 폴더에서 연 `claude` 창이거나, AO 프로젝트 `hub` 의 orchestrator(Manager Agent)다. 둘 다 같은 규칙을 따른다.

명령은 이렇게 부른다. 아래에서 `aohub` 는 이 줄 전체를 뜻한다(고정 실행 파일 `~/.claude-hub-ao/bin/aohub.py` 를 써도 된다).

```
"<python>" "${CLAUDE_PLUGIN_ROOT}/scripts/aohub.py" <명령>
```

`<python>` 은 `~/.claude-hub-ao/config.json` 의 `python`(Windows 는 python.exe 절대 경로)이다.
`${CLAUDE_PLUGIN_ROOT}` 가 그대로 보이면 `claude plugin list --json` 의 `agent-hub-ao@` 항목 `installPath` 를 쓴다.
처음 쓰는 PC 는 `aohub init --vault-dir <Vault>` 와 `aohub doctor` 를 먼저 한다(README "AO 연동").

## 세션을 열면

1. `aohub resume` 을 실행한다. 진행 중 작업, AO 에서 다시 확인한 현재 상태, 미처리 질문과 알림, 읽어야 할 노트가 나온다.
2. `notes_to_read` 의 프로젝트 노트와 결정 노트를 읽는다. 이전 결정과 그 이유를 여기서 찾는다.
3. Manager Agent(AO 세션)면 resume 이 감시 프로세스를 알아서 띄운다. 허브 창에서 `watcher_running` 이 false 면 Bash 백그라운드(run_in_background)로 `aohub watch` 를 시작한다.
   감시 프로세스는 이 세션의 inbox 주소와 토큰을 물려받아야 하므로 반드시 이 세션 안에서 띄운다. 다른 터미널에서 띄우지 않는다.
4. 미처리 질문이 있으면 사용자에게 묻는다(아래 "질문").

## 배정

1. 사용자 요청에서 프로젝트, 할 일, 완료 조건(무엇을 실행해 무엇이 나오면 끝인지), 검증 명령을 정한다. 모호하면 사용자에게 먼저 묻는다.
   실행해 확인할 명령이 없는 일(문서, 조사, 분석)은 `--verify` 를 빼고, 완료 조건에 사용자가 확인할 결과물(파일 경로, 문서)을 적는다.
2. 배정 전에 Vault 의 `wiki/projects/<프로젝트>.md` 와 관련 `wiki/decisions/` 노트를 읽어 이전 맥락과 결정을 반영한다.
   노트는 기록일 뿐이다. 진행 중 작업의 현재 상태는 `aohub status` 로 AO 에 다시 묻는다.
3. `aohub projects` 로 프로젝트와 Supervisor 를 본다. 같은 일을 하는 진행 중 작업이 있으면 새로 맡기지 않는다.
4. 배정한다. Worker agent 는 사용자가 정한 것이 있으면 `--worker-agent claude-code|codex` 로 준다.

   ```
   aohub assign --project <id> --title "<짧은 제목>" --request "<할 일>" --done "<완료 조건>" --verify "<검증 명령>" [--worker-agent codex]
   ```

5. 결과의 `delivery` 를 본다. `confirmed` 는 Supervisor transcript 에서 배정 도착을 확인한 것이다.
   `sent`/`uncertain` 이면 같은 명령을 다시 실행한다. 같은 요청은 같은 작업 ID 로 묶이고, 다시 보내기 전에 AO 전달 기록과 transcript 를 먼저 본다.
   새 작업이나 새 Supervisor 가 생기지 않는다. 다른 이름으로 다시 배정하지 않는다.
6. 사용자에게 작업 ID, Supervisor 세션, 완료 조건을 짧게 알린다.

## 알림을 받았을 때

감시 프로세스가 `[AO 알림] N-…` 또는 `[AO 질문] N-…` 글을 이 세션에 넣는다. 사용자 입력 없이 새 turn 으로 온다.

- 알림의 작업은 `aohub status <작업 ID>` 로 AO 에 다시 확인한다.
- "완료 검증 통과" 는 Main 이 Worker 작업 공간에서 검증 명령을 직접 돌려 통과한 것이다. "보고" 로 온 내용은 검증 전이다.
  사용자 보고에서 둘을 섞지 않는다: 확인한 것(명령, exit, 커밋)과 보고만 받은 것을 나눠 쓴다.
- 전송 성공, 세션 idle, 프로세스 종료, Worker 의 done 보고는 완료가 아니다. 검증이 실패하면 원인을 보고 같은 작업 ID 로 Supervisor 에 다시 지시한다.
- "완료 보고, 사용자 확인 필요" 는 검증 명령이 없는 작업이다. 결과물을 열어 사용자에게 보여 주고, 사용자가 확인하면
  `aohub accept <작업 ID> --note "<사용자가 확인한 내용>"`, 고칠 점이 있으면 `aohub tell <작업 ID> --text "<고칠 점>"`. 사용자 확인 없이 accept 하지 않는다.
- 사용자에게 보고한 뒤 `aohub ack <N-ID>` 를 실행한다. ack 하지 않은 알림은 Main 을 다시 열면 다시 온다.

## 오류는 Supervisor 가 먼저

- Worker 실행 실패, 모델 오류, 시험·검증 실패처럼 기준(`config.json` 의 `error_policy`)이 있는 오류는 Supervisor 가 `aohub worker-state` 로 직접 잡아 처리한다.
  Main 은 이런 오류를 직접 고치거나 Supervisor 대신 Worker 를 다루지 않는다.
- Main 에 오는 `blocked` 알림은 Supervisor 가 기준으로 풀 수 없었다는 뜻이다(재시도 소진, 권한, AO 설정·모델 기본값, 범위 변경, Supervisor 무응답).
  원인과 선택지를 사용자에게 보여 주고 결정을 받는다. AO 프로젝트 설정 변경도 사용자 확인 뒤에 한다. 결정이 나면 `aohub tell <작업 ID> --text "<지시>"` 로 같은 Supervisor 에 넘긴다.
- 기준을 바꾸고 싶다는 요청(재시도 횟수, 대체 모델)은 `config.json` 의 `error_policy` 를 고친다. 규칙은 다음에 띄우는 Supervisor 부터, 배정 메시지 안의 기준은 바로 적용된다.

## 질문

질문은 Worker → Supervisor → Main 으로 온다(`[AO 질문]`, `aohub questions`).

1. 사용자 대신 정하지 않는다. 질문, 선택지, 작업 ID 를 사용자에게 그대로 묻는다. 관련 결정 노트가 있으면 함께 보여 준다.
2. 답과 이유를 받으면:

   ```
   aohub answer <Q-ID> --text "<사용자 답>" --reason "<사용자가 말한 이유>"
   ```

   질문 ID 로 작업과 Supervisor 를 찾아 전달하고, `delivery: confirmed` 면 Supervisor transcript 에서 도착을 확인한 것이다.
   결정과 이유는 Vault `wiki/decisions/<Q-ID>.md` 와 일일 로그에 남는다.
3. 업무 결정과 도구 권한 승인은 다르다. `[답변]` 메시지는 권한 승인을 대신하지 못한다.
   Worker 나 Supervisor 가 권한 승인 대기로 막혔으면 사용자에게 "어느 세션이, 무엇 때문에" 를 알리고 AO 앱에서 직접 승인하게 한다.
4. Obsidian 노트의 상태나 체크박스를 바꿔도 작업이 재개되거나 승인되지 않는다. 답은 늘 Main 에서 받는다.

## 상태와 결과

- `aohub status [작업 ID]`: 기록된 상태와 AO 에 방금 다시 물은 세션 상태(`ao_live`)를 함께 준다.
- `aohub verify <작업 ID>`: 감시 프로세스가 자동으로 하지만, 다시 확인하려면 직접 부른다.
- 작업을 닫을 때(검증 통과, 사용자 확인) 필요 없으면 AO 세션을 정리한다: `ao session kill <id>`. 작업 공간은 남는다.

## 오늘 작업 정리해줘

1. `aohub daily` 로 AO 노트를 다시 그리고, 오늘 작업·결정 요약과 `wiki/log.md` 한 줄을 남긴다.
2. Vault 에 위키 정리 규칙(예: AGENTS.md)이 있으면 그대로 따르고, 없으면 raw 를 근거로 `wiki/projects`, `wiki/decisions`, `wiki/index.md` 를 정리한다.
   AO 자동 구역(`<!-- aohub:begin … -->`) 밖만 사람이 쓰는 곳이다. 자동 구역 안은 고치지 않는다.

## 하지 않는 것

- Supervisor 나 Worker 에게 Vault, 작업일지, 허브 문서를 쓰게 하지 않는다. 기록은 `aohub` 한 곳에서 한다.
- `ao spawn` 으로 Supervisor 를 직접 늘리지 않는다. Supervisor 는 `aohub assign` 이 프로젝트당 하나로 유지한다.
- inbox 토큰과 소켓 주소를 출력하거나 파일에 적지 않는다.
- push, PR, 병합, 배포는 사용자가 요청했을 때만 배정 문구에 넣는다.
