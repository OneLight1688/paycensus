#!/usr/bin/env python3
"""Watchdog: when attribute_github.py (Phase C) finishes, auto-run gh_home merge."""
import subprocess
import time

while subprocess.run("pgrep -f attribute_github.py > /dev/null", shell=True).returncode == 0:
    time.sleep(300)
r = subprocess.run(
    "cd /opt/paycensus && python3 attribute_gh_home.py > /var/lib/paycensus/gh_home.log 2>&1",
    shell=True, capture_output=True, text=True, timeout=7200)
print("chain done rc=", r.returncode)
