"""
scheduler.py - Runs the pipeline at configured times (5x/day default).
Uses APScheduler. Falls back to a manual loop if not installed.
"""
import sys
import time
import logging
import threading
from datetime import datetime

import config
import db

log = logging.getLogger("scheduler")


def _run_pipeline():
    """Wrapper to call main pipeline."""
    from main import NewsPipeline
    log.info(f"⏰ Scheduled run at {datetime.now().strftime('%H:%M')}")
    try:
        db.init_db()
        pipeline = NewsPipeline()
        pipeline.run(
            limit      = config.PIPELINE["articles_per_run"],
            live       = True,
            skip_noise = config.PIPELINE["skip_noise"],
        )
    except Exception as e:
        log.error(f"❌ Scheduled run failed: {e}", exc_info=True)


class Scheduler:
    """
    Schedules pipeline runs at times defined in config.SCHEDULE_TIMES.
    Uses APScheduler if available, falls back to a polling loop.
    """

    def __init__(self):
        self._thread    = None
        self._stop_evt  = threading.Event()
        self._use_aps   = self._try_aps()

    def _try_aps(self) -> bool:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            self._aps_cls = BackgroundScheduler
            return True
        except ImportError:
            log.info("APScheduler not found — using built-in polling loop")
            return False

    def start(self):
        if self._use_aps:
            self._start_aps()
        else:
            self._start_loop()

    def stop(self):
        self._stop_evt.set()
        if hasattr(self, "_sched"):
            self._sched.shutdown(wait=False)

    # ── APScheduler path ──────────────────────────────

    def _start_aps(self):
        from apscheduler.schedulers.background import BackgroundScheduler

        self._sched = BackgroundScheduler(timezone="local")
        for t in config.SCHEDULE_TIMES:
            h, m = map(int, t.split(":"))
            self._sched.add_job(
                _run_pipeline,
                trigger="cron",
                hour=h, minute=m,
                id=f"pipeline_{t}",
                replace_existing=True,
            )
        self._sched.start()
        log.info(f"✅ APScheduler started: {config.SCHEDULE_TIMES}")

    # ── Simple polling fallback ───────────────────────

    def _start_loop(self):
        self._thread = threading.Thread(
            target=self._loop, name="scheduler", daemon=True
        )
        self._thread.start()
        log.info(f"✅ Polling scheduler started: {config.SCHEDULE_TIMES}")

    def _loop(self):
        fired_today: set = set()

        while not self._stop_evt.is_set():
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")
            hhmm  = now.strftime("%H:%M")

            # Reset fired set at midnight
            if not any(k.startswith(today) for k in fired_today):
                fired_today.clear()

            key = f"{today}_{hhmm}"
            if hhmm in config.SCHEDULE_TIMES and key not in fired_today:
                fired_today.add(key)
                threading.Thread(target=_run_pipeline,
                                 name=f"run_{hhmm}",
                                 daemon=True).start()

            self._stop_evt.wait(30)  # check every 30s


# ── Standalone entry point ────────────────────────────

def run_service():
    """Start scheduler + optional Flask webhook (from main.py pattern)."""
    import signal

    config.print_status()
    db.init_db()

    sched = Scheduler()
    sched.start()

    print(f"⏰ Scheduler running. Times: {config.SCHEDULE_TIMES}")
    print(f"   Instance: {config.INSTANCE_NAME}")
    print("   Press Ctrl+C to stop\n")

    def _shutdown(sig, frame):
        log.info("Shutting down scheduler...")
        sched.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    run_service()
