"""Script page: scripts as markdown files in a folder, reviewed in the browser.

Each script is one markdown file with frontmatter (type, id, title, status). The page shows it one
line at a time. The reviewer comments on a line or on the words they selected, edits a line directly,
and clicks Send. Send saves the edits to the file first, then writes the notes as markdown for the
agent. The agent edits the file, and the open tab reloads itself. "Copy read" puts the read on the
clipboard as one paragraph, for a teleprompter.

The server stays up and serves every script, so a phone bookmark works at any time.

Security: notes go straight to an agent and edits go straight to a file, so only these pages may
send them. The server listens on 127.0.0.1 only. It answers for Host 127.0.0.1/localhost on its own
port, plus this computer's Tailscale name when `tailscale serve` forwards to it (blocks DNS rebinding).
A POST needs the token the page was served with in an X-Script-Token header (a cross-site page
can't set it without a CORS preflight, which the server never grants), an allowed Origin, and a
JSON body under 512 KB. It serves no files from disk: only the list, a script page, and the API.
"""
import hashlib, html, json, os, re, secrets, shutil, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOME = Path.home()
DEFAULTS = {
    "scripts_dir": "~/Documents/scripts",                 # one markdown file per script
    "state_dir": "~/.local/state/reviewing-scripts",      # notes, every version of every script, the server log
    "port": 8792,
    "title": "Scripts",                                   # the list page's heading
    "reviewer": "The reviewer",                           # the person's name, as the notes say it
    "file_type": "script",                                # frontmatter `type:` of the files this tool lists
    "read_section": "Spoken Script",                      # the section that is read aloud
    "hooks_section": "Alternate hooks",                   # "Hook 2 (kind): <line>" lines, added after the read on copy
    "sections": [],                                       # other headers to recognize when written bare or in bold
    "words_per_minute": 150,
    "statuses": {"idea": "Ideas", "drafted": "Drafted", "ready": "Ready to film", "filmed": "Filmed", "posted": "Posted"},
    "list_order": ["ready", "drafted", "idea", "filmed", "posted"],
    "collapsed": ["filmed", "posted"],                    # groups the list page shows folded
    "idea_status": "idea", "draft_status": "drafted", "approved_status": "ready",
    "alias_fields": [],
    "tailscale": False,                                   # true: `up` asks Tailscale to serve the page to your own devices                                   # frontmatter fields that hold another id for the same script
}


def load_config():
    """DEFAULTS, then the JSON config file, then SCRIPT_REVIEW_DIR / _STATE / _PORT from the environment."""
    path = Path(os.environ.get("SCRIPT_REVIEW_CONFIG") or HOME / ".config" / "reviewing-scripts" / "config.json").expanduser()
    cfg = dict(DEFAULTS)
    if path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SystemExit(f"{path} isn't valid JSON: {e}")
        unknown = sorted(set(user) - set(DEFAULTS)) if isinstance(user, dict) else ["(the file must hold one JSON object)"]
        if unknown:
            raise SystemExit(f"{path}: unknown setting {', '.join(unknown)}. Known settings: {', '.join(DEFAULTS)}")
        cfg.update(user)
    for key, env in (("scripts_dir", "SCRIPT_REVIEW_DIR"), ("state_dir", "SCRIPT_REVIEW_STATE"), ("port", "SCRIPT_REVIEW_PORT")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    statuses = cfg["statuses"]
    if not (isinstance(statuses, dict) and statuses and all(re.match(r"^[a-z][a-z0-9-]*$", k) for k in statuses)):
        raise SystemExit(f"{path}: statuses must map lowercase ids to labels, for example {{\"drafted\": \"Drafted\"}}")
    for key in ("idea_status", "draft_status", "approved_status"):
        if cfg[key] not in statuses:
            raise SystemExit(f"{path}: {key} is {cfg[key]!r}, which isn't one of the statuses ({', '.join(statuses)})")
    order = [st for st in cfg["list_order"] if st in statuses]
    cfg["list_order"] = order + [st for st in statuses if st not in order]
    return cfg


CFG = load_config()
SCRIPTS_DIR = Path(str(CFG["scripts_dir"])).expanduser()
STATE_DIR = Path(str(CFG["state_dir"])).expanduser()
PORT = int(CFG["port"])
STATUS_LABEL = dict(CFG["statuses"])
STATUSES = list(STATUS_LABEL)
LIST_ORDER = CFG["list_order"]
IDEA, DRAFT, APPROVED = CFG["idea_status"], CFG["draft_status"], CFG["approved_status"]
WORDS_PER_MIN = float(CFG["words_per_minute"])
REVIEWER, FILE_TYPE, ALIAS_FIELDS = str(CFG["reviewer"]), str(CFG["file_type"]), list(CFG["alias_fields"])
MAX_BODY, MAX_ITEMS = 512 * 1024, 300
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,90}$")


def norm(line):
    return re.sub(r"[#*\\\s]", "", line).lower()


# Section headers recognized without a leading '#', keyed by their normalized form.
HEADERS = {norm(h): h for h in [CFG["read_section"], CFG["hooks_section"], *CFG["sections"]]}
READ_KEY, HOOKS_KEY = norm(CFG["read_section"]), norm(CFG["hooks_section"])


# ---------- the file format ----------

FM = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./-]*$")


def split_file(text):
    """(frontmatter lines, body) of a script file. No frontmatter: ([], the whole text)."""
    text = text.replace("\r\n", "\n")
    m = FM.match(text)
    return (m.group(1).split("\n"), text[m.end():]) if m else ([], text)


def meta_of(fm_lines):
    meta = {}
    for line in fm_lines:
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            continue
        v = m.group(2).strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.strip('"')
        elif len(v) >= 2 and v[0] == v[-1] == "'":
            v = v[1:-1].replace("''", "'")
        meta[m.group(1)] = v
    return meta


def fm_value(v):
    """A YAML scalar. Anything YAML could read as a number, a boolean, or syntax gets double quotes."""
    v = str(v)
    plain = PLAIN.match(v) and v.lower() not in ("true", "false", "null", "yes", "no", "on", "off") \
        and not re.match(r"^[\d.]+$", v) and not v.endswith(" ")
    return v if plain else json.dumps(v, ensure_ascii=False)


def set_meta(fm_lines, updates):
    """fm_lines with each key in updates replaced in place, or added at the end. Other lines are kept."""
    out, left = [], dict(updates)
    for line in fm_lines:
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        if m and m.group(1) in left:
            out.append(f"{m.group(1)}: {fm_value(left.pop(m.group(1)))}")
        else:
            out.append(line)
    return out + [f"{k}: {fm_value(v)}" for k, v in left.items()]


def join_file(fm_lines, body):
    return "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body.strip("\n") + "\n"


def heading(line):
    """The heading text when a line is a section header ("## Caption", "**Caption**", or a known bare one)."""
    s = line.strip()
    if s.startswith("#"):
        return s.lstrip("#").strip() or None
    return HEADERS.get(norm(s))


def layout(lines):
    """One dict per body line: {i, text, kind: h|line|blank, section, read}. read marks the read section.
    A body with no read section is a script typed by hand, so all of it is the read."""
    out, section = [], ""
    bare = not has_read_section(lines)
    for i, text in enumerate(lines):
        h = heading(text)
        if h:
            section = h
            out.append({"i": i, "text": h, "kind": "h", "section": section, "read": False})
        elif not text.strip():
            out.append({"i": i, "text": "", "kind": "blank", "section": section, "read": False})
        else:
            out.append({"i": i, "text": text, "kind": "line", "section": section,
                        "read": norm(section) == READ_KEY or (bare and norm(section) not in HEADERS)})
    return out


def has_read_section(lines):
    return any(norm(heading(line) or "") == READ_KEY for line in lines)


def read_text(lines):
    """The read as one paragraph: what the teleprompter gets."""
    return re.sub(r"\s+", " ", " ".join(b["text"] for b in layout(lines) if b["read"])).strip()


HOOK = re.compile(r"^Hook\s+(\d+)\s*\([^)]*\)\s*:\s*(.+)$", re.I)
NUMBER = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}


def alternate_hooks(lines):
    """[(number, line)] for hooks 2 and up in the hooks section ("Hook 2 (claim): <line>").
    Hook 1 is line 1 of the read, so it's never listed."""
    out = []
    for b in layout(lines):
        if b["kind"] == "line" and norm(b["section"]) == HOOKS_KEY:
            m = HOOK.match(re.sub(r"^\s*[-*]\s+", "", b["text"]).replace("**", "").strip())
            if m and int(m.group(1)) > 1:
                out.append((int(m.group(1)), m.group(2).strip()))
    return out


def teleprompter_text(lines):
    """What Copy read copies: the read, then each alternate hook as it is filmed ("Two. <line>").
    Nothing before the first line, and none of the other sections."""
    read = read_text(lines)
    hooks = [f"{NUMBER.get(n, n)}. {text}" for n, text in alternate_hooks(lines)] if read else []
    return re.sub(r"\s+", " ", " ".join([read] + hooks)).strip()


def normalize_body(body):
    """A body with bare or bold header lines, with every known header as '## Header'."""
    out = []
    for line in body.replace("\r\n", "\n").strip("\n").split("\n"):
        s = line.strip()
        if not s.startswith("#") and norm(s) in HEADERS:
            if out and out[-1].strip():
                out.append("")
            out.append("## " + HEADERS[norm(s)])
        else:
            out.append(line.rstrip())
    return "\n".join(out)


def slug(text, limit=60):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "script"


class Script:
    def __init__(self, path):
        self.path = Path(path)
        self.id = self.path.stem
        self.raw = self.path.read_text(encoding="utf-8")
        self.fm, body = split_file(self.raw)
        self.body = body.strip("\n")
        self.meta = meta_of(self.fm)
        self.lines = self.body.split("\n")
        self.version = hashlib.sha256(self.raw.encode()).hexdigest()[:16]
        self.title = self.meta.get("title") or self.id
        self.status = self.meta.get("status") if self.meta.get("status") in STATUSES else DRAFT

    @property
    def read(self):
        return read_text(self.lines)

    @property
    def words(self):
        return len(self.read.split())

    @property
    def teleprompter(self):
        return teleprompter_text(self.lines)

    @property
    def copied(self):
        """The confirmation Copy read shows."""
        n = len(alternate_hooks(self.lines))
        return f"Copied {self.words} words" + (f" + {n} hook{'s' if n > 1 else ''}" if n else "")


def short_date(iso):
    """'2026-11-16' as 'Nov 16' (with the year when it isn't this year)."""
    try:
        t = time.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return iso or ""
    return f"{time.strftime('%b', t)} {t.tm_mday}" + ("" if t.tm_year == time.localtime().tm_year else f", {t.tm_year}")


def duration(words):
    s = round(words * 60 / WORDS_PER_MIN)
    return f"{s // 60}:{s % 60:02d}"


# ---------- applying what the page sends ----------

def apply_edits(lines, edits):
    """Apply the reviewer's line edits to lines in place. An edit names a line by number and by its old text;
    the text wins, so an edit still lands after the agent moved lines around. Returns (applied, missed)."""
    applied, missed, plan, used = [], [], [], set()
    for e in edits:
        i, before = e["line"], e["before"]
        if not (0 <= i < len(lines) and lines[i] == before and i not in used):
            hits = [j for j, line in enumerate(lines) if line == before and j not in used]
            i = hits[0] if len(hits) == 1 else -1
        if i < 0:
            missed.append(e)
            continue
        used.add(i)
        indent = before[:len(before) - len(before.lstrip())]
        new = [indent + part.strip() for part in e["after"].split("\n") if part.strip()]
        plan.append((i, new))
        applied.append(dict(e, line=i, after="\n".join(new)))
    for i, new in sorted(plan, reverse=True):
        lines[i:i + 1] = new
    return applied, missed


def clean_payload(data):
    """The page's JSON with types and sizes checked. Raises on anything malformed."""
    comments, edits = data.get("comments", []), data.get("edits", [])
    assert isinstance(comments, list) and isinstance(edits, list) and len(comments) + len(edits) <= MAX_ITEMS
    return {
        "id": str(data["id"]),
        "overall": str(data.get("overall", ""))[:5000].strip(),
        "comments": [{"line": int(c.get("line", -1)), "line_text": str(c.get("line_text", ""))[:4000],
                      "quote": str(c.get("quote", ""))[:500], "note": str(c["note"])[:5000].strip()}
                     for c in comments if str(c.get("note", "")).strip()],
        "edits": [{"line": int(e.get("line", -1)), "before": str(e["before"])[:4000],
                   "after": str(e["after"])[:4000].replace("\r", "")} for e in edits],
    }


def file_line(raw, text):
    """1-based line number of text in the file when it appears exactly once, else None."""
    lines = raw.split("\n")
    return lines.index(text) + 1 if text and lines.count(text) == 1 else None


def notes_markdown(script, data, applied, missed, approved, status_changed):
    """The notes the agent reads. script is the file as it stands after the reviewer's edits were saved."""
    sections = {b["text"]: b["section"] for b in layout(script.lines) if b["kind"] == "line"}
    new_text = {e["before"]: e["after"] for e in applied}
    out = [f"# Script notes: {script.title}", "", f"Script: {script.id}", f"File: {script.path}",
           f"Sent: {time.strftime('%Y-%m-%d %I:%M %p').replace(' 0', ' ')}", ""]
    if approved:
        out += ["Looks good. No notes." + (f" Status changed to {APPROVED}." if status_changed else ""), ""]
    if applied:
        out += [f"## {REVIEWER}'s edits (already saved to the file)", "",
                "These are the reviewer's own words. Keep them exactly unless a note asks for a change.", ""]
        for e in applied:
            if e["after"]:
                where = file_line(script.raw, e["after"].split("\n")[0])
                new = " and ".join(f"“{part}”" for part in e["after"].strip().split("\n"))
                split = " (split in two)" if "\n" in e["after"].strip() else ""
                out += [f"- {'File line ' + str(where) if where else 'Line'}: “{e['before'].strip()}” became {new}{split}"]
            else:
                out += [f"- Removed: “{e['before'].strip()}”"]
        out.append("")
    if missed:
        out += ["## Edits that didn't land", "",
                "The line changed before these were sent, so the file wasn't touched. Apply them by hand.", ""]
        for e in missed:
            out += [f"- “{e['before'].strip()}” should become “{e['after'].strip()}”" if e["after"].strip()
                    else f"- Remove: “{e['before'].strip()}”"]
        out.append("")
    if data["comments"]:
        out += ["## Comments", ""]
        for c in data["comments"]:
            text = new_text.get(c["line_text"], c["line_text"])
            where = file_line(script.raw, text.split("\n")[0])
            section = sections.get(text.split("\n")[0], "")
            head = " · ".join(x for x in (section, f"file line {where}" if where else "") if x) or "Line"
            out += [f"### {head}", f"Line: “{text.strip()}”"]
            if c["quote"] and c["quote"] != text.strip():
                out.append(f"On: “{c['quote']}”")
            if c["line_text"] in new_text:
                out.append("(This line was edited in the same send. The line above is the new wording.)")
            out += [f"Note: {c['note']}", ""]
    if data["overall"]:
        out += ["## Overall", data["overall"], ""]
    return "\n".join(out).strip() + "\n"


class Desk:
    """The scripts folder and the state folder: every read and write goes through here."""

    def __init__(self, scripts=None, state=None):
        self.scripts, self.state = Path(scripts or SCRIPTS_DIR), Path(state or STATE_DIR)
        self.lock = threading.Lock()
        self._snapped = {}

    def path(self, sid):
        if not ID_RE.match(sid or ""):
            return None
        p = self.scripts / f"{sid}.md"
        return p if p.is_file() else None

    def load(self, sid):
        p = self.path(sid)
        if not p:
            return None
        s = Script(p)
        self.snapshot(s)
        return s

    def find(self, any_id):
        """A script by its id (the file name), or by another id for it held in one of the
        alias_fields in its frontmatter (for example the id it had in the tool it was imported from)."""
        s = self.load(any_id)
        if s or not any_id:
            return s
        for x in self.all():
            if any_id in [x.meta.get(f) for f in ALIAS_FIELDS]:
                self.snapshot(x)
                return x
        return None

    def create(self, title, body="", status=None, sid=None, created=None, updated=None, extra=None):
        """Write a new script file and return it. Raises ValueError rather than overwrite anything."""
        title = re.sub(r"\s+", " ", title or "").strip()
        if not title:
            raise ValueError("a script needs a title")
        status = status or DRAFT
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        created = created or time.strftime("%Y-%m-%d")
        sid = sid or f"{created}-{slug(title)}"
        if not ID_RE.match(sid):
            raise ValueError(f"bad id {sid!r}: lowercase letters, digits, and hyphens only")
        self.scripts.mkdir(parents=True, exist_ok=True)
        path = self.scripts / f"{sid}.md"
        with self.lock:
            if path.exists():
                raise ValueError(f"{path} already exists")
            meta = {"type": FILE_TYPE, "id": sid, "title": title, "status": status, "created": created,
                    "updated": updated or time.strftime("%Y-%m-%d")}
            meta.update(extra or {})
            path.write_text(join_file(set_meta([], meta), normalize_body(body)), encoding="utf-8")
        return self.load(sid)

    def all(self):
        out = []
        for p in sorted(self.scripts.glob("*.md")):
            if not ID_RE.match(p.stem):
                continue
            try:
                s = Script(p)
            except (OSError, UnicodeDecodeError):
                continue
            if s.meta.get("type") == FILE_TYPE:
                out.append(s)
        return out

    def snapshot(self, s):
        """Keep a copy of every version of a script the server sees, so nothing is ever lost to an
        overwrite. Copies live in the state folder, never the scripts folder."""
        if self._snapped.get(s.id) == s.version:
            return
        d = self.state / "versions" / s.id
        d.mkdir(parents=True, exist_ok=True)
        if not any(f.name.endswith(f"-{s.version}.md") for f in d.iterdir()):
            (d / f"{time.strftime('%Y%m%d-%H%M%S')}-{s.version}.md").write_text(s.raw, encoding="utf-8")
        self._snapped[s.id] = s.version

    def write(self, s, fm, body):
        """Write a script file in one step (a temp file, then a rename), and return the reloaded script."""
        self.snapshot(s)
        tmp = s.path.with_name(f".{s.path.name}.tmp")
        tmp.write_text(join_file(fm, body), encoding="utf-8")
        os.replace(tmp, s.path)
        new = Script(s.path)
        self.snapshot(new)
        return new

    def inbox(self, sid):
        return self.state / "notes" / sid / "inbox"

    def unread(self, sid=None):
        """Unread notes files, oldest first: [(script id, path)]."""
        root = self.state / "notes"
        ids = [sid] if sid else sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
        out = []
        for i in ids:
            box = self.inbox(i)
            if box.is_dir():
                out += [(i, f) for f in sorted(box.glob("notes-*.md"))]
        return sorted(out, key=lambda x: x[1].name)

    def mark_read(self, sid, path):
        done = self.state / "notes" / sid / "read"
        done.mkdir(parents=True, exist_ok=True)
        os.replace(path, done / path.name)

    def set_status(self, sid, status):
        assert status in STATUSES
        with self.lock:
            s = self.load(sid)
            if not s:
                return None
            if s.status == status:
                return s
            return self.write(s, set_meta(s.fm, {"status": status, "updated": time.strftime("%Y-%m-%d")}), s.body)

    def send(self, data):
        """Save the reviewer's edits to the file, then write the notes for the agent. Returns the page's reply."""
        with self.lock:
            s = self.load(data["id"])
            if not s:
                return None
            lines = list(s.lines)
            applied, missed = apply_edits(lines, data["edits"])
            approved = not (data["comments"] or data["edits"] or data["overall"])
            status_changed = approved and s.status == DRAFT and APPROVED != DRAFT
            updates = {}
            if status_changed:
                updates["status"] = APPROVED
            if applied or updates:
                updates["updated"] = time.strftime("%Y-%m-%d")
                s = self.write(s, set_meta(s.fm, updates), "\n".join(lines))
            if applied:
                log = self.state / "edits.jsonl"
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as f:
                    for e in applied:
                        f.write(json.dumps({"id": s.id, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                            "before": e["before"], "after": e["after"]}, ensure_ascii=False) + "\n")
            box = self.inbox(s.id)
            box.mkdir(parents=True, exist_ok=True)
            name = box / f"notes-{time.strftime('%Y%m%d-%H%M%S')}.md"
            n = 1
            while name.exists():   # two sends in one second
                n += 1
                name = box / f"notes-{time.strftime('%Y%m%d-%H%M%S')}-{n}.md"
            name.write_text(notes_markdown(s, data, applied, missed, approved, status_changed), encoding="utf-8")
            return {"ok": True, "version": s.version, "approved": approved, "status": s.status,
                    "status_label": STATUS_LABEL[s.status], "status_changed": status_changed, "applied": len(applied), "missed": len(missed)}


# ---------- Tailscale ----------

def tailscale_bin():
    return shutil.which("tailscale") or next((p for p in ("/usr/local/bin/tailscale",
                                             "/Applications/Tailscale.app/Contents/MacOS/Tailscale") if os.path.exists(p)), None)


def tailscale_name():
    """This computer's Tailscale DNS name (no trailing dot), or None when Tailscale isn't running."""
    exe = tailscale_bin()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=8).stdout
        return (json.loads(out)["Self"]["DNSName"] or "").rstrip(".") or None
    except Exception:
        return None


# ---------- the pages ----------

CSS = """
:root {
  color-scheme: light dark;
  --canvas: light-dark(#f5f4f0, #0f1016); --rail: light-dark(#fbfaf8, #14151d); --card: light-dark(#ffffff, #1a1c26);
  --card-border: light-dark(#e2e0da, #272a38); --hair: light-dark(#dedcd5, #232633); --strong-border: light-dark(#c9c6bd, #3a3e52);
  --txt: light-dark(#23242b, #dfe2ee); --strong-txt: light-dark(#101116, #f6f7fc); --mute: light-dark(#5c6070, #979db3);
  --soft: light-dark(#eceae4, #1d1f2a); --input-bg: light-dark(#fdfdfb, #11121a); --focus: light-dark(#23242b, #dfe2ee);
  --accent: light-dark(#cf3d14, #ff6a3d); --on-accent: light-dark(#ffffff, #1a0d07);
  --go: light-dark(#157347, #4ade9b); --pin: light-dark(#8a6100, #f5b93a); --new: light-dark(#2757c4, #7aa8ff);
  --edit: var(--go); --danger: light-dark(#a83527, #ff8f7d); --mark: light-dark(#ffe08a, #6b5210);
  --btn-bg: var(--accent); --btn-fg: var(--on-accent); --role: var(--mute); --target: 30px;
  --lift: 0 10px 28px -18px rgb(0 0 0 / .6);
  scrollbar-color: var(--strong-border) transparent; accent-color: var(--accent);
}
[data-role="approved"] { --role: var(--go); } [data-role="draft"] { --role: var(--pin); } [data-role="idea"] { --role: var(--new); }
@media (pointer: coarse) { :root { --target: 44px; } }
* { box-sizing: border-box; }
::selection { background: color-mix(in oklab, var(--accent) 38%, transparent); color: var(--strong-txt); }
html { scroll-padding-top: 76px; -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--canvas); color: var(--txt); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, sans-serif; overflow-wrap: break-word; -webkit-font-smoothing: antialiased; }
button, textarea, select { font: inherit; color: inherit; }
button, select { min-block-size: var(--target); cursor: pointer; }
textarea, input { caret-color: var(--accent); }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
a { color: inherit; }
.top { position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; padding: 12px max(20px, env(safe-area-inset-right)) 12px max(20px, env(safe-area-inset-left)); background: var(--canvas); border-bottom: 1px solid var(--hair); }
.top h1 { flex: 1 1 240px; min-inline-size: 0; margin: 0; font-size: 19px; line-height: 1.25; font-weight: 700; letter-spacing: -0.015em; color: var(--strong-txt); text-wrap: balance; }
.top.home { position: static; padding-block: 34px 6px; border-bottom: 0; align-items: baseline; }
.top.home h1 { display: flex; align-items: center; gap: 12px; font-size: 34px; line-height: 1.1; font-weight: 750; letter-spacing: -0.03em; }
.top.home h1::before { content: ""; flex: none; inline-size: 14px; block-size: 14px; margin-inline-start: 5px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 5px color-mix(in oklab, var(--accent) 22%, transparent); }
.top.home, .list { max-inline-size: 760px; margin-inline: auto; }
.back { display: inline-flex; align-items: center; gap: 7px; min-block-size: var(--target); padding: 0 4px; color: var(--mute); text-decoration: none; font-size: 13.5px; font-weight: 600; }
.back::before { content: ""; inline-size: 7px; block-size: 7px; border-inline-start: 2px solid; border-block-end: 2px solid; rotate: 45deg; }
.back:hover { color: var(--strong-txt); }
.meta { color: var(--mute); font-size: 13px; white-space: nowrap; font-variant-numeric: tabular-nums; }
select { padding: 3px 10px; border: 1px solid color-mix(in oklab, var(--role) 55%, transparent); border-radius: 999px; background: color-mix(in oklab, var(--role) 14%, var(--card)); color: var(--role); font-size: 13px; font-weight: 700; }
select:hover { border-color: var(--role); }
select option { color: var(--txt); background: var(--card); }
.wrap { display: grid; grid-template-columns: minmax(0, 1fr) 360px; }
.doc { min-inline-size: 0; inline-size: 100%; max-inline-size: 800px; margin: 0 auto; padding: 20px 24px 120px; }
.h { margin: 44px 0 10px; padding: 16px 12px 0 26px; border-top: 1px solid var(--hair); font-size: 12px; font-weight: 700; letter-spacing: 0.09em; text-transform: uppercase; color: var(--mute); }
.h:first-child { margin-top: 4px; padding-top: 0; border-top: 0; }
.h.readh { color: var(--accent); }
.line { position: relative; margin: 2px 0; padding: 7px 12px 7px 26px; border-radius: 9px; cursor: pointer; }
.line:hover { background: var(--soft); }
.read:hover { background: color-mix(in oklab, var(--accent) 9%, transparent); }
.line.active, .read.active { background: var(--card); box-shadow: 0 0 0 1.5px var(--accent), var(--lift); cursor: default; }
.line.noted:not(.active) { background: color-mix(in oklab, var(--pin) 8%, transparent); }
.line.edited:not(.active) { background: color-mix(in oklab, var(--edit) 8%, transparent); }
.t { margin: 0; font-size: 14px; color: var(--mute); text-wrap: pretty; white-space: pre-wrap; }
.line:hover .t, .line.active .t { color: var(--txt); }
.read .t, .read:hover .t, .read.active .t { font-size: 22px; line-height: 1.42; font-weight: 500; letter-spacing: -0.012em; color: var(--strong-txt); }
.read { padding-block: 9px; }
/* A dot in the gutter marks a line with a note (amber) or an edit (green). */
.line.noted .t::before, .line.edited .t::before { content: ""; position: absolute; inline-size: 8px; block-size: 8px; margin-inline-start: -17px; margin-block-start: calc(0.5lh - 4px); border-radius: 50%; background: var(--pin); }
.line.edited .t::before { background: var(--edit); }
.t.gone { text-decoration: line-through; text-decoration-thickness: 1.5px; color: var(--mute); }
mark { background: var(--mark); color: inherit; border-radius: 4px; padding: 1px 2px; margin-inline: -2px; text-decoration: underline; text-decoration-color: var(--pin); text-decoration-thickness: 2px; text-underline-offset: 4px; }
.tags { display: flex; gap: 8px; margin-top: 4px; }
.tags:empty { display: none; }
.tag { font-size: 11.5px; font-weight: 700; letter-spacing: 0.02em; }
.tag.edit { color: var(--edit); } .tag.new { color: var(--new); } .tag.note { color: var(--pin); }
.acts, .foot { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 10px; }
.btn { padding: 5px 14px; border: 1px solid var(--strong-border); border-radius: 9px; background: var(--card); font-size: 13.5px; font-weight: 600; }
.btn:hover { border-color: var(--accent); box-shadow: var(--lift); }
.btn:active { scale: 0.97; }
.btn:disabled { opacity: .55; cursor: default; box-shadow: none; scale: none; }
.btn.primary { border-color: var(--btn-bg); background: var(--btn-bg); color: var(--btn-fg); font-weight: 700; }
.btn.primary:hover { border-color: var(--btn-bg); }
.btn.quiet { border-color: transparent; background: none; color: var(--mute); }
.btn.quiet:hover { color: var(--strong-txt); box-shadow: none; }
.push { margin-inline-start: auto; }
textarea { display: block; inline-size: 100%; padding: 10px 12px; border: 1px solid var(--strong-border); border-radius: 9px; background: var(--input-bg); font-size: 16px; line-height: 1.5; resize: vertical; field-sizing: content; min-block-size: 3lh; }
textarea::placeholder, input::placeholder { color: var(--mute); opacity: 1; }
textarea:focus { outline: 2px solid var(--accent); outline-offset: 0; border-color: transparent; }
.box { margin-top: 10px; }
.q { margin: 0 0 8px; padding: 6px 10px; border-radius: 7px; background: color-mix(in oklab, var(--pin) 14%, transparent); color: var(--txt); font-size: 13px; }
.hint { color: var(--mute); font-size: 12px; }
.rail { position: sticky; top: var(--top, 56px); align-self: start; display: flex; flex-direction: column; block-size: calc(100dvh - var(--top, 56px)); border-inline-start: 1px solid var(--hair); background: var(--rail); }
.rail-scroll { flex: 1; min-block-size: 0; overflow-y: auto; padding: 20px 14px; }
.rail-head { display: flex; align-items: baseline; gap: 8px; padding: 0 4px 12px; }
.section { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.01em; color: var(--strong-txt); }
.cards { display: grid; gap: 8px; margin-bottom: 14px; }
.empty { margin: 0; padding: 4px; color: var(--mute); font-size: 13.5px; }
.card { --c: var(--pin); padding: 11px 13px; border: 1px solid color-mix(in oklab, var(--c) 30%, var(--card-border)); border-radius: 11px; background: color-mix(in oklab, var(--c) 9%, var(--card)); cursor: pointer; }
.card.e { --c: var(--edit); }
.card:hover { border-color: var(--c); box-shadow: var(--lift); }
.card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; color: var(--c); font-size: 12px; font-weight: 650; }
.card-head::before { content: ""; flex: none; inline-size: 8px; block-size: 8px; border-radius: 50%; background: var(--c); }
.card-head .btn { color: var(--mute); }
.card p { margin: 0; font-size: 14px; white-space: pre-wrap; color: var(--strong-txt); }
.card .was { color: var(--mute); text-decoration: line-through; }
.card .ctx { color: var(--mute); font-size: 12.5px; margin-bottom: 5px; }
.actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 14px; border-top: 1px solid var(--hair); background: var(--rail); }
.actions .btn { inline-size: 100%; padding: 11px 12px; border-radius: 11px; font-size: 15px; }
.copy { position: relative; display: block; }
.tip { position: absolute; inset-block-end: calc(100% + 8px); inset-inline-start: 50%; translate: -50% 4px; padding: 4px 10px; border-radius: 7px; background: var(--strong-txt); color: var(--canvas); font-size: 12.5px; font-weight: 650; font-variant-numeric: tabular-nums; white-space: nowrap; opacity: 0; pointer-events: none; }
[data-copied] + .tip, [data-failed] + .tip { opacity: 1; translate: -50% 0; }
#count:not(:empty) { padding: 0 8px; border-radius: 999px; background: var(--accent); color: var(--on-accent); font-size: 12px; font-weight: 700; }
#status { grid-column: 1 / -1; min-block-size: 1lh; color: var(--mute); font-size: 12.5px; text-align: center; }
#selpop { position: fixed; inset: auto; margin: 0; padding: 0; border: 0; background: none; overflow: visible; }
#selpop { display: none; }
#selpop.open { display: block; }
#selpop:popover-open { display: block; }
#selpop .btn { box-shadow: 0 8px 20px -8px rgb(0 0 0 / .55); }
@media (prefers-reduced-motion: no-preference) {
  .line, .item, .card { transition: background-color .14s ease-out, box-shadow .14s ease-out, border-color .14s ease-out; }
  .btn, select { transition: border-color .14s ease-out, box-shadow .14s ease-out, scale .14s ease-out; }
  .tip { transition: opacity .15s ease-out, translate .2s cubic-bezier(.16, 1, .3, 1); }
  .group > summary::after { transition: rotate .2s cubic-bezier(.16, 1, .3, 1); }
}
@media (max-width: 860px) {
  html { scroll-padding-bottom: calc(110px + env(safe-area-inset-bottom, 0px)); }
  .top { padding-block: 10px; }
  .top h1 { font-size: 17px; }
  .top.home { padding-block: 22px 2px; }
  .top.home h1 { font-size: 30px; }
  .wrap { display: block; }
  .doc { max-inline-size: none; padding: 10px 8px 28px; }
  .h { margin-top: 32px; }
  .read .t, .read:hover .t, .read.active .t { font-size: 19.5px; }
  .rail { position: static; block-size: auto; border-inline-start: 0; border-top: 1px solid var(--hair); padding-bottom: calc(124px + env(safe-area-inset-bottom, 0px)); }
  .rail-scroll { overflow: visible; }
  .actions { position: fixed; inset-inline: 0; inset-block-end: 0; z-index: 6; padding: 10px max(12px, env(safe-area-inset-right)) max(10px, env(safe-area-inset-bottom)) max(12px, env(safe-area-inset-left)); box-shadow: 0 -10px 26px -18px rgb(0 0 0 / .6); }
}
/* the list page */
.new { margin: 14px 0 0; }
.new > summary { display: inline-flex; align-items: center; list-style: none; border-color: color-mix(in oklab, var(--accent) 60%, transparent); color: var(--accent); }
.new > summary::-webkit-details-marker { display: none; }
.new form { display: grid; gap: 6px; margin-top: 12px; padding: 16px; border: 1px solid var(--card-border); border-radius: 13px; background: var(--card); }
.new label { color: var(--mute); font-size: 12.5px; font-weight: 650; }
.new label + * { margin-bottom: 6px; }
.new input { inline-size: 100%; min-block-size: var(--target); padding: 9px 12px; border: 1px solid var(--strong-border); border-radius: 9px; background: var(--input-bg); font: inherit; font-size: 16px; color: inherit; }
.new input:focus { outline: 2px solid var(--accent); outline-offset: 0; border-color: transparent; }
.new textarea { min-block-size: 6lh; }
.list { padding: 4px 20px 64px; }
.group > summary, .group-head { display: flex; align-items: baseline; gap: 9px; margin: 34px 0 10px; padding: 0; font-size: 20px; line-height: 1.25; font-weight: 700; letter-spacing: -0.015em; color: var(--strong-txt); }
.group > summary { cursor: pointer; min-block-size: var(--target); align-items: center; list-style: none; color: var(--mute); }
.group-head::before, .group > summary::before { content: ""; flex: none; align-self: center; inline-size: 10px; block-size: 10px; border-radius: 50%; background: var(--role); }
.group > summary::-webkit-details-marker { display: none; }
.group > summary:hover, .group[open] > summary { color: var(--strong-txt); }
.group > summary::after { content: ""; flex: none; inline-size: 8px; block-size: 8px; margin-inline-start: auto; margin-inline-end: 6px; border-inline-end: 2px solid; border-block-end: 2px solid; rotate: -45deg; }
.group[open] > summary::after { rotate: 45deg; }
.n { align-self: center; padding: 1px 9px; border-radius: 999px; background: color-mix(in oklab, var(--role) 18%, transparent); color: var(--role); font-size: 13px; font-weight: 700; letter-spacing: 0; font-variant-numeric: tabular-nums; }
.item { display: flex; align-items: center; gap: 12px; margin: 8px 0; padding: 6px 10px 6px 18px; border: 1px solid var(--card-border); border-radius: 13px; background: var(--card); }
.item:hover { border-color: var(--role); box-shadow: var(--lift); }
[data-role="approved"] .item .btn { border-color: var(--accent); background: var(--accent); color: var(--on-accent); font-weight: 700; }
.item a { flex: 1; min-inline-size: 0; padding: 10px 0; text-decoration: none; }
.item .title { display: block; color: var(--strong-txt); font-size: 17px; line-height: 1.3; font-weight: 650; letter-spacing: -0.01em; text-wrap: pretty; }
.item .sub { display: block; margin-top: 2px; color: var(--mute); font-size: 13px; font-variant-numeric: tabular-nums; }
.item .sub span { white-space: nowrap; }
.item .sub span + span:not(.waiting)::before { content: "·"; margin-inline: .45em; }
.item .waiting { margin-inline-start: .6em; padding: 1px 7px; border-radius: 999px; background: var(--mark); color: var(--strong-txt); font-weight: 650; }
.item .copy { flex: none; }
.item .tip { inset-inline-start: auto; inset-inline-end: 0; translate: 0 4px; }
.item [data-copied] + .tip, .item [data-failed] + .tip { translate: none; }
@media (max-width: 860px) {
  .list { padding-inline: 16px; }
  .top.home { padding-inline: max(16px, env(safe-area-inset-left)) max(16px, env(safe-area-inset-right)); }
  .group > summary, .group-head { margin-top: 28px; font-size: 18px; }
  .item { gap: 8px; padding: 4px 8px 4px 14px; }
  .item .sub { font-size: 12.5px; }
  .item .upd:not(:first-child) { display: none; }
  .item .btn { padding-inline: 11px; }
}
"""

SHARED_JS = r"""
const $ = (id) => document.getElementById(id);
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch {} };
const words = (t) => (t.trim() ? t.trim().split(/\s+/).length : 0);

// Clipboard. navigator.clipboard needs https or localhost; the phone reaches this page over
// Tailscale, which is plain http until HTTPS is turned on for the tailnet, so keep the old path too.
function legacyCopy(text) {
  const field = Object.assign(document.createElement("textarea"), { value: text });
  field.setAttribute("readonly", "");
  field.style.cssText = "position:fixed;inset-block-start:0;opacity:0";
  document.body.append(field);
  field.select();
  try { return document.execCommand("copy"); } finally { field.remove(); }
}
async function copyText(text) {
  if (!navigator.clipboard || !window.isSecureContext) return legacyCopy(text);
  try { await navigator.clipboard.writeText(text); return true; } catch { return legacyCopy(text); }
}
const timers = new WeakMap();
let lastMessage = "";
function report(button, ok, okText) {
  const tip = button.nextElementSibling;
  clearTimeout(timers.get(button));
  button.removeAttribute("data-copied"); button.removeAttribute("data-failed");
  let message = ok ? okText : "Copy failed";
  if (message === lastMessage) message += "\u00A0";
  lastMessage = message;
  tip.textContent = "";
  button.setAttribute(ok ? "data-copied" : "data-failed", "");
  requestAnimationFrame(() => (tip.textContent = message));
  timers.set(button, setTimeout(() => { button.removeAttribute("data-copied"); button.removeAttribute("data-failed"); tip.textContent = ""; }, 2200));
}
async function copyRead(button, text, okText) {
  if (!text) return report(button, false);
  let ok = false;
  try { ok = await copyText(text); } catch {}
  report(button, ok, okText || `Copied ${words(text)} words`);
}
"""

SCRIPT_JS = r"""
const fine = matchMedia("(pointer: fine)").matches;
const KEY = "script:" + DATA.id;
const LINES = DATA.lines, byI = new Map(LINES.map((l) => [l.i, l]));
const blank = () => ({ comments: [], edits: [], overall: "" });
let state; try { state = Object.assign(blank(), JSON.parse(store(KEY)) || {}); } catch { state = blank(); }
const save = () => store(KEY, JSON.stringify(state));

// Notes are kept in this browser until Send. If the file changed since, find each note's line again by its text.
const anchor = (text, hint) => {
  if (byI.get(hint)?.kind === "line" && byI.get(hint).text === text) return hint;
  const hits = LINES.filter((l) => l.kind === "line" && l.text === text);
  return hits.length === 1 ? hits[0].i : -1;
};
state.edits.forEach((e) => (e.line = anchor(e.before, e.line)));
state.comments.forEach((c) => (c.line = anchor(c.line_text, c.line)));
save();

// Lines that changed since the version this browser last showed.
let seen; try { seen = JSON.parse(store("seen:" + DATA.id)); } catch {}
const texts = LINES.filter((l) => l.kind === "line").map((l) => l.text);
if (!seen || !Array.isArray(seen.lines)) seen = { version: DATA.version, lines: texts, prev: null };
else if (seen.version !== DATA.version) seen = { version: DATA.version, lines: texts, prev: seen.lines };
store("seen:" + DATA.id, JSON.stringify(seen));
const prev = seen.prev && new Set(seen.prev);
const CHANGED = new Set(prev ? LINES.filter((l) => l.kind === "line" && !prev.has(l.text)).map((l) => l.i) : []);

let active = -1, mode = null, quote = "";
const editOf = (i) => state.edits.find((e) => e.line === i);
const shown = (l) => { const e = editOf(l.i); return e ? e.after : l.text; };
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const button = (label, cls, fn) => { const b = el("button", "btn " + (cls || ""), label); b.type = "button"; b.onclick = (ev) => { ev.stopPropagation(); fn(); }; return b; };

// Text with each commented quote wrapped in <mark>. Built from text nodes, never innerHTML.
function fill(p, text, quotes) {
  const spans = [];
  for (const q of quotes) { const at = text.indexOf(q); if (q && at >= 0) spans.push([at, at + q.length]); }
  spans.sort((a, b) => a[0] - b[0]);
  let pos = 0;
  for (const [a, b] of spans) {
    if (a < pos) continue;
    if (a > pos) p.append(text.slice(pos, a));
    p.append(el("mark", "", text.slice(a, b)));
    pos = b;
  }
  p.append(text.slice(pos));
}

function lineEl(l) {
  const e = editOf(l.i), notes = state.comments.filter((c) => c.line === l.i);
  const box = el("div", "line" + (l.read ? " read" : "") + (notes.length ? " noted" : "") + (e ? " edited" : "") + (l.i === active ? " active" : ""));
  box.dataset.i = l.i; box.tabIndex = 0;
  const p = el("p", "t");
  if (e && !e.after.trim()) { p.classList.add("gone"); p.textContent = l.text; }
  else fill(p, shown(l), notes.map((n) => n.quote));
  const tags = el("div", "tags");
  if (e) tags.append(el("span", "tag edit", e.after.trim() ? "Your edit" : "You removed this line"));
  if (notes.length) tags.append(el("span", "tag note", notes.length === 1 ? "1 note" : notes.length + " notes"));
  if (CHANGED.has(l.i) && !e) tags.append(el("span", "tag new", "Changed in this draft"));
  box.append(p, tags);
  if (l.i === active) box.append(mode === "comment" ? composer(l) : mode === "edit" ? editor(l) : acts(l));
  return box;
}
function acts(l) {
  const row = el("div", "acts");
  row.append(button("Comment", "", () => setActive(l.i, "comment")), button("Edit", "", () => setActive(l.i, "edit")));
  if (editOf(l.i)) row.append(button("Undo my edit", "quiet", () => { dropEdit(l.i); setActive(l.i, null); }));
  return row;
}
function composer(l) {
  const box = el("div", "box composer");
  if (quote) box.append(el("p", "q", "“" + quote + "”"));
  const ta = el("textarea"); ta.id = "draft"; ta.placeholder = "What should change here?"; ta.setAttribute("aria-label", "Your note on this line");
  const foot = el("div", "foot");
  foot.append(el("span", "hint", fine ? "Enter adds the note. Shift+Enter makes a new line." : ""), button("Cancel", "quiet push", () => { ta.value = ""; setActive(l.i, null); }), button("Add note", "primary", () => setActive(-1, null)));
  ta.onkeydown = (ev) => {
    if (ev.key === "Escape") { ta.value = ""; setActive(l.i, null); }
    else if (ev.key === "Enter" && !ev.shiftKey && fine) { ev.preventDefault(); setActive(-1, null); }
  };
  box.append(ta, foot);
  return box;
}
function editor(l) {
  const box = el("div", "box editor");
  const ta = el("textarea"); ta.id = "draft"; ta.value = shown(l); ta.setAttribute("aria-label", "Edit this line");
  const foot = el("div", "foot");
  foot.append(el("span", "hint", "Clear the text to remove the line. A new line splits it in two."), button("Cancel", "quiet push", () => { ta.value = shown(l); setActive(l.i, null); }), button("Save", "primary", () => setActive(-1, null)));
  ta.onkeydown = (ev) => {
    if (ev.key === "Escape") { ta.value = shown(l); setActive(l.i, null); }
    else if (ev.key === "Enter" && !ev.shiftKey && fine) { ev.preventDefault(); setActive(-1, null); }
  };
  box.append(ta, foot);
  return box;
}
const dropEdit = (i) => { state.edits = state.edits.filter((e) => e.line !== i); save(); };

// Whatever is typed in an open box is kept when the reviewer moves on: a note is added, an edit is saved.
// Both show in the Notes list, where one click removes them.
function commitOpen() {
  const ta = $("draft"), l = byI.get(active);
  if (!ta || !l) return;
  if (mode === "comment" && ta.value.trim()) state.comments.push({ line: l.i, line_text: l.text, quote, note: ta.value.trim() });
  if (mode === "edit") {
    const after = ta.value.replace(/\r/g, "").split("\n").map((s) => s.trim()).filter(Boolean).join("\n");
    state.edits = state.edits.filter((e) => e.line !== l.i);
    if (after !== l.text.trim()) state.edits.push({ line: l.i, before: l.text, after });
  }
  save();
}
function setActive(i, m, q) {
  commitOpen();
  active = i; mode = m; quote = q || "";
  hidePop(); render();
  if (m) { const ta = $("draft"); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  else if (i >= 0 && lineNode(i)) lineNode(i).focus({ preventScroll: true });
}

function renderDoc() {
  const doc = $("doc"); doc.textContent = "";
  for (const l of LINES) {
    if (l.kind === "h") doc.append(el("h2", "h" + (l.text.replace(/[#*\\\s]/g, "").toLowerCase() === DATA.read ? " readh" : ""), l.text));
    else if (l.kind === "line") doc.append(lineEl(l));
  }
}
const lineNode = (i) => document.querySelector(`.line[data-i="${i}"]`);
const go = (i) => { if (!lineNode(i)) return; setActive(i, null); lineNode(i).scrollIntoView({ block: "center", behavior: "smooth" }); };
function renderRail() {
  const list = $("list"); list.textContent = "";
  const items = [...state.edits.map((e) => ({ e, line: e.line })), ...state.comments.map((c) => ({ c, line: c.line }))];
  items.sort((a, b) => (a.line < 0) - (b.line < 0) || a.line - b.line);
  for (const it of items) {
    const card = el("div", "card" + (it.e ? " e" : "")), head = el("div", "card-head");
    const l = byI.get(it.line), where = it.line < 0 ? "This line changed since" : (l && l.section) || "Script";
    head.append(el("span", "", it.e ? "Your edit · " + where : where));
    if (it.e) {
      head.append(button("Undo", "quiet push", () => { state.edits = state.edits.filter((x) => x !== it.e); save(); render(); }));
      card.append(head, el("p", "was", it.e.before.trim()), el("p", "", it.e.after.trim() || "(line removed)"));
    } else {
      head.append(button("Delete", "quiet push", () => { state.comments = state.comments.filter((x) => x !== it.c); save(); render(); }));
      card.append(head, el("p", "ctx", "“" + (it.c.quote || it.c.line_text.trim()).slice(0, 140) + "”"), el("p", "", it.c.note));
    }
    if (it.line >= 0) card.onclick = () => go(it.line);
    list.append(card);
  }
  if (!items.length) list.append(el("p", "empty", "Tap a line to comment on it or edit it." + (fine ? " Select words to comment on just those." : "")));
  $("count").textContent = items.length || "";
  $("send").textContent = hasNotes() ? "Send notes" : "Looks good";
}
const hasNotes = () => state.comments.length > 0 || state.edits.length > 0 || state.overall.trim() !== "";
const busy = () => hasNotes() || !!($("draft") && $("draft").value.trim() && mode === "comment") || mode === "edit";
function render() { renderDoc(); renderRail(); }

$("doc").addEventListener("click", (ev) => {
  if (ev.target.closest("button, textarea, .box")) return;
  const n = ev.target.closest(".line");
  if (!n || String(getSelection()).trim()) return;
  const i = +n.dataset.i;
  setActive(active === i && !mode ? -1 : i, null);
});
$("doc").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target.classList && ev.target.classList.contains("line")) { ev.preventDefault(); setActive(+ev.target.dataset.i, null); }
});
$("overall").value = state.overall;
$("overall").oninput = () => { state.overall = $("overall").value; save(); renderRail(); };

// Select words with a mouse to comment on just those words. Touch screens tap the line instead:
// their own selection menu sits where this button would.
const pop = $("selpop");
let picked = null, popDown = false;
const showPop = () => { if (pop.showPopover) { if (!pop.matches(":popover-open")) pop.showPopover(); } else pop.classList.add("open"); };
function hidePop() { if (popDown) return; if (pop.hidePopover) { if (pop.matches(":popover-open")) pop.hidePopover(); } else pop.classList.remove("open"); }
function checkSelection() {
  if (!fine) return;
  const s = getSelection();
  if (!s || s.isCollapsed || !s.rangeCount) return hidePop();
  const r = s.getRangeAt(0);
  const from = r.startContainer.nodeType === 1 ? r.startContainer : r.startContainer.parentElement;
  const t = from && from.closest(".line .t");
  if (!t || t.classList.contains("gone")) return hidePop();
  const clip = r.cloneRange();
  if (!t.contains(r.endContainer)) clip.setEnd(t, t.childNodes.length);   // a selection that runs past the line counts up to its end
  const text = clip.toString().replace(/\s+/g, " ").trim();
  if (!text) return hidePop();
  picked = { line: +t.closest(".line").dataset.i, quote: text.slice(0, 500) };
  showPop();
  const b = clip.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(innerWidth - w - 8, b.left + b.width / 2 - w / 2)) + "px";
  pop.style.top = (b.top - h - 8 < 8 ? b.bottom + 8 : b.top - h - 8) + "px";
}
let selTimer;
document.addEventListener("selectionchange", () => { clearTimeout(selTimer); selTimer = setTimeout(checkSelection, 120); });
addEventListener("scroll", () => requestAnimationFrame(checkSelection), { passive: true });
pop.addEventListener("pointerdown", (ev) => { ev.preventDefault(); popDown = true; });
addEventListener("pointerup", () => setTimeout(() => (popDown = false), 0));
addEventListener("pointercancel", () => (popDown = false));
$("selbtn").onclick = () => { popDown = false; if (picked) { getSelection().removeAllRanges(); setActive(picked.line, "comment", picked.quote); } };

// Copy read: the read, then hooks 2 and up as they are filmed ("Two. <line>"). Nothing before the
// first line, and none of the other sections. Unsent edits are included, since they're what the page shows.
const HOOK = /^Hook\s+(\d+)\s*\([^)]*\)\s*:\s*(.+)$/i, NUMBER = { 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six" };
function teleprompter() {
  const flat = (t) => t.replace(/\s+/g, " ").trim();
  const read = flat(LINES.filter((l) => l.read && l.kind === "line").map(shown).join(" "));
  const hooks = [];
  for (const l of LINES) {
    if (l.kind !== "line" || l.section.replace(/[#*\\\s]/g, "").toLowerCase() !== DATA.hooks) continue;
    const m = shown(l).replace(/^\s*[-*]\s+/, "").replace(/\*\*/g, "").trim().match(HOOK);
    if (m && +m[1] > 1) hooks.push(`${NUMBER[m[1]] || m[1]}. ${flat(m[2])}`);
  }
  if (!read) return { text: "", label: "" };
  return { text: flat([read, ...hooks].join(" ")), label: `Copied ${words(read)} words` + (hooks.length ? ` + ${hooks.length} hook${hooks.length > 1 ? "s" : ""}` : "") };
}
$("copy").onclick = () => { const t = teleprompter(); copyRead($("copy"), t.text, t.label); };

const say = (text) => ($("status").textContent = text);
const reload = () => { try { sessionStorage.setItem("scroll:" + DATA.id, String(scrollY)); } catch {} location.reload(); };
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Script-Token": DATA.token }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
$("send").onclick = async () => {
  commitOpen(); active = -1; mode = null; render();
  say("Sending…");
  try {
    const out = await post("/api/send", { id: DATA.id, base: DATA.version, comments: state.comments, edits: state.edits, overall: state.overall.trim() });
    let msg = out.approved ? "Approved." + (out.status_changed ? ` Marked ${out.status_label.toLowerCase()}.` : "") : "Sent. This page reloads when the next draft is ready.";
    if (out.missed) msg += ` ${out.missed} of your edits couldn't be placed, so they went as notes.`;
    // The reviewer's own edits aren't "changed in this draft" on the next load.
    seen.lines = seen.lines.concat(...state.edits.map((e) => e.after.split("\n")));
    store("seen:" + DATA.id, JSON.stringify(seen));
    state = blank(); save();
    try { sessionStorage.setItem("flash:" + DATA.id, msg); } catch {}
    reload();
  } catch (err) {
    say(err.message === "403" ? "This page is out of date. Reload it and send again; your notes are kept." : "Couldn't send: the script server on the computer isn't answering. Your notes are kept in this browser.");
  }
};
$("state").onchange = async () => {
  try { await post("/api/status", { id: DATA.id, status: $("state").value }); $("state").dataset.role = DATA.roles[$("state").value] || "other"; if (!busy()) reload(); else say("Status saved."); }
  catch { say("Couldn't save the status."); $("state").value = DATA.status; }
};

// Reload when the file changes, unless notes are waiting to be sent.
let newer = false;
async function check() {
  if (document.hidden) return;
  try {
    const out = await (await fetch("/api/version?id=" + encodeURIComponent(DATA.id))).json();
    if (out.version === DATA.version) return;
    if (!busy()) reload();
    else if (!newer) { newer = true; say("A newer draft is ready. Send or clear your notes to load it."); }
  } catch {}
}
setInterval(check, 3000);
document.addEventListener("visibilitychange", check);

new ResizeObserver(() => document.documentElement.style.setProperty("--top", document.querySelector(".top").offsetHeight + "px")).observe(document.querySelector(".top"));
render();
try {
  const y = sessionStorage.getItem("scroll:" + DATA.id), flash = sessionStorage.getItem("flash:" + DATA.id);
  sessionStorage.removeItem("scroll:" + DATA.id); sessionStorage.removeItem("flash:" + DATA.id);
  if (y) scrollTo(0, +y);
  if (flash) say(flash);
} catch {}
"""

LIST_JS = r"""
document.addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-read]");
  if (b) { const r = READS[b.dataset.read] || {}; copyRead(b, r.text || "", r.label); }
});
// Don't reload over a half-typed idea.
const typing = () => $("new").open && ($("newtitle").value.trim() || $("newbody").value.trim());
async function check() {
  if (document.hidden || typing()) return;
  try { const out = await (await fetch("/api/version")).json(); if (out.version !== VERSION) location.reload(); } catch {}
}
$("newform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("newstatus").textContent = "Saving…";
  try {
    const r = await fetch("/api/new", { method: "POST", headers: { "Content-Type": "application/json", "X-Script-Token": TOKEN },
      body: JSON.stringify({ title: $("newtitle").value, body: $("newbody").value }) });
    if (!r.ok) throw new Error(String(r.status));
    location.href = "/s/" + (await r.json()).id;
  } catch (err) {
    $("newstatus").textContent = err.message === "400" ? "Couldn't save: a script with that title already exists today. Change the title." : "Couldn't save. Your text is still here; reload the page and try again.";
  }
});
setInterval(check, 5000);
document.addEventListener("visibilitychange", check);
"""

HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>__TITLE__</title>
<style nonce="__NONCE__">__CSS__</style></head>
"""

SCRIPT_PAGE = HEAD + """<body>
  <header class="top">
    <a class="back" href="/">Scripts</a>
    <h1>__H1__</h1>
    <span class="meta">__META__</span>
    <select id="state" aria-label="Status" data-role="__ROLE__">__OPTIONS__</select>
  </header>
  <div class="wrap">
    <main class="doc" id="doc"></main>
    <aside class="rail" aria-label="Your notes">
      <div class="rail-scroll">
        <div class="rail-head"><h2 class="section">Notes</h2><span id="count" class="meta"></span></div>
        <div id="list" class="cards"></div>
        <textarea id="overall" placeholder="Overall note (optional)" aria-label="Overall note"></textarea>
      </div>
      <div class="actions">
        <span class="copy"><button id="copy" class="btn" type="button">Copy read</button><span class="tip" role="status"></span></span>
        <button id="send" class="btn primary" type="button">Looks good</button>
        <div id="status" role="status"></div>
      </div>
    </aside>
  </div>
  <div id="selpop" popover="manual"><button id="selbtn" class="btn primary" type="button">Comment</button></div>
  <script nonce="__NONCE__">
    const DATA = __DATA__;
    __JS__
  </script>
</body></html>"""

LIST_PAGE = HEAD + """<body>
  <header class="top home"><h1>__H1__</h1><span class="meta">__META__</span></header>
  <main class="list">
    <details class="new" id="new">
      <summary class="btn">New idea or script</summary>
      <form id="newform">
        <label for="newtitle">Title</label>
        <input id="newtitle" name="title" required maxlength="200" autocomplete="off">
        <label for="newbody">Notes or the script (optional)</label>
        <textarea id="newbody" name="body" placeholder="A few words is saved as an idea. A full script is saved as a draft, and all of it is the read."></textarea>
        <div class="foot"><span id="newstatus" class="hint" role="status"></span><button class="btn primary push" type="submit">Save</button></div>
      </form>
    </details>
    __GROUPS__
  </main>
  <script nonce="__NONCE__">
    const READS = __READS__, VERSION = __VERSION__, TOKEN = __TOKEN__;
    __JS__
  </script>
</body></html>"""


def role(status):
    """The color role of a status: approved (green), draft (amber), idea (blue), or other (neutral)."""
    return {APPROVED: "approved", DRAFT: "draft", IDEA: "idea"}.get(status, "other")


def js_data(obj):
    """JSON that is safe inside a <script> block: no '<' can close the tag or open a comment."""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def fill_page(page, **parts):
    # One pass over the template, so text inside a script can never be read as a placeholder.
    return re.sub(r"__([A-Z0-9]+)__", lambda m: parts.get(m.group(1), m.group(0)), page)


def script_page(s, token, nonce):
    data = {"id": s.id, "title": s.title, "status": s.status, "version": s.version, "token": token, "hooks": HOOKS_KEY, "read": READ_KEY,
            "roles": {st: role(st) for st in STATUSES}, "lines": layout(s.lines)}
    meta = f"{s.words} words · about {duration(s.words)}" if s.words else "No read yet"
    options = "".join(f'<option value="{st}"{" selected" if st == s.status else ""}>{html.escape(STATUS_LABEL[st])}</option>'
                      for st in STATUSES)
    return fill_page(SCRIPT_PAGE, TITLE=html.escape(s.title), CSS=CSS, NONCE=nonce, H1=html.escape(s.title), META=meta,
                     OPTIONS=options, ROLE=role(s.status), JS=SHARED_JS + SCRIPT_JS, DATA=js_data(data))


def list_version(desk):
    scripts = desk.all()
    key = "|".join(f"{s.id}:{s.version}" for s in scripts) + "|" + ",".join(f.name for _, f in desk.unread())
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def list_page(desk, token, nonce):
    scripts = desk.all()
    waiting = {}
    for sid, _ in desk.unread():
        waiting[sid] = waiting.get(sid, 0) + 1
    reads, groups = {}, []
    for st in LIST_ORDER:
        rows = sorted((s for s in scripts if s.status == st), key=lambda s: (s.meta.get("updated", ""), s.id), reverse=True)
        if not rows:
            continue
        items = []
        for s in rows:
            full = s.words and st != IDEA   # an idea is a few words of notes, not something to read aloud
            sub = [f"{s.words} words", f"about {duration(s.words)}"] if full else []
            if s.meta.get("updated"):
                sub.append("updated " + html.escape(short_date(s.meta["updated"])))
            sub = ['<span class="upd">' + x + "</span>" if x.startswith("updated ") else f"<span>{x}</span>" for x in sub]
            note = f'<span class="waiting">{waiting[s.id]} sent, waiting on the agent</span>' if s.id in waiting else ""
            copy = ""
            if full:
                reads[s.id] = {"text": s.teleprompter, "label": s.copied}
                copy = (f'<span class="copy"><button class="btn" type="button" data-read="{s.id}" '
                        f'aria-label="Copy the read for {html.escape(s.title, quote=True)}">Copy read</button>'
                        f'<span class="tip" role="status"></span></span>')
            items.append(f'<div class="item"><a href="/s/{s.id}"><span class="title">{html.escape(s.title)}</span>'
                         f'<span class="sub">{"".join(sub)}{note}</span></a>{copy}</div>')
        head = f'{html.escape(STATUS_LABEL[st])} <span class="n">{len(rows)}</span>'
        if st in CFG["collapsed"]:
            groups.append(f'<details class="group" data-role="{role(st)}"><summary>{head}</summary>{"".join(items)}</details>')
        else:
            groups.append(f'<section data-role="{role(st)}"><h2 class="group-head">{head}</h2>{"".join(items)}</section>')
    if not groups:
        groups = ['<p class="empty">No scripts yet. The agent adds one with script_review.py new.</p>']
    return fill_page(LIST_PAGE, TITLE=html.escape(str(CFG["title"])), H1=html.escape(str(CFG["title"])), CSS=CSS, NONCE=nonce, META=f"{len(scripts)} scripts",
                     GROUPS="".join(groups), JS=SHARED_JS + LIST_JS, READS=js_data(reads), TOKEN=json.dumps(token),
                     VERSION=json.dumps(list_version(desk)))


# ---------- the server ----------

def load_token(state):
    """One token per install, kept in the state folder, so open tabs survive a server restart."""
    f = Path(state) / "token"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    f.write_text(token)
    os.chmod(f, 0o600)
    return token


def make_server(desk, port=PORT, ts_name=tailscale_name):
    """A server for desk on 127.0.0.1:port (0 picks a free port). ts_name() returns this computer's
    Tailscale name; it is asked at most once a minute."""
    token = load_token(desk.state)
    ts = {"name": None, "at": 0.0}

    def ts_host():
        if CFG["tailscale"] and time.time() - ts["at"] > 60:
            ts["name"], ts["at"] = ts_name(), time.time()
        return ts["name"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "ScriptPage"

        def log_message(self, *a):
            pass

        def allowed_hosts(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}": "http", f"localhost:{port}": "http"}
            name = ts_host()
            if name and CFG["tailscale"]:   # `tailscale serve --https=<port>` forwards here and keeps the Host header
                hosts[f"{name}:{port}"] = "https"
            return hosts

        def host_ok(self):
            if self.headers.get("Host") in self.allowed_hosts():
                return True
            self.send_error(403)
            return False

        def reply(self, body, ctype="application/json", nonce=None, code=200):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            body = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if nonce:
                self.send_header("Content-Security-Policy",
                                 f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                                 "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.host_ok():
                return
            url = urlparse(self.path)
            nonce = secrets.token_urlsafe(16)
            if url.path == "/":
                return self.reply(list_page(desk, token, nonce), "text/html; charset=utf-8", nonce)
            if url.path.startswith("/s/"):
                s = desk.load(url.path[3:])
                if not s:   # another id for the script (alias_fields) in a link or a note: send it to the script's own address
                    old = desk.find(url.path[3:])
                    if not old:
                        return self.send_error(404)
                    self.send_response(302)
                    self.send_header("Location", f"/s/{old.id}")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                return self.reply(script_page(s, token, nonce), "text/html; charset=utf-8", nonce)
            if url.path == "/api/version":
                sid = parse_qs(url.query).get("id", [None])[0]
                if sid is None:
                    return self.reply({"version": list_version(desk)})
                s = desk.load(sid)
                return self.reply({"version": s.version, "status": s.status}) if s else self.send_error(404)
            if url.path == "/api/ping":
                return self.reply({"ok": True, "scripts": str(desk.scripts)})
            self.send_error(404)

        def do_POST(self):
            if not self.host_ok():
                return
            hosts, origin = self.allowed_hosts(), self.headers.get("Origin")
            n = int(self.headers.get("Content-Length") or 0)
            if (not secrets.compare_digest(self.headers.get("X-Script-Token", ""), token)
                    or (origin and origin not in {f"{scheme}://{host}" for host, scheme in hosts.items()})
                    or not self.headers.get("Content-Type", "").startswith("application/json") or n > MAX_BODY):
                return self.send_error(403)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/send":
                    out = desk.send(clean_payload(data))
                elif self.path == "/api/new":   # an idea or a hand-typed script, from the list page
                    text = str(data.get("body", ""))[:40000]
                    kind = IDEA if len(read_text(normalize_body(text).split("\n")).split()) < 40 else DRAFT
                    s = desk.create(str(data["title"])[:200], text, kind, extra={"written_by": slug(REVIEWER)})
                    out = {"ok": True, "id": s.id, "status": s.status}
                elif self.path == "/api/status":
                    status = str(data["status"])
                    assert status in STATUSES
                    s = desk.set_status(str(data["id"]), status)
                    out = s and {"ok": True, "status": s.status, "version": s.version}
                else:
                    return self.send_error(404)
            except Exception:
                return self.send_error(400)
            return self.reply(out) if out else self.send_error(404)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
