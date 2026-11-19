"""이벤트 발행과 구독. 에이전트는 emit 만 알고, 누가 듣는지 모른다 (F3012)."""
from datetime import datetime

class EventBus:
    def __init__(self):
        self._subs = []

    def subscribe(self, fn):
        self._subs.append(fn)

    def publish(self, event, session_id, **payload):
        msg = {"event": event, "session_id": session_id,
               "ts": datetime.now().isoformat(timespec="seconds"), "payload": payload}
        for fn in list(self._subs):
            try:
                fn(msg)
            except Exception:
                pass            # 구독자가 고장 나도 메인 흐름은 계속
        return msg

bus = EventBus()
