import threading
import time

from .config import get_settings
from .services import process_due_tasks
from .utils import now_iso

_worker_started = False
_worker_thread = None
_state_lock = threading.Lock()
_worker_state = {
    "started_at": None,
    "last_heartbeat_at": None,
    "last_success_at": None,
    "last_error": None,
}


def _set_state(**values):
    with _state_lock:
        _worker_state.update(values)


def worker_status():
    with _state_lock:
        state = dict(_worker_state)
    state["thread_alive"] = bool(_worker_thread and _worker_thread.is_alive())
    return state


def worker_loop():
    settings = get_settings()
    stamp = now_iso()
    _set_state(started_at=stamp, last_heartbeat_at=stamp, last_error=None)
    print("[{}] Background worker started. interval={} max_tick={}".format(
        stamp, settings.worker_interval_seconds, settings.max_send_per_tick
    ))
    while True:
        _set_state(last_heartbeat_at=now_iso())
        try:
            count = process_due_tasks(settings.max_send_per_tick)
            stamp = now_iso()
            _set_state(last_heartbeat_at=stamp, last_success_at=stamp, last_error=None)
            if count:
                print("[{}] Processed due tasks: {}".format(stamp, count))
        except Exception as exc:
            stamp = now_iso()
            _set_state(last_heartbeat_at=stamp, last_error=str(exc)[:1000])
            print("[{}] Worker error: {}".format(stamp, exc))
        time.sleep(settings.worker_interval_seconds)


def start_worker_once():
    global _worker_started, _worker_thread
    if _worker_started and _worker_thread and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(target=worker_loop, daemon=True, name="sendgrid-scheduler-worker")
    _worker_thread.start()
    _worker_started = True
