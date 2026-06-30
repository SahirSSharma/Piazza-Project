import os, sys, getpass
from pathlib import Path

# Load .env
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from piazza_api import Piazza

DRAFTS_FILE = "drafts.txt"

def load_draft(post_nr):
    """Return the draft answer for a given post number, or None."""
    text = Path(DRAFTS_FILE).read_text(encoding="utf-8") if Path(DRAFTS_FILE).exists() else ""
    blocks = text.split("-" * 60)
    for block in blocks:
        header = f"@{post_nr}  [syllabus]"
        if header in block:
            answer = None
            for line in block.splitlines():
                if line.startswith("DRAFT ANSWER:"):
                    answer = line[len("DRAFT ANSWER:"):].strip()
            return answer
    return None

def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python publish.py <post_number>\nExample: python publish.py 7")

    post_nr = sys.argv[1].lstrip("@")
    draft = load_draft(post_nr)
    if not draft:
        sys.exit(f"No syllabus draft found for @{post_nr} in {DRAFTS_FILE}.")

    print(f"\nPost:   @{post_nr}")
    print(f"Answer: {draft}\n")
    confirm = input("Post this to Piazza? [y/N]: ").strip().lower()
    if confirm != "y":
        sys.exit("Cancelled.")

    email = os.environ.get("PIAZZA_EMAIL") or input("Piazza email: ")
    password = os.environ.get("PIAZZA_PASSWORD") or getpass.getpass("Piazza password: ")
    network_id = (os.environ.get("PIAZZA_NETWORK") or input("Network id: ")).strip()

    p = Piazza()
    p.user_login(email=email, password=password)
    network = p.network(network_id)

    # Fetch the post by its number
    post = None
    for candidate in network.iter_all_posts(limit=200):
        if str(candidate.get("nr")) == str(post_nr):
            post = candidate
            break

    if post is None:
        sys.exit(f"Could not find post @{post_nr} on Piazza.")

    result = network.create_followup(post, draft)
    print(f"\nPosted successfully. Followup id: {result.get('id', '?')}")

if __name__ == "__main__":
    main()
