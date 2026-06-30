"""
Runs both bots as separate subprocesses in a single Railway worker.

  bot.py         — primary bot (runs forever)
  assistant_b.py — secondary bot (exits automatically after reaching its post limit)

Railway requires a single entry point per service. This script starts both
and keeps the container alive as long as the primary bot is running.
"""
import subprocess, sys, os, time
from pathlib import Path

HERE = Path(__file__).parent

def launch(script):
    return subprocess.Popen(
        [sys.executable, "-u", str(HERE / script)],
        env=os.environ.copy()
    )

primary   = launch("bot.py")
secondary = launch("assistant_b.py")

print(f"Both bots started. Primary pid={primary.pid} | Secondary pid={secondary.pid}")

# Wait for secondary bot to finish (self-terminates at post limit)
secondary.wait()
print("Secondary bot has exited. Primary bot continues running.")

# Keep the container alive while the primary bot is still running
primary.wait()
