"""
Runs both bots as separate subprocesses in a single Railway worker.

  bot.py      — COGS 9 bot (runs forever)
  chem_bot.py — CHEM 11 bot (exits automatically after 10 content posts)

Railway requires a single entry point per service. This script starts both
and keeps the container alive as long as the COGS 9 bot is running.
"""
import subprocess, sys, os, time
from pathlib import Path

HERE = Path(__file__).parent

def launch(script):
    return subprocess.Popen(
        [sys.executable, "-u", str(HERE / script)],
        env=os.environ.copy()
    )

cogs9 = launch("bot.py")
chem  = launch("chem_bot.py")

print(f"Both bots started. COGS 9 pid={cogs9.pid} | CHEM 11 pid={chem.pid}")

# Wait for chem bot to finish (self-terminates at 10 content posts)
chem.wait()
print("CHEM 11 bot has exited. COGS 9 bot continues running.")

# Keep the container alive while the COGS 9 bot is still running
cogs9.wait()
