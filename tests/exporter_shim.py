"""Run steam_backlog_export.py as __main__ with time.sleep replaced, for tests.

    python tests/exporter_shim.py path/to/steam_backlog_export.py [exporter args...]

Why: the exporter backs off with real sleeps (5, 10, 20 and 40 seconds when a reply isn't valid JSON,
and whatever Retry-After says on a 429), and rate-limits itself with more. Tests shouldn't wait for
those, but should see that they were requested. This records every sleep as [seconds, function that
asked] and writes the list as JSON to $SHIM_SLEEP_LOG when the script ends. The exporter itself runs
unmodified, through runpy, with its real argv and environment.
"""
import json
import os
import runpy
import sys
import time

sleeps = []


def fake_sleep(seconds):
    sleeps.append([float(seconds), sys._getframe(1).f_code.co_name])


time.sleep = fake_sleep

script = sys.argv[1]
sys.argv = [script] + sys.argv[2:]
try:
    runpy.run_path(script, run_name="__main__")
finally:
    path = os.environ.get("SHIM_SLEEP_LOG")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sleeps, f)
