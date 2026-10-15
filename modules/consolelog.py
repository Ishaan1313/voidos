"""
modules/consolelog.py — everything Void OS prints to its own console window.

Plain, consistently indented text: no live screen, no key presses, nothing
that behaves differently on one OS than another. Every line starts two
spaces in; a long message wraps under its own first line instead of running
off the edge; the start-up summary lines its labels up in a column.

This module imports nothing from the rest of Void OS, so anything may use it
(config.py uses it before the app exists).
"""
import shutil
import textwrap

INDENT = "  "
_MARK = {"warn": "! ", "error": "x ", "ok": "+ "}


def _width() -> int:
    try:
        cols = shutil.get_terminal_size((100, 24)).columns
    except Exception:
        cols = 100
    return max(60, min(cols, 110))


def _emit(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:        # a console that can't show a character: never let that stop the server
        try:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass
    except (OSError, ValueError):     # stdout closed (detached service)
        pass


def blank() -> None:
    _emit()


def say(msg, tag: str = "", level: str = "") -> None:
    """One message, indented, optionally tagged ('[tunnel] ...') and marked ('! ' warning, 'x ' error).
    Multi-line text keeps every line indented; long lines wrap under the first."""
    head = INDENT + (f"[{tag}] " if tag else "") + _MARK.get(level, "")
    pad = " " * len(head)
    width = max(30, _width() - len(head))
    first = True
    for raw in str(msg).splitlines() or [""]:
        parts = textwrap.wrap(raw, width=width, break_long_words=False, break_on_hyphens=False,
                              replace_whitespace=False, drop_whitespace=True) or [""]
        for part in parts:
            _emit((head if first else pad) + part)
            first = False


def warn(msg, tag: str = "") -> None:
    say(msg, tag, "warn")


def error(msg, tag: str = "") -> None:
    say(msg, tag, "error")


def rows(items, label_width: int = 0) -> None:
    """Aligned 'Label   value' lines; a long value wraps under its own column."""
    items = [(str(k), str(v)) for k, v in items if v not in (None, "")]
    lw = label_width or max((len(k) for k, _ in items), default=0)
    head_w = len(INDENT) + lw + 2
    width = max(30, _width() - head_w)
    for k, v in items:
        lines = []
        for raw in v.splitlines() or [""]:
            lines += textwrap.wrap(raw, width=width, break_long_words=False, break_on_hyphens=False) or [""]
        _emit(f"{INDENT}{k:<{lw}}  {lines[0]}")
        for extra in lines[1:]:
            _emit(" " * head_w + extra)


def banner(title: str, items) -> None:
    """The start-up summary box."""
    w = min(_width() - len(INDENT), 64)
    bar = "=" * w
    _emit()
    _emit(INDENT + bar)
    _emit(INDENT + title)
    _emit(INDENT + "-" * w)
    rows(items)
    _emit(INDENT + bar)
    _emit()


