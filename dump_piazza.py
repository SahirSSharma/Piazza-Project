import os, re, sys, html, getpass
from piazza_api import Piazza

# READ-ONLY: dumps every post (questions + answers + followups) to prove visibility.
# Never writes to Piazza.

LIMIT = 200  # how many recent posts to walk

def strip_html(raw):
    return html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).strip()

def latest(node):
    """Return the most recent (subject, content) for a post or answer node."""
    hist = (node.get("history") or [{}])[0]
    subj = strip_html(hist.get("subject", "") or node.get("subject", ""))
    body = strip_html(hist.get("content", "") or node.get("subject", ""))
    return subj, body

ANSWER_LABEL = {"i_answer": "INSTRUCTOR ANSWER", "s_answer": "STUDENT ANSWER"}

def print_children(children, indent):
    for child in children or []:
        ctype = child.get("type")
        subj, body = latest(child)
        text = (subj or body or "").strip()
        if ctype in ANSWER_LABEL:
            print(f"{indent}{ANSWER_LABEL[ctype]}: {text}")
        elif ctype == "followup":
            print(f"{indent}FOLLOWUP: {text}")
        elif ctype == "feedback":
            print(f"{indent}REPLY: {text}")
        else:
            print(f"{indent}{(ctype or 'NOTE').upper()}: {text}")
        # recurse into nested followups/replies
        print_children(child.get("children"), indent + "    ")

def main():
    email = os.environ.get("PIAZZA_EMAIL") or input("Piazza email: ")
    password = os.environ.get("PIAZZA_PASSWORD") or getpass.getpass("Piazza password: ")
    network_id = (os.environ.get("PIAZZA_NETWORK") or input("Class network id: ")).strip()

    p = Piazza()
    try:
        p.user_login(email=email, password=password)
    except Exception as e:
        sys.exit(f"Login failed: {e}")
    network = p.network(network_id)
    print(f"Connected. Walking up to {LIMIT} posts...\n")

    n = 0
    for post in network.iter_all_posts(limit=LIMIT):
        n += 1
        nr = post.get("nr", "?")
        ptype = post.get("type", "?")
        subj, body = latest(post)
        print("=" * 70)
        print(f"@{nr}  ({ptype})")
        print(f"  Q SUBJECT: {subj}")
        if body and body != subj:
            print(f"  Q BODY:    {body}")
        print_children(post.get("children"), "  ")
    print("\n" + "=" * 70)
    print(f"Done. Walked {n} posts. (Read-only: nothing was posted.)")

if __name__ == "__main__":
    main()
