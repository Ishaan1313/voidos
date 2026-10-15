"""
modules/envfile.py — the one place that reads and edits .env.

Used by Void OS itself (setup wizard, terminal password, first-run secret
key) and by the installer ("first setup/helper.py"), so every writer follows
the same rules. Standard library only: the installer runs it before any
packages are installed.

Rules:
  - An existing `KEY=value` line is updated in place; its inline comment
    (`KEY=value   # explanation`) is kept.
  - If the key only exists as a commented-out example (`# KEY=…`, as in
    .env.example), that line is switched on in place rather than a duplicate
    being appended.
  - Otherwise the key is appended at the end.
  - Values are written unquoted when safe, else in single quotes — never
    double quotes, where python-dotenv turns "\\n" into a newline (which
    silently breaks Windows paths like C:\\Users\\You\\new).
  - `None` removes the key.
  - The file is replaced atomically and kept owner-readable only (POSIX):
    it holds the secret key and any API tokens.
  - A new .env is a full copy of "first setup/.env.example": the active
    essentials plus every other setting, commented out with its default, so
    everything is right there to switch on.
"""
from __future__ import annotations

import os
import re

_ACTIVE = r"^\s*{k}\s*="
_COMMENTED = r"^\s*#\s*{k}\s*="


def _split_value(rest: str) -> tuple[str, str]:
    """'value   # comment' → ('value', '   # comment'); quoted values intact."""
    rest = rest.rstrip("\n")
    if rest[:1] in ("'", '"'):
        q = rest[0]
        end = rest.find(q, 1)
        if end != -1:
            return rest[: end + 1], rest[end + 1:]
    m = re.search(r"\s+#", rest)
    return (rest[: m.start()], rest[m.start():]) if m else (rest, "")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def format_value(value: str) -> str:
    value = str(value)
    if value == "" or re.fullmatch(r"[A-Za-z0-9_./:\\@+,=\-$]+", value):
        return value
    return "'" + value.replace("'", "") + "'"


def read_lines(path) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def write_lines(path, lines: list[str]) -> None:
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def get(path, key: str) -> str:
    pat = re.compile(_ACTIVE.format(k=re.escape(key)))
    for line in read_lines(path):
        if pat.match(line):
            return _unquote(_split_value(line.split("=", 1)[1])[0])
    return ""


def update(path, updates: dict) -> None:
    """Apply {KEY: value-or-None} to the file at `path` (created if missing)."""
    lines = read_lines(path)
    for key, value in updates.items():
        active = re.compile(_ACTIVE.format(k=re.escape(key)))
        commented = re.compile(_COMMENTED.format(k=re.escape(key)))
        idx = next((i for i, l in enumerate(lines) if active.match(l)), None)
        if value is None:
            if idx is not None:
                del lines[idx]
            continue
        if idx is None:
            idx = next((i for i, l in enumerate(lines) if commented.match(l)), None)
        if idx is not None:
            _old, comment = _split_value(lines[idx].split("=", 1)[1])
            lines[idx] = f"{key}={format_value(value)}{comment}"
        else:
            lines.append(f"{key}={format_value(value)}")
    write_lines(path, lines)


def sync_missing(path, example) -> int:
    """Append every setting that "first setup/.env.example" lists and `path` does not mention
    at all (neither active nor commented out), as commented-out lines with their explanation.
    Nothing existing is touched and no value changes: an old .env just gains the options added
    since it was made. Returns how many settings were added."""
    have = set()
    for ln in read_lines(path):
        m = re.match(r"^\s*#?\s*([A-Za-z][A-Za-z0-9_]*)\s*=", ln)
        if m:
            have.add(m.group(1))
    ex = read_lines(example)
    out, block, added, seen = [], [], 0, set()
    for ln in ex:
        s = ln.strip()
        m = re.match(r"^#?\s*([A-Za-z][A-Za-z0-9_]*)=", s)
        if m and m.group(1) not in have and m.group(1) not in seen and s.startswith("#"):
            seen.add(m.group(1))
            out.extend(block)
            block = []
            out.append(ln)
            added += 1
        elif m and not s.startswith("#") and m.group(1) not in have and m.group(1) not in seen:
            seen.add(m.group(1))                      # an active line in the example: add it commented
            out.extend(block)
            block = []
            out.append("# " + ln)
            added += 1
        elif not s:
            block = []
        elif s.startswith("#") and not s.startswith("# ──") and not s.startswith("# ═"):
            block.append(ln)
    if not added:
        return 0
    cur = read_lines(path)
    cur += ["", "", "# ── Added by an update (new options; defaults shown) ──────────────────", ""] + out
    write_lines(path, cur)
    return added


def create_from_example(path, example) -> bool:
    """Create `path` from `example` if it doesn't exist. True if created. The whole
    example is copied: the active essentials and every other setting, commented
    out with its default. If the example file is missing, a minimal file is
    written instead (Void OS then runs on its defaults)."""
    if os.path.exists(path):
        return False
    lines = read_lines(example)
    if lines:
        write_lines(path, lines)
    else:
        write_lines(path, ["# Void OS configuration — see 'first setup/.env.example'", "VOID_SECRET_KEY="])
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return True
