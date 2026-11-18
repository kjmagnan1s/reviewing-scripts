#!/usr/bin/env python3
"""Script review: scripts live as markdown files in a folder, and a person reviews them in the browser.

  script_review.py up                          start the page server if it's down; print the local and phone addresses
  script_review.py new --title T [--body-file F] [--id ID] [--status S]
                                               add a script (body from the file or stdin); prints its id, file, and address.
                                               Saved as an idea unless --status says otherwise.
  script_review.py open [<id>]                 open a script (or the list) in the browser
  script_review.py wait <id> [--timeout 7200]  wait for Send on that script, then print the notes
  script_review.py inbox [--peek]              print every unread set of notes (--peek leaves them unread)
  script_review.py list [--status ready] [--json]   id, status, words, title
  script_review.py status <id> <status>        set a script's status
  script_review.py read <id> [--plain]         print what Copy read copies: the read as one paragraph, then
                                               hooks 2 and up ("Two. <line>"); --plain prints the read alone
  script_review.py config                      print the settings in use and where they come from
  script_review.py down                        stop the server
  script_review.py serve                       run the server in this terminal (up starts it for you)

The loop: save a script with `new`, then `up`, `open <id>`, and `wait <id>` in the background. The
reviewer comments on lines, edits lines, and clicks Send. Their edits are already saved in the file
when `wait` prints the notes. Apply the notes to the file with the smallest change; the open tab
reloads itself and marks the lines that changed. Run `wait <id>` again for the next round. "Looks
good" approves a draft and moves it to the approved status.

Settings: ~/.config/reviewing-scripts/config.json (SCRIPT_REVIEW_CONFIG overrides the path). See the README.
Phone:    with "tailscale": true in the config file, `up` asks Tailscale to forward
          https://<this computer>.ts.net:<port> to the server, so only your own devices reach it.
          The server itself listens on 127.0.0.1 only.
"""
import argparse, json, os, pathlib, re, signal, subprocess, sys, time, urllib.request, webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import script_page as SP

LOCAL = f"http://127.0.0.1:{SP.PORT}"


def desk():
    return SP.Desk()


def alive():
    """True when our server answers on the port and serves this scripts folder."""
    try:
        with urllib.request.urlopen(f"{LOCAL}/api/ping", timeout=1.5) as r:
            return json.load(r).get("scripts") == str(SP.SCRIPTS_DIR)
    except Exception:
        return False


def start_server():
    if os.environ.get("SCRIPT_REVIEW_NO_SERVER"):   # the tests: never start a real server
        return None
    SP.STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(SP.STATE_DIR / "server.log", "a")
    proc = subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()), "serve"], stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if alive():
            return proc.pid
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    sys.exit(f"The script server didn't start. See {SP.STATE_DIR / 'server.log'}")


def phone_url(setup=False):
    """(address, problem). The Tailscale address when the forward is in place; with setup, add it when missing."""
    exe, name = SP.tailscale_bin(), SP.tailscale_name()
    if not exe or not name:
        return None, "Tailscale isn't running on this computer, so a phone can't reach the page. The local address works without it."
    target, url = f"http://127.0.0.1:{SP.PORT}", f"https://{name}:{SP.PORT}/"
    try:
        status = subprocess.run([exe, "serve", "status", "--json"], capture_output=True, text=True, timeout=10).stdout
    except subprocess.TimeoutExpired:
        status = ""
    if target in status:
        return url, None
    if not setup:
        return None, "Tailscale isn't forwarding to the script server yet. Run: script_review.py up"
    try:
        r = subprocess.run([exe, "serve", "--bg", f"--https={SP.PORT}", target], capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired as e:   # it waits here when HTTPS isn't turned on for the tailnet yet
        text = lambda b: b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        return None, "Tailscale didn't finish setting up the forward:\n" + (text(e.stdout) + text(e.stderr)).strip()
    if r.returncode == 0:
        return url, None
    return None, "Tailscale didn't set up the forward:\n" + ((r.stdout + r.stderr).strip() or "no output")


def cmd_up(a):
    if alive():
        print(f"Script server is running: {LOCAL}/")
    else:
        pid = start_server()
        print(f"Script server started (pid {pid}): {LOCAL}/")
    if not SP.CFG["tailscale"]:
        return print('Phone: off. To open the page on your own devices through Tailscale, set "tailscale": true in the config file.')
    url, problem = phone_url(setup=True)
    print(f"Phone (any device on your Tailscale network): {url}" if url else problem)


def cmd_down(a):
    out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{SP.PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    if not out or not alive():
        return print("Script server isn't running.")
    for pid in out:
        os.kill(int(pid), signal.SIGTERM)
    print("Script server stopped.")


def cmd_serve(a):
    SP.SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    server = SP.make_server(desk())
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} serving {SP.SCRIPTS_DIR} on {LOCAL}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def need(sid):
    s = desk().find(sid)
    if not s:
        sys.exit(f"No script with id {sid!r} in {SP.SCRIPTS_DIR}. Run: script_review.py list")
    return s


def write_new(title, body, status=None, sid=None, created=None, extra=None, updated=None):
    """Create the script file and return its Script. Refuses to overwrite."""
    try:
        return desk().create(title, body, status, sid, created, updated, extra)
    except ValueError as e:
        sys.exit(f"Not created: {e}. Edit that script, or pass a different --id.")


def cmd_new(a):
    body = pathlib.Path(a.body_file).read_text(encoding="utf-8") if a.body_file else ("" if sys.stdin.isatty() else sys.stdin.read())
    lines = body.replace("\r\n", "\n").strip("\n").split("\n")
    m = re.match(r"^#\s+(.+)$", lines[0].strip()) if lines and lines[0].strip() else None
    title = a.title or (m.group(1).strip() if m else None)
    if not title:
        sys.exit("Give the script a title: --title \"...\"")
    if m:
        body = "\n".join(lines[1:])
    s = write_new(title, body, a.status or SP.IDEA, a.id)
    print(f"id: {s.id}\nfile: {s.path}\npage: {LOCAL}/s/{s.id}\n{s.words} words, about {SP.duration(s.words)}")
    if not alive():
        print("The server isn't running. Run: script_review.py up")


def cmd_open(a):
    if not alive():
        start_server()
    url = f"{LOCAL}/s/{need(a.id).id}" if a.id else f"{LOCAL}/"
    webbrowser.open(url)
    print(url)


def print_notes(d, items, keep=False):
    for sid, path in items:
        print(path.read_text(encoding="utf-8").rstrip() + "\n")
        if not keep:
            d.mark_read(sid, path)


def cmd_wait(a):
    s, d = need(a.id), desk()   # a.id may be an alias; s.id is the file's id
    if not alive():
        start_server()
    print(f"Waiting for Send on {LOCAL}/s/{s.id} ...", flush=True)
    end = time.time() + a.timeout
    while time.time() < end:
        items = d.unread(s.id)
        if items:
            return print_notes(d, items)
        time.sleep(1)
    print("No notes before the timeout. The page keeps unsent notes; run wait again.")
    sys.exit(2)


def cmd_inbox(a):
    d = desk()
    items = d.unread()
    if not items:
        return print("No notes waiting.")
    print_notes(d, items, keep=a.peek)


def cmd_list(a):
    rows = [s for s in desk().all() if not a.status or s.status == a.status]
    rows.sort(key=lambda s: (SP.LIST_ORDER.index(s.status), s.id))
    if a.json:
        return print(json.dumps([{"id": s.id, "title": s.title, "status": s.status, "words": s.words,
                                  "created": s.meta.get("created"), "updated": s.meta.get("updated"),
                                  **{f: s.meta.get(f) for f in SP.ALIAS_FIELDS}, "file": str(s.path)} for s in rows], indent=1))
    for s in rows:
        print(f"{s.id}\t{s.status}\t{s.words}w\t{s.title}")
    if not rows:
        print("No scripts.")


def cmd_status(a):
    s = desk().set_status(need(a.id).id, a.status)
    print(f"{s.id}: {s.status}")


def cmd_read(a):
    s = need(a.id)
    print(s.read if a.plain else s.teleprompter)


def cmd_config(a):
    path = pathlib.Path(os.environ.get("SCRIPT_REVIEW_CONFIG") or pathlib.Path.home() / ".config" / "reviewing-scripts" / "config.json").expanduser()
    print(f"Config file: {path}" + ("" if path.is_file() else " (not there; using the defaults)"))
    print(json.dumps(dict(SP.CFG, scripts_dir=str(SP.SCRIPTS_DIR), state_dir=str(SP.STATE_DIR), port=SP.PORT), indent=1, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("up").set_defaults(fn=cmd_up)
    sub.add_parser("down").set_defaults(fn=cmd_down)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    x = sub.add_parser("new"); x.set_defaults(fn=cmd_new)
    x.add_argument("--title"); x.add_argument("--body-file"); x.add_argument("--id"); x.add_argument("--status", choices=SP.STATUSES)
    x = sub.add_parser("open"); x.set_defaults(fn=cmd_open); x.add_argument("id", nargs="?")
    x = sub.add_parser("wait"); x.set_defaults(fn=cmd_wait); x.add_argument("id"); x.add_argument("--timeout", type=int, default=7200)
    x = sub.add_parser("inbox"); x.set_defaults(fn=cmd_inbox); x.add_argument("--peek", action="store_true")
    x = sub.add_parser("list"); x.set_defaults(fn=cmd_list); x.add_argument("--status", choices=SP.STATUSES); x.add_argument("--json", action="store_true")
    x = sub.add_parser("status"); x.set_defaults(fn=cmd_status); x.add_argument("id"); x.add_argument("status", choices=SP.STATUSES)
    x = sub.add_parser("read"); x.set_defaults(fn=cmd_read); x.add_argument("id"); x.add_argument("--plain", action="store_true")
    sub.add_parser("config").set_defaults(fn=cmd_config)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
