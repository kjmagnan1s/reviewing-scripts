---
name: reviewing-scripts
description: Runs a local browser page where a reviewer goes through a video script line by line, comments on lines or selected words, edits lines directly, and sends the notes back to the agent, which revises the markdown file. Use when the user wants to review, comment on, or approve a script draft in the browser, asks for the script page or their script list, wants a script on their phone or teleprompter, or after drafting a script that needs their review.
---

# Reviewing scripts

Each script is one markdown file. The reviewer reads it on a local page, and their notes come back
to you as markdown. All commands are `python3 scripts/script_review.py <command>`, with the path
relative to this skill's folder. Python 3.9 or later, no packages to install.

## The review loop

1. **Save the script.** Write the body to a temporary file and run
   `script_review.py new --title "<title>" --body-file <file> --status drafted`. It prints the
   script's id, file path, and page address. Put one sentence or beat on each line of the read, so
   each line can take its own comment.
2. **Start the page.** Run `script_review.py up`, then `script_review.py open <id>`. Give the
   reviewer the address.
3. **Wait for notes.** Run `script_review.py wait <id>` in the background. It prints the notes
   when the reviewer clicks **Send notes**, and exits with code 2 after two hours with no send.
4. **Apply the notes.** Edit the script file with the smallest change that answers each note. The
   open page reloads itself and marks the lines that changed.
5. **Repeat from step 3** until the notes say "Looks good." That send also moves a draft to the
   approved status.

If you weren't waiting when notes were sent, `script_review.py inbox` prints every unread set.

## Rules for revising

- **Keep the reviewer's edits word for word.** Lines listed under "edits (already saved to the
  file)" are their own wording and are in the file before you see the notes. Change one only when
  a note asks you to.
- **Change only what a note covers.** Don't rewrite lines that have no comment.
- **Apply "Edits that didn't land" by hand.** The line moved before the send, so the file wasn't
  changed.
- **Leave frontmatter keys you didn't set.** Change the status with `script_review.py status <id>
  <status>`, not by editing the file.
- **Don't delete a script file** unless the reviewer asks.

## The file format

```markdown
---
type: script
id: 2026-01-15-three-prompt-habits
title: Three prompt habits
status: drafted
---

## Spoken Script

Your first line, one sentence per line.
The second line.

## Alternate hooks

Hook 2 (question): A second opening line to film.

## Caption

Anything else you keep with the script.
```

**Copy read** on the page copies the `Spoken Script` section as one paragraph, then each alternate
hook from hook 2 up as "Two. <line>". A file with no `Spoken Script` section is all read.
`script_review.py read <id>` prints the same text.

## Other commands

- `list [--status <status>] [--json]`: every script with its status and word count.
- `status <id> <status>`: set a status (`idea`, `drafted`, `ready`, `filmed`, `posted` by default).
- `config`: the settings in use and the config file's path.
- `down`: stop the page server.

Settings (folders, port, section names, statuses, the reviewer's name) and phone access through
Tailscale are described in the README at the repository root.
