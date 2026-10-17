"""
modules/notifications.py — outbound notifications for major Void OS
events (login, admin setting changes, etc.) via email, Discord, or
Telegram. Dependency-free like every other integration module in this
app: email uses stdlib smtplib/email.mime, Discord and Telegram are both
just one HTTPS POST via urllib.request (a webhook URL for Discord, the
Bot API for Telegram) — no new pip packages for what's underneath three
simple, well-documented HTTP calls.

This module only knows how to send; voidos.py owns *when* (which audit
actions count as "major"), the per-channel config, retries, and recording
delivery status.

Every send_* returns (ok, message). On failure, `message` is a plain-
language explanation of what's wrong with *the configuration* (wrong
password, deleted webhook, bot not in the chat...) — not a raw exception —
because it's shown directly under the channel in Settings.
"""
from __future__ import annotations

import html
import json
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

TIMEOUT = 10
DISCORD_DESC_MAX = 4000
TELEGRAM_MAX = 4000


def _network_reason(e: Exception, what: str) -> str:
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        e = e.reason if isinstance(e.reason, Exception) else e
    if isinstance(e, socket.gaierror):
        return f"Couldn't find {what} — check the address, and that this server has internet/DNS access."
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"Timed out connecting to {what} — the server may be blocked by a firewall or offline."
    if isinstance(e, ConnectionRefusedError):
        return f"{what} refused the connection — check the host and port."
    if isinstance(e, ssl.SSLError):
        return (f"Secure connection to {what} failed — check the TLS/SSL setting matches the port "
                f"(465 = SSL, 587 = STARTTLS).")
    if isinstance(e, OSError):
        return f"Network error reaching {what} — check this server's internet connection."
    return f"Couldn't reach {what}."


def _http_post_json(url: str, payload: dict, what: str) -> tuple[bool, int, str]:
    """(ok, http_status, body_or_reason)."""
    try:
        r = urllib.request.Request(url, method="POST",
                                   headers={"Content-Type": "application/json", "User-Agent": "VoidOS"},
                                   data=json.dumps(payload).encode())
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            return True, resp.status, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:400].decode("utf-8", "replace")
        except Exception:
            body = ""
        return False, e.code, body
    except Exception as e:
        return False, 0, _network_reason(e, what)


def send_discord(webhook_url: str, title: str, body: str) -> tuple[bool, str]:
    if not webhook_url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                                   "https://ptb.discord.com/api/webhooks/", "https://canary.discord.com/api/webhooks/")):
        return False, "That doesn't look like a Discord webhook URL (it should start with https://discord.com/api/webhooks/)."
    ok, status, info = _http_post_json(webhook_url, {
        "embeds": [{"title": title[:250], "description": body[:DISCORD_DESC_MAX], "color": 0x1FB6FF}]
    }, "Discord")
    if ok:
        return True, "Delivered to Discord"
    if status in (401, 404):
        return False, "Discord says this webhook doesn't exist — it was probably deleted. Create a new one and paste it here."
    if status == 429:
        return False, "Discord is rate-limiting this webhook — too many messages; it will recover on its own."
    if status == 400:
        return False, f"Discord rejected the message ({info[:120] or 'bad request'})."
    return False, info or f"Discord returned HTTP {status}."


_NTFY_TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NTFY_MAX = 3500


# The emoji at the start of a title becomes ntfy's own icon (a "tag"), so it is shown once, in the app's style,
# instead of being repeated inside the title text. 🚨 is urgent (priority 5), ⚠️ high (4), most others normal.
_NTFY_TAGS = {"\U0001F6A8": ("rotating_light", 5), "\u26a0": ("warning", 4), "\u2705": ("white_check_mark", 3),
              "\U0001F513": ("unlock", 3), "\U0001F464": ("bust_in_silhouette", 3), "\U0001F5D1": ("wastebasket", 3),
              "\U0001F511": ("key", 3), "\U0001F6E1": ("shield", 3), "\u2699": ("gear", 3), "\u21a9": ("leftwards_arrow_with_hook", 3),
              "\U0001F9E9": ("jigsaw", 3), "\U0001F310": ("globe_with_meridians", 3), "\U0001F389": ("tada", 3),
              "\U0001F451": ("crown", 3), "\U0001F512": ("lock", 3), "\U0001F4BE": ("floppy_disk", 4),
              "\U0001F9F9": ("broom", 3), "\U0001F4BD": ("minidisc", 4), "\U0001F4E6": ("package", 4), "\u2601": ("cloud", 4)}


def _ntfy_title(title: str) -> tuple:
    t = title.strip()
    if t[:1] in _NTFY_TAGS:
        tag, prio = _NTFY_TAGS[t[:1]]
        return (t[1:].lstrip("\ufe0f\u200d ").strip() or t), tag, prio
    return t, "bell", 3


def _ntfy_when(iso: str) -> str:
    """'2026-10-17T13:41:09+00:00' -> 'Oct 17, 1:41 PM' in the server's own time zone."""
    try:
        from datetime import datetime
        d = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
        d = d.astimezone() if d.tzinfo else d
        return f"{d.strftime('%b')} {d.day}, {d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"
    except Exception:
        return iso.strip()


_NTFY_ICONS = {"user": "\U0001F464", "ip": "\U0001F310", "target": "\U0001F3AF", "detail": "\U0001F4DD"}


def _ntfy_body(body: str) -> str:
    """The shared 'User: … IP: … Time: …' text, reshaped for a phone notification: one short line per fact, led by
    an icon, no repeated labels, and a friendly time. Text that isn't in that shape (a test message) passes through."""
    keep, server, when, known = [], "", "", False
    for ln in body.splitlines():
        k, sep, v = ln.partition(": ")
        kl, v = k.strip().lower(), v.strip()
        if sep and kl in _NTFY_ICONS:
            known = True
            if v and v not in ("\u2014", "-"):
                keep.append(f"{_NTFY_ICONS[kl]} {v}")
        elif sep and kl == "time":
            known, when = True, _ntfy_when(v)
        elif sep and kl == "server":
            known, server = True, v
        elif ln.startswith("(+"):
            keep.append("\U0001F501 " + ln.strip("() "))
        elif ln.strip():
            keep.append(ln.strip())
    if not known:
        return body
    foot = " \u00b7 ".join(x for x in (when, server) if x)
    if foot:
        keep.append("\U0001F552 " + foot)
    return "\n".join(keep)


def send_ntfy(server: str, topic: str, token: str, title: str, body: str) -> tuple[bool, str]:
    """Publish to ntfy (https://ntfy.sh, or a server of your own) as JSON, which keeps emoji and any language
    intact in the title. The phone app shows it as a push notification for everyone subscribed to the topic."""
    server = (server or "https://ntfy.sh").strip().rstrip("/")
    topic, token = (topic or "").strip(), (token or "").strip()
    pu = urllib.parse.urlparse(server)
    if pu.scheme not in ("http", "https") or not pu.hostname or pu.username or pu.password or pu.query or pu.fragment:
        return False, "The ntfy server address should look like https://ntfy.sh (no login, query or extra parts)."
    if not _NTFY_TOPIC_RE.fullmatch(topic):
        return False, "The topic can only contain letters, digits, underscores and hyphens (up to 64 characters)."
    nice_title, tag, priority = _ntfy_title(title)
    payload = {"topic": topic, "title": nice_title[:250], "message": _ntfy_body(body)[:_NTFY_MAX],
               "priority": priority, "tags": [tag]}
    headers = {"Content-Type": "application/json", "User-Agent": "VoidOS"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        r = urllib.request.Request(server + "/", method="POST", headers=headers, data=json.dumps(payload).encode())
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            return True, "Delivered to ntfy"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, ("ntfy refused it: that topic needs a login. Add an access token (ntfy account \u2192 "
                           "Access tokens) or use a topic that isn't protected.")
        if e.code == 404:
            return False, "That ntfy address doesn't answer like an ntfy server. Check the server address."
        if e.code == 429:
            return False, "ntfy is rate-limiting this server (too many messages); it will recover on its own."
        if e.code == 413:
            return False, "ntfy says the message was too large."
        return False, f"ntfy returned HTTP {e.code}."
    except Exception as e:
        return False, _network_reason(e, "the ntfy server")


def send_telegram(bot_token: str, chat_id: str, title: str, body: str) -> tuple[bool, str]:
    # HTML mode with escaping. The previous parse_mode="Markdown" made
    # Telegram reject (HTTP 400 "can't parse entities") any message whose
    # text contained an unpaired _ * ` or [ — e.g. a username like john_doe
    # or a file called my_report.pdf — so those notifications were lost.
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", bot_token or ""):
        return False, "That doesn't look like a Telegram bot token (it should look like 123456789:ABC...). Copy it again from @BotFather."
    if not re.fullmatch(r"-?\d{3,20}|@[A-Za-z0-9_]{4,64}", str(chat_id or "")):
        return False, "That doesn't look like a Telegram chat ID (a number such as 123456789 or -1001234567890, or @channelname)."
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"[:TELEGRAM_MAX]
    ok, status, info = _http_post_json(url, {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                             "disable_web_page_preview": True}, "Telegram")
    if ok:
        return True, "Delivered to Telegram"
    low = info.lower()
    from_telegram = '"ok":false' in low.replace(" ", "")  # a real Bot API reply, not a proxy/firewall page
    if status and not from_telegram:
        return False, (f"Got HTTP {status} from something between this server and Telegram (a proxy or "
                       f"firewall?) — check that the server can reach api.telegram.org.")
    if status == 401 or "unauthorized" in low:
        return False, "Telegram rejected the bot token — copy it again from @BotFather."
    if status == 404:
        return False, "Telegram doesn't recognise that bot token — copy it again from @BotFather."
    if "chat not found" in low:
        return False, ("Telegram can't find that chat — send your bot a message first (or add it to the group), "
                       "and double-check the chat ID.")
    if "bot was blocked" in low or status == 403:
        return False, "The bot isn't allowed to message that chat — it was blocked or removed. Unblock/re-add it."
    if status == 429:
        return False, "Telegram is rate-limiting the bot — it will recover on its own."
    return False, info[:200] if status == 0 else f"Telegram returned HTTP {status}: {info[:150]}"


def send_email(smtp_host: str, smtp_port: int, username: str, password: str,
               use_tls: bool, from_addr: str, to_addr: str, title: str, body: str) -> tuple[bool, str]:
    try:
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = title
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="voidos.local")
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=TIMEOUT) as s:
                if username:
                    s.login(username, password)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=TIMEOUT) as s:
                if use_tls:
                    s.starttls(context=ssl.create_default_context())
                if username:
                    s.login(username, password)
                s.sendmail(from_addr, [to_addr], msg.as_string())
        return True, f"Sent to {to_addr}"
    except smtplib.SMTPAuthenticationError:
        return False, ("The mail server rejected the username/password. Gmail, Outlook and Yahoo need an "
                       "app password here, not your normal password.")
    except smtplib.SMTPNotSupportedError:
        return False, "The mail server doesn't support STARTTLS on this port — try port 465 (SSL) or turn TLS off."
    except smtplib.SMTPRecipientsRefused:
        return False, f"The mail server refused the recipient address ({to_addr})."
    except smtplib.SMTPSenderRefused:
        return False, ("The mail server refused the From address — most providers require it to match the "
                       "account you sign in with.")
    except smtplib.SMTPServerDisconnected:
        return False, ("The mail server closed the connection — usually a TLS/port mismatch "
                       "(465 = SSL, 587 = STARTTLS).")
    except smtplib.SMTPException as e:
        return False, f"The mail server returned an error: {str(e)[:150]}"
    except Exception as e:
        return False, _network_reason(e, f"the mail server {smtp_host}:{smtp_port}")
