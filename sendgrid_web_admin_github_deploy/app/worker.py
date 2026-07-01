import threading
import time
from .config import get_settings
from .services import process_due_tasks
from .utils import now_iso

_worker_started = False


def worker_loop():
    settings = get_settings()
    print("[{}] Background worker started. interval={} max_tick={}".format(
        now_iso(), settings.worker_interval_seconds, settings.max_send_per_tick
    ))
    while True:
        try:
            count = process_due_tasks(settings.max_send_per_tick)
            if count:
                print("[{}] Processed due tasks: {}".format(now_iso(), count))
        except Exception as exc:
            print("[{}] Worker error: {}".format(now_iso(), exc))
        time.sleep(settings.worker_interval_seconds)


def start_worker_once():
    global _worker_started
    if _worker_started:
        return
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    _worker_started = True
