"""로깅 서브 Agent: 이벤트를 구독해 세션 단위로 그대로 저장. LLM 호출 없음 (F3013)."""
import json
from backend import db
from backend.events import bus

def _save(msg: dict):
    with db.conn() as c:
        c.execute("INSERT INTO logs(session_id, ts, event_json) VALUES(?,?,?)",
                  (msg["session_id"], msg["ts"], json.dumps(msg, ensure_ascii=False, default=str)))

def attach():
    bus.subscribe(_save)
