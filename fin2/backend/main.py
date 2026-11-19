import json
import os
from datetime import date, timedelta
from calendar import monthrange

from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import db, logger_agent
from backend.agent import make_graph, MEMORY
from backend.events import bus
from backend.llm import llm_plan, llm_answer
from backend.tools import catalog
import uuid

db.init()
logger_agent.attach()            # 이 한 줄이 로깅 Agent 연결의 전부. 빼도 대화는 그대로 동작한다
app = FastAPI(title="Finance Planner Agent")
NOT_FOUND = "그런 계획이 없습니다."

class ChatIn(BaseModel):
    message: str
    session_id: str | None = None

class ChatOut(BaseModel):
    session_id: str
    answer: str
    plan_draft: dict | None = None

class PlanIn(BaseModel):
    title: str
    kind: str
    amount: int | None = None
    months: int | None = None
    start_date: date
    summary: str = ""

class PlanOut(PlanIn):
    plan_id: int
    created_at: str
    overlaps: list[int] = []          # 같은 종류·기간이 겹치는 기존 계획 번호 (저장은 막지 않는다)

class LogSession(BaseModel):
    session_id: str
    n: int
    started: str

class LogEvent(BaseModel):
    event: str
    session_id: str
    ts: str
    payload: dict

def add_months(d: date, m: int) -> date:
    y, mo = divmod(d.month - 1 + m, 12)
    y, mo = d.year + y, mo + 1
    return date(y, mo, min(d.day, monthrange(y, mo)[1]))

def to_out(r, overlaps=()):
    d = dict(r)
    d["start_date"] = date.fromisoformat(d["start_date"])
    return PlanOut(**d, overlaps=list(overlaps))

def find_overlaps(p: PlanIn, exclude=None):
    if not p.months:
        return []
    end = add_months(p.start_date, p.months)
    out = []
    with db.conn() as c:
        for r in c.execute("SELECT * FROM plans WHERE kind=? AND months IS NOT NULL", (p.kind,)):
            if r["plan_id"] == exclude:
                continue
            s = date.fromisoformat(r["start_date"])
            if s < end and p.start_date < add_months(s, r["months"]):
                out.append(r["plan_id"])
    return out

@app.post("/api/chat", response_model=ChatOut)
def chat(req: ChatIn):
    sid = req.session_id or uuid.uuid4().hex[:8]
    emit = lambda event, **p: bus.publish(event, sid, **p)
    emit("session.start", user=req.message)
    try:
        out = make_graph(llm_plan, llm_answer, emit).invoke({"user": req.message, "sid": sid})
        answer, draft = out["answer"], out.get("plan_draft")
        if draft:
            draft["start_date"] = date.today().isoformat()
    except Exception:                  # 오류 상세는 화면에 노출하지 않는다 (NF3002)
        emit("error", code="AGENT_ERROR")
        answer, draft = "처리 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요.", None
    emit("session.end", answer=answer[:40])
    return ChatOut(session_id=sid, answer=answer, plan_draft=draft)

@app.get("/api/tools")
def tools():
    return catalog()

@app.post("/api/plans", response_model=PlanOut, status_code=201)
def create_plan(p: PlanIn):
    ov = find_overlaps(p)
    with db.conn() as c:
        cur = c.execute("INSERT INTO plans(title,kind,amount,months,start_date,summary) VALUES(?,?,?,?,?,?)",
                        (p.title, p.kind, p.amount, p.months, p.start_date.isoformat(), p.summary))
        r = c.execute("SELECT * FROM plans WHERE plan_id=?", (cur.lastrowid,)).fetchone()
    return to_out(r, ov)

@app.get("/api/plans", response_model=list[PlanOut])
def list_plans():
    with db.conn() as c:
        return [to_out(r) for r in c.execute("SELECT * FROM plans ORDER BY plan_id DESC")]

@app.get("/api/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int):
    with db.conn() as c:
        r = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not r:
        raise HTTPException(404, NOT_FOUND)
    return to_out(r)

@app.put("/api/plans/{plan_id}", response_model=PlanOut)
def update_plan(plan_id: int, p: PlanIn):
    get_plan(plan_id)
    with db.conn() as c:
        c.execute("UPDATE plans SET title=?,kind=?,amount=?,months=?,start_date=?,summary=? WHERE plan_id=?",
                  (p.title, p.kind, p.amount, p.months, p.start_date.isoformat(), p.summary, plan_id))
        r = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    return to_out(r, find_overlaps(p, exclude=plan_id))

@app.delete("/api/plans/{plan_id}", status_code=204)
def delete_plan(plan_id: int):
    get_plan(plan_id)
    with db.conn() as c:
        c.execute("DELETE FROM plans WHERE plan_id=?", (plan_id,))

# 실행 로그: 목록 · 상세 · 삭제만. 수정 API 는 두지 않는다 (F3014)
@app.get("/api/logs", response_model=list[LogSession])
def list_logs():
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT session_id, COUNT(*) AS n, MIN(ts) AS started FROM logs GROUP BY session_id ORDER BY MIN(id) DESC")]

@app.get("/api/logs/{session_id}", response_model=list[LogEvent])
def get_log(session_id: str):
    with db.conn() as c:
        rows = [json.loads(r["event_json"]) for r in c.execute(
            "SELECT event_json FROM logs WHERE session_id=? ORDER BY id", (session_id,))]
    if not rows:
        raise HTTPException(404, "그런 실행 로그가 없습니다.")
    return rows

@app.delete("/api/logs/{session_id}", status_code=204)
def delete_log(session_id: str):
    get_log(session_id)
    with db.conn() as c:
        c.execute("DELETE FROM logs WHERE session_id=?", (session_id,))
    MEMORY.pop(session_id, None)

app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "frontend"), html=True), name="ui")
