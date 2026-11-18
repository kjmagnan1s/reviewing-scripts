#!/usr/bin/env python3
"""Offline tests for the script page and its command line, both in skills/reviewing-scripts/scripts/.

  python3 tests/test_script_page.py

Everything runs in a temp folder on a free port; your scripts folder and state folder are never touched.
"""
import json, os, pathlib, subprocess, sys, tempfile, threading, urllib.error, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
tmp = pathlib.Path(tempfile.mkdtemp())
scripts, state = tmp / "scripts", tmp / "state"
scripts.mkdir()
CONFIG = {"scripts_dir": str(scripts), "state_dir": str(state), "title": "Sam's <scripts>", "reviewer": "Sam",
          "hooks_section": "Instagram hooks", "sections": ["Caption"], "alias_fields": ["old_id"], "words_per_minute": 190, "tailscale": True}
(tmp / "config.json").write_text(json.dumps(CONFIG))
for k in ("SCRIPT_REVIEW_DIR", "SCRIPT_REVIEW_STATE", "SCRIPT_REVIEW_PORT"):
    os.environ.pop(k, None)
os.environ["SCRIPT_REVIEW_CONFIG"] = str(tmp / "config.json")   # before the import: script_page reads its settings once
SCRIPTS = HERE.parent / "skills" / "reviewing-scripts" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import script_page as SP

fails = 0


def check(name, got, want=True):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}, want {want!r}"))


BODY = """# Parallel helpers

Spoken Script

Your agent can spawn helpers. Most people never ask.
One. Say it out loud.
Two. Start with read-only jobs.

Instagram hooks
Hook 1 (claim) is line 1 of the read.
Hook 2 (list): Five moves your agent already knows.

**Caption**
Save this.
"""

desk = SP.Desk()
check("the config file sets the folders", [desk.scripts, desk.state], [scripts, state])

# --- file format ---
fm = SP.set_meta([], {"type": "script", "id": "a-b", "title": 'Claude: "2x" faster', "status": "drafted", "created": "2026-11-16"})
check("a title with a colon and quotes survives the frontmatter", SP.meta_of(fm)["title"], 'Claude: "2x" faster')
check("a plain value stays unquoted", fm[3], "status: drafted")
check("a number-like value is quoted", SP.fm_value("123"), '"123"')
check("set_meta replaces in place and keeps other lines", SP.set_meta(["a: 1", "# note", "status: idea"], {"status": "ready"}), ["a: 1", "# note", "status: ready"])
body = SP.normalize_body(BODY.split("\n", 1)[1])
check("bare and bold headers become ## headers", [l for l in body.split("\n") if l.startswith("##")], ["## Spoken Script", "## Instagram hooks", "## Caption"])
(scripts / "a-b.md").write_text(SP.join_file(fm, body))
s = desk.load("a-b")
check("the read is one paragraph", s.read, "Your agent can spawn helpers. Most people never ask. One. Say it out loud. Two. Start with read-only jobs.")
check("Copy read adds hooks 2 and up after the last line, and nothing else", s.teleprompter,
      s.read + " Two. Five moves your agent already knows.")
check("the copy confirmation counts the read and the hooks", s.copied, "Copied 19 words + 1 hook")
check("bulleted, bold, and numbered-3 hooks are found; hook 1 and the how-to line are not",
      SP.alternate_hooks(["## Instagram hooks", "Say the number, pause, then the line.", "Hook 1 (claim) is line 1 of the read.",
                          "- Hook 2 (list): Second.", "**Hook 3 (pain):** Third.", "Hook 4 (x) has no colon"]),
      [(2, "Second."), (3, "Third.")])
check("no read, no hooks in the copy", SP.teleprompter_text(["## Instagram hooks", "Hook 2 (x): Alone."]), "")
check("only Spoken Script lines are the read", [b["text"] for b in SP.layout(s.lines) if b["read"]][-1], "Two. Start with read-only jobs.")
check("duration at 190 words a minute", SP.duration(190), "1:00")
check("a bad id finds nothing", desk.load("../a-b"), None)
(scripts / "README.md").write_text("# not a script\n")
(scripts / "other.md").write_text("---\ntype: note\ntitle: Not a script\n---\n\nHi.\n")
check("only files with the configured type are listed", [x.id for x in desk.all()], ["a-b"])
check("the list page offers the new-idea form", "newform" in SP.list_page(desk, "tok", "n"))
check("the list page shows the configured title, escaped", "<h1>Sam&#x27;s &lt;scripts&gt;</h1>" in SP.list_page(desk, "tok", "n"))

# --- edits ---
lines = ["## Spoken Script", "One. Say it out loud.", "Two. Start small.", "Three."]
applied, missed = SP.apply_edits(lines, [
    {"line": 1, "before": "One. Say it out loud.", "after": "One. Say it.\nOut loud."},      # split in two
    {"line": 9, "before": "Two. Start small.", "after": "Two. Start tiny."},                 # wrong number, right text
    {"line": 3, "before": "Three.", "after": ""},                                             # removed
    {"line": 2, "before": "This line is gone.", "after": "x"}])                               # can't be placed
check("edits land by text, split, and remove", lines, ["## Spoken Script", "One. Say it.", "Out loud.", "Two. Start tiny."])
check("an edit whose line changed is reported, not applied", [len(applied), len(missed)], [3, 1])

# --- the server ---
server = SP.make_server(desk, 0, ts_name=lambda: "mac.example.ts.net")
port = server.server_port
threading.Thread(target=server.serve_forever, daemon=True).start()
token = (state / "token").read_text()


def call(path, data=None, host=None, headers=None):
    h = {"Host": host or f"127.0.0.1:{port}"}
    if data is not None:
        h.update({"Content-Type": "application/json", "X-Script-Token": token})
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if data is None else json.dumps(data).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, "", e.headers


code, page, headers = call("/s/a-b")
check("the script page loads", code, 200)
check("the page carries the script and no stray placeholder", ["Start with read-only jobs" in page, "__DATA__" in page], [True, False])
check("the page sets a nonce-only script policy", "script-src 'nonce-" in headers.get("Content-Security-Policy", ""))
check("the list page loads", call("/")[0], 200)
check("an unknown host is refused (DNS rebinding)", call("/", host="evil.example")[0], 403)
check("the Tailscale name on this port is accepted", call("/", host=f"mac.example.ts.net:{port}")[0], 200)
SP.CFG["tailscale"] = False
check("the Tailscale name is refused when the setting is off", call("/", host=f"mac.example.ts.net:{port}")[0], 403)
SP.CFG["tailscale"] = True
check("no file is served from disk", call("/README.md")[0], 404)
check("a post without the token is refused", call("/api/send", {"id": "a-b"}, headers={"X-Script-Token": "nope"})[0], 403)
check("a post from another origin is refused", call("/api/send", {"id": "a-b"}, headers={"Origin": "https://evil.example"})[0], 403)
check("a malformed post is a 400", call("/api/send", {"id": "a-b", "comments": "x"})[0], 400)

old = s.raw
code, out, _ = call("/api/send", {"id": "a-b", "overall": "Tighter overall.", "edits": [
    {"line": 3, "before": "One. Say it out loud.", "after": "One. Say it out loud, every time."}], "comments": [
    {"line": 2, "line_text": "Your agent can spawn helpers. Most people never ask.", "quote": "Most people never ask", "note": "Punchier."},
    {"line": 3, "line_text": "One. Say it out loud.", "quote": "", "note": "Keep this beat."}]},
    headers={"Origin": f"http://127.0.0.1:{port}"})
out = json.loads(out)
check("send applies the edit", [code, out["applied"], out["missed"], out["approved"]], [200, 1, 0, False])
s2 = desk.load("a-b")
check("the edit is in the file", "One. Say it out loud, every time." in s2.lines)
check("the rest of the file is untouched", s2.raw.replace("One. Say it out loud, every time.", "One. Say it out loud.").replace("updated: " + s2.meta["updated"] + "\n", ""), old)
notes = desk.unread("a-b")
text = notes[0][1].read_text()
check("one notes file is waiting", len(notes), 1)
check("the notes name the reviewer", "## Sam's edits (already saved to the file)" in text)
check("the notes quote the edit, the selected words, and the overall note", ["became “One. Say it out loud, every time.”" in text, "On: “Most people never ask”" in text,
      "Note: Punchier." in text, "This line was edited in the same send" in text, "Tighter overall." in text], [True] * 5)
check("the old version is kept", any(f.read_text() == old for f in (state / "versions" / "a-b").iterdir()))
check("the edit is logged", json.loads((state / "edits.jsonl").read_text().splitlines()[0])["after"], "One. Say it out loud, every time.")
desk.mark_read(*notes[0])
check("read notes leave the inbox and stay on disk", [len(desk.unread("a-b")), len(list((state / "notes" / "a-b" / "read").iterdir()))], [0, 1])

out = json.loads(call("/api/send", {"id": "a-b"})[1])
check("an empty send approves and marks a drafted script ready", [out["approved"], out["status"], out["status_label"]], [True, "ready", "Ready to film"])
code, out, _ = call("/api/new", {"title": "Phone idea: test", "body": "Try the notch teleprompter."})
made = json.loads(out)
check("the list page can add an idea", [code, made["status"], desk.load(made["id"]).title, desk.load(made["id"]).meta.get("written_by")], [200, "idea", "Phone idea: test", "sam"])
check("a long hand-typed script is drafted and all of it is the read", json.loads(call("/api/new", {"title": "Typed", "body": "word " * 60})[1])["status"], "drafted")
check("the same title twice in a day is refused, not overwritten", call("/api/new", {"title": "Typed", "body": "x"})[0], 400)
check("a script is found by an id in an alias field", [desk.create("Old card", "## Spoken Script\nHi.", extra={"old_id": "86abc"}).id == desk.find("86abc").id, desk.find("nope")], [True, None])
check("an alias in a link lands on the script's page", call("/s/86abc")[0], 200)
check("status can be set from the page", json.loads(call("/api/status", {"id": "a-b", "status": "filmed"})[1])["status"], "filmed")
check("a made-up status is refused", call("/api/status", {"id": "a-b", "status": "done"})[0], 400)
check("a send for a missing script is a 404", call("/api/send", {"id": "nope"})[0], 404)

# A script whose text looks like HTML or a template placeholder can't break the page.
(scripts / "x-y.md").write_text(SP.join_file(SP.set_meta([], {"type": "script", "id": "x-y", "title": "</script><b>hi", "status": "idea"}),
                                             "## Spoken Script\n</script><script>alert(1)</script> __JS__ __CSS__"))
page = call("/s/x-y")[1]
check("script text can't close the script tag", page.count("</script>"), 1)
check("script text is never read as a placeholder", "__JS__ __CSS__" in page)
server.shutdown()

# --- the command line ---
cli = [sys.executable, str(SCRIPTS / "script_review.py")]
env = dict(os.environ)
(tmp / "body.md").write_text(BODY)
r = subprocess.run(cli + ["new", "--body-file", str(tmp / "body.md"), "--id", "new-one"], capture_output=True, text=True, env=env)
check("new takes the title from the first line and saves an idea", ["id: new-one" in r.stdout, desk.load("new-one").title, desk.load("new-one").status], [True, "Parallel helpers", "idea"])
check("new refuses to overwrite", subprocess.run(cli + ["new", "--title", "x", "--id", "new-one"], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL).returncode != 0)
check("read prints what Copy read copies", subprocess.run(cli + ["read", "new-one"], capture_output=True, text=True, env=env).stdout.strip(), desk.load("new-one").teleprompter)
check("read --plain prints the read alone", subprocess.run(cli + ["read", "new-one", "--plain"], capture_output=True, text=True, env=env).stdout.strip(), desk.load("new-one").read)
check("status sets the frontmatter", subprocess.run(cli + ["status", "new-one", "ready"], capture_output=True, text=True, env=env).stdout.strip(), "new-one: ready")
desk.send(SP.clean_payload({"id": "new-one", "comments": [{"line": 2, "line_text": "One. Say it out loud.", "note": "Louder."}]}))
r = subprocess.run(cli + ["wait", "new-one", "--timeout", "3"], capture_output=True, text=True, env=dict(env, SCRIPT_REVIEW_NO_SERVER="1"))
check("wait prints the notes and marks them read", ["Note: Louder." in r.stdout, len(desk.unread("new-one"))], [True, 0])
r = subprocess.run(cli + ["new", "--title", "Straight to draft", "--status", "drafted"], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
check("new --status saves at that status", desk.load(r.stdout.split()[1]).status, "drafted")
r = subprocess.run(cli + ["list", "--json"], capture_output=True, text=True, env=env)
check("list --json carries the alias fields", {x["old_id"] for x in json.loads(r.stdout)}, {None, "86abc"})
r = subprocess.run(cli + ["config"], capture_output=True, text=True, env=env)
check("config prints the settings in use", ['"reviewer": "Sam"' in r.stdout, str(tmp / "config.json") in r.stdout], [True, True])
(tmp / "bad.json").write_text(json.dumps({"scripts_dirr": "x"}))
r = subprocess.run(cli + ["list"], capture_output=True, text=True, env=dict(env, SCRIPT_REVIEW_CONFIG=str(tmp / "bad.json")))
check("a misspelled setting is named, not ignored", [r.returncode != 0, "unknown setting scripts_dirr" in r.stderr], [True, True])
(tmp / "bad.json").write_text(json.dumps({"approved_status": "shipped"}))
r = subprocess.run(cli + ["list"], capture_output=True, text=True, env=dict(env, SCRIPT_REVIEW_CONFIG=str(tmp / "bad.json")))
check("a status role that isn't a status is refused", [r.returncode != 0, "approved_status" in r.stderr], [True, True])
(tmp / "custom.json").write_text(json.dumps({"scripts_dir": str(tmp / "c"), "state_dir": str(tmp / "cs"), "statuses": {"draft": "Drafts", "final": "Final"},
                                             "idea_status": "draft", "draft_status": "draft", "approved_status": "final"}))
cenv = dict(env, SCRIPT_REVIEW_CONFIG=str(tmp / "custom.json"), SCRIPT_REVIEW_NO_SERVER="1")
r = subprocess.run(cli + ["new", "--title", "Custom"], capture_output=True, text=True, env=cenv, stdin=subprocess.DEVNULL)
cid = r.stdout.split()[1]
r = subprocess.run(cli + ["status", cid, "final"], capture_output=True, text=True, env=cenv)
check("custom statuses work end to end", r.stdout.strip(), f"{cid}: final")
r = subprocess.run(cli + ["list"], capture_output=True, text=True, env=dict(cenv, SCRIPT_REVIEW_DIR=str(scripts)))
check("SCRIPT_REVIEW_DIR overrides the config file", "a-b" in r.stdout)
check("a hand-typed script with no headers is all read", SP.read_text(["First line.", "", "Second line."]), "First line. Second line.")

print("\nall passed" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
