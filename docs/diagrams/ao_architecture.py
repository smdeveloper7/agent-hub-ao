"""README 구성도(agent-hub-ao) 생성: python docs/diagrams/ao_architecture.py

밝은 화면·어두운 화면 HTML 두 개를 만든다. PNG 는 헤드리스 브라우저로 찍는다(Edge 또는 Chrome).

    msedge --headless=new --hide-scrollbars --force-device-scale-factor=1.5 --window-size=1064,668       --virtual-time-budget=6000 --screenshot=docs/images/ao-architecture.png file:///<저장소>/docs/diagrams/ao-architecture.html
    (어두운 화면은 ao-architecture-dark.html → docs/images/ao-architecture-dark.png)
원본 agent-hub 구성도(architecture.html)의 색, 글꼴, 점 무늬를 따른다.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

LIGHT = dict(name="ao-architecture", bg="#f5f5f5", ink="#2d3142", muted="#5a6580", line="#4f5d75",
             accent="#bf4520", accent_fill="rgba(191,69,32,0.08)", node="#ffffff", dots="rgba(45,49,66,0.1)",
             group_fill="rgba(45,49,66,0.025)", group_stroke="rgba(45,49,66,0.14)", eyebrow="#4f5d75")
DARK = dict(name="ao-architecture-dark", bg="#2d3142", ink="#f5f5f5", muted="#bfc0c0", line="#949eb2",
            accent="#f08a59", accent_fill="rgba(240,138,89,0.10)", node="#393e53", dots="rgba(245,245,245,0.1)",
            group_fill="rgba(245,245,245,0.03)", group_stroke="rgba(245,245,245,0.14)", eyebrow="#bfc0c0")

SANS = "'Geist', 'Noto Sans KR', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif"
MONO = "'Geist Mono', ui-monospace, monospace"

TITLE = "agent-hub-ao 구성"
CLAIM = ("사용자는 Manager Agent 와 대화하고 Obsidian 으로 현황을 봅니다. Manager 는 AO CLI 로 프로젝트별 Supervisor 에 맡기고, "
         "Supervisor 는 Worker 를 띄웁니다. Supervisor 보고와 감시 프로세스가 읽고 직접 검증한 결과가 상태 DB 에 쌓이고, "
         "알림은 ao send 로 Manager 에게, 기록은 Obsidian 노트로 갑니다.")


def svg(c):
    o = []
    a = o.append

    def text(x, y, s, size=12, color=None, weight=None, font=SANS, anchor="middle", ls=None):
        extra = (' font-weight="%s"' % weight if weight else "") + (' letter-spacing="%s"' % ls if ls else "")
        a('<text x="%s" y="%s" fill="%s" font-size="%s" font-family="%s" text-anchor="%s"%s>%s</text>'
          % (x, y, color or c["ink"], size, font, anchor, extra, s))

    def label(x, y, s, color=None, mono=False, w=None):
        """선 위 글자: 배경색 띠를 깔아 선과 겹쳐도 읽히게."""
        size = 10 if mono else 12
        w = w or (len(s) * (6.2 if mono else 11.5) + 10)
        a('<rect x="%s" y="%s" width="%s" height="16" rx="2" fill="%s"/>' % (x - w / 2, y - 12, w, c["bg"]))
        text(x, y, s, size=size, color=color or c["line"], weight=None if mono else 500, font=MONO if mono else SANS)

    def node(x, y, w, h, tag, title, sub, accent=False, stack=False):
        if stack:  # 여러 개라는 표시: 뒤에 한 장 더
            a('<rect x="%s" y="%s" width="%s" height="%s" rx="6" fill="%s" stroke="%s" stroke-width="1" opacity="0.55"/>'
              % (x + 7, y - 7, w, h, c["node"], c["line"]))
        a('<rect x="%s" y="%s" width="%s" height="%s" rx="6" fill="%s"/>' % (x, y, w, h, c["bg"]))
        a('<rect x="%s" y="%s" width="%s" height="%s" rx="6" fill="%s" stroke="%s" stroke-width="%s"/>'
          % (x, y, w, h, c["accent_fill"] if accent else c["node"], c["accent"] if accent else c["ink"],
             1.3 if accent else 1))
        tw = len(tag) * 5.6 + 10
        a('<rect x="%s" y="%s" width="%s" height="13" rx="2" fill="none" stroke="%s" stroke-opacity="0.5" stroke-width="0.8"/>'
          % (x + 12, y + 10, tw, c["accent"] if accent else c["muted"]))
        text(x + 12 + tw / 2, y + 19.5, tag, size=7, color=c["accent"] if accent else c["muted"], font=MONO, ls="0.08em")
        text(x + w / 2, y + h / 2 + 8, title, size=14, weight=600)
        text(x + w / 2, y + h / 2 + 25, sub, size=9.5, color=c["muted"], font=MONO)

    def group(x, y, w, h, caption):
        a('<rect x="%s" y="%s" width="%s" height="%s" rx="10" fill="%s" stroke="%s" stroke-width="0.9"/>'
          % (x, y, w, h, c["group_fill"], c["group_stroke"]))
        cw = len(caption) * 5.4 + 16
        a('<rect x="%s" y="%s" width="%s" height="12" rx="2" fill="%s"/>' % (x + 14, y - 6, cw, c["bg"]))
        text(x + 14 + cw / 2, y + 3.5, caption, size=7.5, color=c["muted"], font=MONO, ls="0.14em")

    def path(d, accent=False, dashed=False, start=False, width=None):
        col = c["accent"] if accent else c["line"]
        mk = "acc" if accent else "arr"
        a('<path d="%s" fill="none" stroke="%s" stroke-width="%s"%s marker-end="url(#%s-%s)"%s/>'
          % (d, col, width or (1.5 if accent else 1.2), ' stroke-dasharray="4,3"' if dashed else "", c["name"], mk,
             ' marker-start="url(#%s-%s-s)"' % (c["name"], mk) if start else ""))

    def halo(d):  # 다른 선 위를 지나갈 때 끊어 보이게
        a('<path d="%s" fill="none" stroke="%s" stroke-width="7"/>' % (d, c["bg"]))

    W, H = 1000, 500
    a('<svg viewBox="0 38 %d %d" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="%s">' % (W, H - 46, CLAIM))
    a("<defs>")
    a('<pattern id="%s-dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="0.9" fill="%s"/></pattern>'
      % (c["name"], c["dots"]))
    for mk, col in (("arr", c["line"]), ("acc", c["accent"])):
        a('<marker id="%s-%s" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="%s"/></marker>'
          % (c["name"], mk, col))
        a('<marker id="%s-%s-s" markerWidth="8" markerHeight="6" refX="1" refY="3" orient="auto"><polygon points="8 0, 0 3, 8 6" fill="%s"/></marker>'
          % (c["name"], mk, col))
    a("</defs>")
    a('<rect width="%d" height="%d" fill="%s"/>' % (W, H, c["bg"]))
    a('<rect width="%d" height="%d" fill="url(#%s-dots)" opacity="0.55"/>' % (W, H, c["name"]))

    # 무리
    group(222, 58, 756, 174, "AGENT ORCHESTRATOR · AO 데몬")
    group(222, 300, 756, 132, "AGENT-HUB-AO · aohub · ~/.claude-hub-ao")

    # 선(상자보다 먼저 그려 상자가 위에 오게)
    path("M 182,132 H 248", accent=True, start=True)                                   # 사용자 ↔ Manager
    path("M 106,162 V 330", start=True, width=1.4)                                     # 사용자 ↔ Obsidian
    path("M 160,330 V 214 Q 160,206 168,206 H 312 Q 320,206 320,198 V 172", dashed=True)  # Obsidian → Manager
    path("M 442,120 H 518", accent=True)                                               # Manager → Supervisor
    path("M 711,116 H 778")                                                            # Supervisor → Worker
    path("M 778,146 H 712")                                                            # Worker → Supervisor(뒤 카드 밖에서 멈춤)
    path("M 612,170 V 330")                                                            # Supervisor → DB
    path("M 870,172 V 330", dashed=True)                                               # AO → 감시(읽기·검증)
    path("M 778,368 H 702")                                                            # 감시 → DB
    path("M 518,368 H 442")                                                            # DB → 기록
    path("M 248,368 H 182")                                                            # 기록 → Obsidian
    notice = "M 806,330 V 276 Q 806,268 798,268 H 418 Q 410,268 410,260 V 172"
    halo(notice)
    path(notice, accent=True)                                                          # 감시 → Manager(알림)

    # 상자
    node(30, 104, 150, 58, "USER", "사용자", "AO 앱 · Obsidian")
    node(248, 96, 194, 74, "MAIN", "Manager Agent", "AO 프로젝트 hub", accent=True)
    node(518, 96, 184, 74, "SUPERVISOR", "Supervisor", "프로젝트마다 하나", stack=True)
    node(778, 96, 184, 74, "WORKER", "Coding Worker", "Claude Code · Codex", stack=True)
    node(30, 332, 150, 72, "OBSIDIAN", "Obsidian Vault", "raw · wiki · 세션")
    node(248, 332, 194, 72, "RECORD", "기록 모듈", "DB → 노트 다시 그리기")
    node(518, 332, 184, 72, "STATE", "상태 DB", "작업·질문·결정·알림")
    node(778, 332, 184, 72, "WATCH", "감시 프로세스", "aohub watch")

    # 선 이름
    label(215, 124, "대화", color=c["accent"])
    label(480, 112, "배정·답변", color=c["accent"])
    label(480, 140, "ao send", color=c["accent"], mono=True)
    label(744, 108, "ao spawn", mono=True)
    label(744, 164, "ao report", mono=True)
    label(652, 254, "sup-report", mono=True)
    label(510, 290, "알림·질문 (ao send)", color=c["accent"])
    label(916, 254, "읽기·검증", w=64)
    label(740, 360, "이벤트")
    label(480, 360, "다시 그리기")
    label(215, 360, "노트")
    label(240, 200, "노트·메모 읽기", mono=False)
    text(98, 238, "현황 보기", size=12, color=c["line"], weight=500, anchor="end")
    text(98, 254, "메모", size=12, color=c["line"], weight=500, anchor="end")

    # 범례
    y = 470
    a('<line x1="230" y1="%d" x2="262" y2="%d" stroke="%s" stroke-width="1.5"/>' % (y, y, c["accent"]))
    text(270, y + 4, "Manager 를 오가는 흐름(대화, 배정, 알림)", size=11, color=c["muted"], anchor="start")
    a('<line x1="540" y1="%d" x2="572" y2="%d" stroke="%s" stroke-width="1.2"/>' % (y, y, c["line"]))
    text(580, y + 4, "명령·기록", size=11, color=c["muted"], anchor="start")
    a('<line x1="680" y1="%d" x2="712" y2="%d" stroke="%s" stroke-width="1.2" stroke-dasharray="4,3"/>' % (y, y, c["line"]))
    text(720, y + 4, "읽기", size=11, color=c["muted"], anchor="start")
    a("</svg>")
    return "\n".join(o)


def page(c):
    return """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&family=Noto+Sans+KR:wght@500;600&family=Noto+Serif+KR&display=swap" rel="stylesheet">
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: {bg}; color: {ink}; font-family: 'Geist', 'Noto Sans KR', sans-serif; padding: 2.5rem 2rem 2rem; }}
.frame {{ max-width: 1000px; margin: 0 auto; }}
.eyebrow {{ font-family: 'Geist Mono', monospace; font-size: 0.66rem; letter-spacing: 0.18em; text-transform: uppercase; color: {eyebrow}; margin-bottom: 0.5rem; }}
h1 {{ font-family: 'Instrument Serif', 'Noto Serif KR', serif; font-weight: 400; font-size: 2rem; margin-bottom: 1.25rem; }}
figure svg {{ width: 100%; height: auto; display: block; }}
figcaption {{ margin-top: 0.75rem; font-size: 0.82rem; line-height: 1.6; color: {muted}; }}
</style>
</head>
<body>
<div class="frame">
<p class="eyebrow">Architecture / agent-hub-ao</p>
<h1>{title}</h1>
<figure>
{svg}
<figcaption>{claim}</figcaption>
</figure>
</div>
</body>
</html>
""".format(title=TITLE, claim=CLAIM, svg=svg(c), **c)


if __name__ == "__main__":
    for c in (LIGHT, DARK):
        path = os.path.join(HERE, c["name"] + ".html")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(page(c))
        print(path)
