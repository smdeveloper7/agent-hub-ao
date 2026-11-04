"""작업, 질문, 이벤트, 알림, 전달 기록을 담는 SQLite 저장소.

여러 프로세스(Main 의 명령, 감시 프로세스, Supervisor 가 부르는 보고 명령)가 같이 쓴다.
WAL 과 busy_timeout 으로 동시 쓰기를 순서대로 처리하고, 번호 발급은 BEGIN IMMEDIATE 안에서 한다.
같은 이벤트는 event_key 기본 키로 한 번만 들어간다.
"""
import datetime as _dt
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from . import config

KST = _dt.timezone(_dt.timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  request_key TEXT UNIQUE,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  request TEXT NOT NULL,
  done_criteria TEXT NOT NULL,
  verify_cmd TEXT,
  worker_agent TEXT,
  status TEXT NOT NULL,
  supervisor_session TEXT,
  worker_sessions TEXT NOT NULL DEFAULT '[]',
  workspace TEXT,
  commit_sha TEXT,
  reported_summary TEXT,
  verification TEXT,
  verified INTEGER,
  ao_state TEXT,
  checked_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions(
  question_id TEXT PRIMARY KEY,
  event_key TEXT UNIQUE,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  asked_by TEXT,
  worker_session TEXT,
  text TEXT NOT NULL,
  options TEXT,
  status TEXT NOT NULL,
  answer TEXT,
  reason TEXT,
  answered_at TEXT,
  delivered_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  event_key TEXT PRIMARY KEY,
  task_id TEXT,
  project_id TEXT,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices(
  notice_id TEXT PRIMARY KEY,
  event_key TEXT UNIQUE,
  task_id TEXT,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_session TEXT,
  sent_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  acked_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY,
  task_id TEXT,
  target_session TEXT,
  kind TEXT NOT NULL,
  marker TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supervisors(
  project_id TEXT PRIMARY KEY,
  session_id TEXT,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mains(
  session_id TEXT PRIMARY KEY,
  pid INTEGER,
  watcher_pid INTEGER,
  attached_at TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
"""

TERMINAL_TASK = ("verified_done", "cancelled")


def now():
    return _dt.datetime.now(KST).isoformat(timespec="seconds")


def today():
    return _dt.datetime.now(KST).strftime("%Y%m%d")


def db_path():
    return os.path.join(config.home(), "state.sqlite3")


class Store:
    def __init__(self, path=None):
        self.path = path or db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def tx(self):
        """BEGIN IMMEDIATE: 쓰기 잠금을 먼저 잡아 번호 발급과 확인-후-쓰기를 한 덩어리로."""
        for attempt in range(50):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.1)
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ---- 번호 ----
    def _next_id(self, db, prefix, width=2):
        day = today()
        key = "seq:%s:%s" % (prefix, day)
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        n = int(row["value"]) + 1 if row else 1
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(n)))
        return "%s-%s-%0*d" % (prefix, day, width, n)

    # ---- 작업 ----
    def create_task(self, request_key, **f):
        """같은 request_key 가 있으면 새로 만들지 않고 (기존 행, False) 를 돌려준다."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
            if row:
                return dict(row), False
            task_id = f.pop("task_id", None) or self._next_id(db, "T")
            ts = now()
            db.execute(
                "INSERT INTO tasks(task_id,request_key,project_id,title,request,done_criteria,verify_cmd,"
                "worker_agent,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, request_key, f["project_id"], f["title"], f["request"], f["done_criteria"],
                 f.get("verify_cmd"), f.get("worker_agent"), "created", ts, ts))
            return dict(db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()), True

    def task(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def tasks(self, project_id=None, open_only=False):
        q = "SELECT * FROM tasks"
        cond, args = [], []
        if project_id:
            cond.append("project_id=?")
            args.append(project_id)
        if open_only:
            cond.append("status NOT IN (%s)" % ",".join("?" * len(TERMINAL_TASK)))
            args += list(TERMINAL_TASK)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at, task_id", args)]

    def update_task(self, task_id, **f):
        if not f:
            return
        f["updated_at"] = now()
        cols = ",".join("%s=?" % k for k in f)
        with self.tx() as db:
            db.execute("UPDATE tasks SET %s WHERE task_id=?" % cols, list(f.values()) + [task_id])

    def add_worker(self, task_id, session_id):
        with self.tx() as db:
            row = db.execute("SELECT worker_sessions FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return False
            ws = json.loads(row["worker_sessions"] or "[]")
            if session_id in ws:
                return False
            ws.append(session_id)
            db.execute("UPDATE tasks SET worker_sessions=?, updated_at=? WHERE task_id=?",
                       (json.dumps(ws), now(), task_id))
            return True

    # ---- 이벤트(중복 제거의 기준) ----
    def record_event(self, event_key, source, kind, task_id=None, project_id=None, summary=None, payload=None):
        """처음 본 이벤트면 True. 같은 event_key 를 다시 받으면 False 이고 아무것도 바꾸지 않는다."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO events(event_key,task_id,project_id,source,kind,summary,payload,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (event_key, task_id, project_id, source, kind, summary,
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None, now()))
            return cur.rowcount == 1

    def events(self, task_id=None, since=None):
        q, args = "SELECT * FROM events", []
        cond = []
        if task_id:
            cond.append("task_id=?")
            args.append(task_id)
        if since:
            cond.append("created_at>=?")
            args.append(since)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at, event_key", args)]

    # ---- 질문 ----
    def add_question(self, event_key, task_id, project_id, text, asked_by=None, worker_session=None, options=None):
        with self.tx() as db:
            row = db.execute("SELECT * FROM questions WHERE event_key=?", (event_key,)).fetchone()
            if row:
                return dict(row), False
            qid = self._next_id(db, "Q")
            db.execute(
                "INSERT INTO questions(question_id,event_key,task_id,project_id,asked_by,worker_session,text,options,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (qid, event_key, task_id, project_id, asked_by, worker_session, text,
                 json.dumps(options, ensure_ascii=False) if options else None, "open", now()))
            return dict(db.execute("SELECT * FROM questions WHERE question_id=?", (qid,)).fetchone()), True

    def question(self, qid):
        row = self.db.execute("SELECT * FROM questions WHERE question_id=?", (qid,)).fetchone()
        return dict(row) if row else None

    def questions(self, status=None, task_id=None):
        q, cond, args = "SELECT * FROM questions", [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if task_id:
            cond.append("task_id=?")
            args.append(task_id)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at, question_id", args)]

    def update_question(self, qid, **f):
        cols = ",".join("%s=?" % k for k in f)
        with self.tx() as db:
            db.execute("UPDATE questions SET %s WHERE question_id=?" % cols, list(f.values()) + [qid])

    # ---- Main 에 보낼 알림 ----
    def add_notice(self, event_key, kind, text, task_id=None):
        with self.tx() as db:
            row = db.execute("SELECT * FROM notices WHERE event_key=?", (event_key,)).fetchone()
            if row:
                return dict(row), False
            nid = self._next_id(db, "N", 3)
            db.execute("INSERT INTO notices(notice_id,event_key,task_id,kind,text,created_at) VALUES(?,?,?,?,?,?)",
                       (nid, event_key, task_id, kind, text, now()))
            return dict(db.execute("SELECT * FROM notices WHERE notice_id=?", (nid,)).fetchone()), True

    def notices(self, unacked=True):
        q = "SELECT * FROM notices" + (" WHERE acked_at IS NULL" if unacked else "")
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at, notice_id")]

    def claim_notice_send(self, notice_id, session_id, renotify_seconds=0):
        """이 Main 세션에 아직 안 보낸 알림이면 보낸 것으로 표시하고 True. 다른 감시자가 먼저 잡았으면 False."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM notices WHERE notice_id=?", (notice_id,)).fetchone()
            if not row or row["acked_at"]:
                return False
            if row["sent_session"] == session_id:
                if not renotify_seconds or not row["sent_at"]:
                    return False
                age = (_dt.datetime.now(KST) - _dt.datetime.fromisoformat(row["sent_at"])).total_seconds()
                if age < renotify_seconds:
                    return False
            db.execute("UPDATE notices SET sent_session=?, sent_at=?, attempts=attempts+1, last_error=NULL"
                       " WHERE notice_id=?", (session_id, now(), notice_id))
            return True

    def notice_send_failed(self, notice_id, error):
        with self.tx() as db:
            db.execute("UPDATE notices SET sent_session=NULL, sent_at=NULL, last_error=? WHERE notice_id=?",
                       (str(error)[:500], notice_id))

    def ack_notice(self, notice_id):
        with self.tx() as db:
            cur = db.execute("UPDATE notices SET acked_at=? WHERE notice_id=? AND acked_at IS NULL", (now(), notice_id))
            return cur.rowcount == 1

    # ---- AO 로 보낸 메시지 ----
    def delivery(self, delivery_id):
        row = self.db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
        return dict(row) if row else None

    def put_delivery(self, delivery_id, **f):
        with self.tx() as db:
            row = db.execute("SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            ts = now()
            if row:
                f["updated_at"] = ts
                cols = ",".join("%s=?" % k for k in f)
                db.execute("UPDATE deliveries SET %s WHERE delivery_id=?" % cols, list(f.values()) + [delivery_id])
            else:
                f.setdefault("status", "pending")
                f["created_at"] = f["updated_at"] = ts
                cols = ",".join(["delivery_id"] + list(f))
                db.execute("INSERT INTO deliveries(%s) VALUES(%s)" % (cols, ",".join("?" * (len(f) + 1))),
                           [delivery_id] + list(f.values()))
        return self.delivery(delivery_id)

    def deliveries(self, status=None):
        q = "SELECT * FROM deliveries" + (" WHERE status=?" if status else "")
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at", (status,) if status else ())]

    # ---- Supervisor ----
    def supervisor(self, project_id):
        row = self.db.execute("SELECT * FROM supervisors WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def claim_supervisor_spawn(self, project_id, stale_seconds=180):
        """띄우기 직전에 잡는다. 다른 프로세스가 띄우는 중이면 False(중복 생성 방지)."""
        with self.tx() as db:
            row = db.execute("SELECT * FROM supervisors WHERE project_id=?", (project_id,)).fetchone()
            if row and row["status"] == "spawning":
                age = (_dt.datetime.now(KST) - _dt.datetime.fromisoformat(row["updated_at"])).total_seconds()
                if age < stale_seconds:
                    return False
            db.execute("INSERT OR REPLACE INTO supervisors(project_id,session_id,status,updated_at) VALUES(?,?,?,?)",
                       (project_id, row["session_id"] if row else None, "spawning", now()))
            return True

    def set_supervisor(self, project_id, session_id, status="active"):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO supervisors(project_id,session_id,status,updated_at) VALUES(?,?,?,?)",
                       (project_id, session_id, status, now()))

    # ---- Main 세션 ----
    def attach_main(self, session_id, pid=None, watcher_pid=None):
        with self.tx() as db:
            ts = now()
            db.execute("UPDATE mains SET active=0 WHERE session_id<>?", (session_id,))
            row = db.execute("SELECT * FROM mains WHERE session_id=?", (session_id,)).fetchone()
            if row:
                db.execute("UPDATE mains SET pid=COALESCE(?,pid), watcher_pid=COALESCE(?,watcher_pid), last_seen=?,"
                           " active=1 WHERE session_id=?", (pid, watcher_pid, ts, session_id))
            else:
                db.execute("INSERT INTO mains(session_id,pid,watcher_pid,attached_at,last_seen,active) VALUES(?,?,?,?,?,1)",
                           (session_id, pid, watcher_pid, ts, ts))

    def active_main(self):
        row = self.db.execute("SELECT * FROM mains WHERE active=1 ORDER BY last_seen DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def touch_main(self, session_id):
        with self.tx() as db:
            db.execute("UPDATE mains SET last_seen=? WHERE session_id=?", (now(), session_id))
