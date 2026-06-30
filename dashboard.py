import os
import json
from pathlib import Path
from flask import Flask, render_template, jsonify
from flask_cors import CORS

BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
CORS(app)


def _read_json_file(path):
    """Read a JSON file, return None if missing or corrupt."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _is_pid_alive(pid):
    """Check if a process is alive via os.kill(pid, 0). Returns bool."""
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we don't own it
    except Exception:
        return False


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    """
    Returns JSON:
    {
      "botA": { ...fields..., "alive": bool },
      "botB": { ...fields..., "alive": bool }
    }

    Fields for botA (from bot_status.json):
      bot, course, status, pid, last_scan, scan_count, posts_answered,
      seen_count, poll_interval, last_post, alive

    Fields for botB (from assistant_b_status.json):
      bot, course, status, pid, last_scan, scan_count, posts_answered,
      post_limit, objective_complete, seen_count, poll_interval, last_post, alive

    If a file is missing or corrupt, return a default "not_started" object.
    """
    # Bot A
    bot_a_data = _read_json_file(BASE_DIR / "bot_status.json")
    if bot_a_data and isinstance(bot_a_data, dict):
        bot_a_data["alive"] = _is_pid_alive(bot_a_data.get("pid"))
    else:
        bot_a_data = {
            "bot": "A",
            "course": "COGS 9",
            "status": "not_started",
            "pid": None,
            "last_scan": None,
            "scan_count": 0,
            "posts_answered": 0,
            "seen_count": 0,
            "poll_interval": None,
            "last_post": None,
            "alive": False,
        }

    # Bot B
    bot_b_data = _read_json_file(BASE_DIR / "assistant_b_status.json")
    if bot_b_data and isinstance(bot_b_data, dict):
        bot_b_data["alive"] = _is_pid_alive(bot_b_data.get("pid"))
    else:
        bot_b_data = {
            "bot": "B",
            "course": "CHEM 11",
            "status": "not_started",
            "pid": None,
            "last_scan": None,
            "scan_count": 0,
            "posts_answered": 0,
            "post_limit": None,
            "objective_complete": False,
            "seen_count": 0,
            "poll_interval": None,
            "last_post": None,
            "alive": False,
        }

    return jsonify({"botA": bot_a_data, "botB": bot_b_data})


@app.route("/api/activity")
def api_activity():
    """
    Returns JSON array of the last 50 entries from activity_log.json,
    most recent first (reversed from file order).
    Returns [] if file is missing or corrupt.
    """
    entries = _read_json_file(BASE_DIR / "activity_log.json")
    if not isinstance(entries, list):
        entries = []
    # Return last 50, most recent first
    return jsonify(list(reversed(entries[-50:])))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
