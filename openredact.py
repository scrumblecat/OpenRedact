import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import textwrap
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
MESSAGE_PATH = re.compile(r"messages/c?([0-9]+)/messages\.(csv|json)", re.IGNORECASE)


class OpenRedactError(Exception):
    pass


class Console:
    COLORS = {
        "accent": "\033[1;36m",
        "muted": "\033[90m",
        "success": "\033[32m",
        "warning": "\033[33m",
        "danger": "\033[1;31m",
    }

    def __init__(self, no_color=False, stream=None):
        self.no_color = no_color
        self.stream = stream
        self.reviewed = False
        self.progress = None
        self.terminal = (stream or sys.stdout).isatty() and os.environ.get("TERM") != "dumb"
        ansi = self.terminal
        if ansi and os.name == "nt":
            ansi = self.enable_windows_color(-12 if stream is sys.stderr else -11)
        self.ansi = ansi
        self.color = not no_color and ansi and "NO_COLOR" not in os.environ

    @staticmethod
    def enable_windows_color(handle_id=-11):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetStdHandle(handle_id)
        mode = wintypes.DWORD()
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))

    def paint(self, text, tone="accent"):
        return f"{self.COLORS[tone]}{text}\033[0m" if self.color else text

    def write(self, text="", tone=None):
        text = self.paint(text, tone) if tone else text
        if self.stream is None:
            print(text)
        else:
            print(text, file=self.stream)

    def step(self, number, total, title, hint=None):
        self.write()
        self.write(f"[{number}/{total}] {title}", "accent")
        if hint:
            self.write(f"  {hint}", "muted")

    def splash(self, dry_run=False):
        try:
            art = (APP_DIR / "ASCII.txt").read_text(encoding="utf-8-sig").rstrip("\r\n")
        except (OSError, UnicodeError):
            art = "OpenRedact"
        self.write(art or "OpenRedact", "accent")
        self.write()
        self.write("  Find and delete your Discord messages using a word blacklist.")
        self.write("  Choose your export and blacklist, review the matches, then confirm.", "muted")
        if dry_run:
            self.write("  DRY RUN / Local preview only. No token or Discord connection needed.", "warning")
        self.write("  " + "-" * 62, "muted")


class Keyboard:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def __enter__(self):
        self.active = self.enabled and sys.stdin.isatty()
        self.settings = None
        if self.active and os.name != "nt":
            import termios
            import tty

            self.settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        return self

    def read(self):
        if not self.active:
            return ""
        if os.name == "nt":
            import msvcrt

            if not msvcrt.kbhit():
                return ""
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return {"H": "up", "P": "down", "I": "page_up", "Q": "page_down", "G": "home", "O": "end"}.get(msvcrt.getwch(), "")
            return key.lower()
        import select

        if not select.select([sys.stdin], [], [], 0)[0]:
            return ""
        key = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
        if key == "\x1b":
            while select.select([sys.stdin], [], [], 0.01)[0] and len(key) < 8:
                key += os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
                if key[-1] in "~ABFH":
                    break
        return {"\x1b[A": "up", "\x1b[B": "down", "\x1b[5~": "page_up", "\x1b[6~": "page_down", "\x1b[H": "home", "\x1b[F": "end", "\x1bOH": "home", "\x1bOF": "end", "\x1b[1~": "home", "\x1b[4~": "end"}.get(key, key.lower())

    def __exit__(self, *args):
        if self.settings is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.settings)


class DeletionProgress:
    FRAMES = ("|", "/", "-", "\\")

    def __init__(self, total, console, verbose=False):
        self.total = total
        self.console = console
        self.verbose = verbose
        self.deleted = self.absent = self.failed = 0
        self.records = []
        self.emitted = 0
        self.frame = 0
        self.line_width = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.live = False
        self.details_screen = False
        self.details_payload = None
        self.finished = False
        self.offset = 0
        self.wrapped = []
        self.wrapped_count = 0
        self.wrapped_width = 0

    @property
    def remaining(self):
        return self.total - self.deleted - self.absent - self.failed

    def __enter__(self):
        self.keyboard = Keyboard(self.console.terminal)
        self.keyboard.__enter__()
        self.live = self.console.terminal and self.console.ansi and self.keyboard.active
        self.console.progress = self
        if self.live:
            self.console.write("  Deleting messages. V: expand log, S: collapse log, Ctrl+C: stop.", "muted")
            self.thread = threading.Thread(target=self.animate, daemon=True)
            self.thread.start()
        return self

    def clear_line(self):
        if self.live:
            stream = self.console.stream or sys.stdout
            stream.write("\r" + " " * self.line_width + "\r")
            self.line_width = 0

    def show_records(self):
        if self.emitted < len(self.records):
            self.clear_line()
            for text, tone in self.records[self.emitted:]:
                self.console.write(text, tone)
            self.emitted = len(self.records)

    def close_details(self):
        if self.details_screen:
            stream = self.console.stream or sys.stdout
            stream.write("\033[?25h\033[?1049l")
            stream.flush()
            self.details_screen = False
            self.details_payload = None

    def layout(self):
        size = shutil.get_terminal_size((80, 24))
        width = max(1, size.columns - 1)
        capacity = max(1, size.lines - 5)
        reflowed = self.wrapped_width != width
        if reflowed:
            self.wrapped = []
            self.wrapped_count = 0
            self.wrapped_width = width
        previous = len(self.wrapped)
        for text, tone in self.records[self.wrapped_count:]:
            self.wrapped.extend((line, tone) for line in (textwrap.wrap(text, width=width, drop_whitespace=False) or [""]))
        self.wrapped_count = len(self.records)
        if self.offset and not reflowed:
            self.offset += len(self.wrapped) - previous
        self.offset = min(self.offset, max(0, len(self.wrapped) - capacity))
        return width, max(1, size.lines), capacity

    def handle_key(self, key):
        if key == "v":
            self.verbose = True
        elif key == "s":
            self.verbose = False
        elif self.verbose:
            _, _, capacity = self.layout()
            if key in ("up", "k"):
                self.offset += 1
            elif key in ("down", "j"):
                self.offset -= 1
            elif key in ("page_up", "b"):
                self.offset += capacity
            elif key in ("page_down", "f"):
                self.offset -= capacity
            elif key == "home":
                self.offset = max(0, len(self.wrapped) - capacity)
            elif key == "end":
                self.offset = 0
            self.offset = max(0, min(self.offset, max(0, len(self.wrapped) - capacity)))

    def draw_details(self):
        stream = self.console.stream or sys.stdout
        if not self.details_screen:
            stream.write("\033[?1049h\033[?25l")
            self.details_screen = True
        width, height, capacity = self.layout()
        end = len(self.wrapped) - self.offset
        start = max(0, end - capacity)
        activity = "Finished" if self.finished else self.FRAMES[self.frame % len(self.FRAMES)]
        lines = [
            (f"  {activity} OpenRedact / Detailed log", "accent"),
            (f"  {self.deleted} deleted, {self.absent} absent, {self.failed} failed, {self.remaining} remaining", "accent"),
            ("  S: collapse | Up/Down, PgUp/PgDn: scroll | Home/End: first/latest", "muted"),
            (f"  Log lines {start + 1 if end else 0}-{end} of {len(self.wrapped)} / " + ("browsing history" if self.offset else "following latest") + (" / Enter: finish" if self.finished else ""), "muted"),
            ("", None),
        ] + self.wrapped[start:end]
        output = []
        for text, tone in lines[:height]:
            text = text[:width]
            output.append("\033[2K" + (self.console.paint(text, tone) if tone else text))
        payload = "\033[H" + "\r\n".join(output) + "\033[J"
        if payload != self.details_payload:
            stream.write(payload)
            stream.flush()
            self.details_payload = payload

    def draw(self):
        if self.verbose:
            self.draw_details()
            self.frame += 1
            return
        self.close_details()
        if self.finished:
            return
        mode = "details" if self.verbose else "summary"
        segments = [
            (f"  {self.FRAMES[self.frame % len(self.FRAMES)]} [{mode}] ", "accent"),
            (str(self.deleted), "success"), (" deleted, ", None),
            (str(self.absent), "muted"), (" absent, ", None),
            (str(self.failed), "danger" if self.failed else "muted"), (" failed, ", None),
            (str(self.remaining), "accent"), (" remaining", None),
        ]
        width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        rendered = []
        used = 0
        for text, tone in segments:
            text = text[:max(0, width - used)]
            rendered.append(self.console.paint(text, tone) if tone else text)
            used += len(text)
        stream = self.console.stream or sys.stdout
        stream.write("\r" + "".join(rendered) + " " * max(0, min(self.line_width, width) - used) + "\r")
        stream.flush()
        self.line_width = used
        self.frame += 1

    def animate(self):
        while not self.stop.is_set():
            key = self.keyboard.read()
            if key == "\x03":
                import _thread

                _thread.interrupt_main()
                return
            with self.lock:
                self.handle_key(key)
                self.draw()
            self.stop.wait(0.1)

    def notice(self, text):
        with self.lock:
            self.records.append((f"  {text}", "warning"))
            if not self.live and self.verbose:
                self.show_records()

    def record(self, index, message, result):
        with self.lock:
            if result == "deleted":
                self.deleted += 1
                tone = "success"
            elif result == "already absent":
                self.absent += 1
                tone = "muted"
            else:
                self.failed += 1
                tone = "danger"
            self.records.append((f"  [{index}/{self.total}] {message.channel_id}/{message.message_id}: {result}", tone))
            if not self.live and self.verbose:
                self.show_records()

    def review(self):
        self.console.reviewed = True
        self.verbose = False
        self.console.write("  V: expand log, S: collapse log, Enter: finish.", "muted")
        try:
            while True:
                key = self.keyboard.read()
                if key in ("\r", "\n", "\x1b"):
                    return
                if key == "\x03":
                    raise KeyboardInterrupt
                self.handle_key(key)
                self.draw()
                if not key:
                    time.sleep(0.1)
        finally:
            self.close_details()

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.stop.set()
            if self.thread is not None:
                self.thread.join()
            if self.verbose and not self.live:
                self.show_records()
            self.close_details()
            self.finished = True
            self.clear_line()
            self.console.write(
                f"  Results: {self.deleted} deleted, {self.absent} absent, {self.failed} failed, {self.remaining} remaining.",
                "warning" if self.failed or self.remaining else "success",
            )
            if self.live and exc_type is None:
                self.review()
        finally:
            self.close_details()
            self.keyboard.__exit__(exc_type, exc, traceback)


@dataclass(frozen=True)
class Message:
    channel_id: str
    message_id: str
    content: str


@dataclass
class Scan:
    messages: list[Message]
    total: int
    account_id: str | None


def snowflake(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OpenRedactError("Invalid message, channel, or account ID in the package.")
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) == 0:
        raise OpenRedactError("Invalid message, channel, or account ID in the package.")
    return value


def load_blacklist(path):
    try:
        words = path.read_text(encoding="utf-8-sig").split(",")
    except UnicodeError as exc:
        raise OpenRedactError("The blacklist must be a UTF-8 text file.") from exc
    words = tuple(dict.fromkeys(word.strip().casefold() for word in words if word.strip()))
    if not words:
        raise OpenRedactError("The blacklist must contain at least one nonempty comma-separated string.")
    return words


def matches(content, words):
    folded = content.casefold()
    return any(word in folded for word in words)


def scan_package(path, words):
    flagged = {}
    seen = set()
    files = 0
    account_id = None
    try:
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                name = entry.filename.replace("\\", "/")
                if name.lower() == "account/user.json":
                    with archive.open(entry) as stream:
                        account = json.load(io.TextIOWrapper(stream, encoding="utf-8-sig"))
                    if not isinstance(account, dict):
                        raise OpenRedactError("Invalid account/user.json in the package.")
                    account_id = snowflake(account.get("id"))
                match = MESSAGE_PATH.fullmatch(name)
                if match is None:
                    continue
                files += 1
                channel_id = snowflake(match[1])
                with archive.open(entry) as stream:
                    text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
                    if match[2].lower() == "csv":
                        rows = csv.DictReader(text, strict=True)
                        if not {"ID", "Contents"}.issubset(rows.fieldnames or []):
                            raise OpenRedactError(f"Missing ID or Contents columns in {name}.")
                    else:
                        rows = json.load(text)
                        if not isinstance(rows, list):
                            raise OpenRedactError(f"Expected a JSON message array in {name}.")
                    for number, row in enumerate(rows, start=1):
                        if not isinstance(row, dict) or "ID" not in row or "Contents" not in row:
                            raise OpenRedactError(f"Missing ID or Contents at record {number} in {name}.")
                        message_id = snowflake(row["ID"])
                        content = row["Contents"]
                        if content is None:
                            content = ""
                        if not isinstance(content, str):
                            raise OpenRedactError(f"Invalid Contents at record {number} in {name}.")
                        key = (channel_id, message_id)
                        seen.add(key)
                        if matches(content, words):
                            flagged[key] = Message(channel_id, message_id, content)
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, csv.Error, RuntimeError, EOFError) as exc:
        raise OpenRedactError("Cannot read the package. Check that it is a valid, unencrypted Discord data ZIP.") from exc
    if not files:
        raise OpenRedactError("No Messages/c<channel ID>/messages.csv or messages.json files were found.")
    return Scan(list(flagged.values()), len(seen), account_id)


def load_config(path):
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise OpenRedactError("config.json is invalid. Fix it or remove it before continuing.") from exc
    if not isinstance(config, dict) or any(not isinstance(config.get(field, ""), str) for field in ("token", "package_path", "blacklist_path")):
        raise OpenRedactError("config.json must be an object with string token, package_path, and blacklist_path fields when present.")
    return config


def save_config(path, config):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(config, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_input(prompt, console, tone="accent"):
    while True:
        value = input(console.paint(prompt, tone))
        if "\x16" not in value and value.strip() != "^V":
            return value
        console.write("  Ctrl+V is not supported by this console. Right-click to paste, then press Enter.", "warning")


def prompt_path(label, console=None, saved=""):
    console = console or Console(no_color=True)
    if saved:
        console.write(f"  Saved path: {saved}", "muted")
    while True:
        suffix = " [Enter to reuse saved path]" if saved else ""
        raw = read_input(f"  {label}{suffix} > ", console).strip().strip('"').strip("'") or saved
        if raw:
            path = Path(os.path.expandvars(raw)).expanduser()
            if path.is_file():
                return path.resolve()
        if saved and raw == saved:
            console.write("  The saved file no longer exists. Enter a new path.", "warning")
            saved = ""
            continue
        console.write("  Enter the path to an existing file.", "warning")


def prompt_token(saved, console=None):
    console = console or Console(no_color=True)
    while True:
        suffix = " [Enter to use saved token]" if saved else ""
        token = read_input(f"  Discord token{suffix} > ", console).strip() or saved
        if token and not any(character.isspace() or ord(character) < 32 or ord(character) > 126 for character in token):
            return token
        console.write("  Enter a nonempty token without quotes or whitespace.", "warning")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DiscordClient:
    def __init__(self, token, opener=None, sleep=time.sleep, output=print):
        self.token = token
        self.opener = opener or request.build_opener(NoRedirect())
        self.sleep = sleep
        self.output = output
        self.pending_delay = 0.0

    @staticmethod
    def seconds(value):
        try:
            value = float(value)
        except (ValueError, TypeError, OverflowError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    @staticmethod
    def decode(body):
        try:
            data = json.loads(body)
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeError):
            return {}

    def send(self, method, route):
        for attempt in range(6):
            if self.pending_delay:
                self.sleep(self.pending_delay)
                self.pending_delay = 0.0
            req = request.Request(
                f"https://discord.com/api/v10{route}",
                headers={"Authorization": self.token, "User-Agent": "OpenRedact/1.0", "Accept": "application/json"},
                method=method,
            )
            try:
                with self.opener.open(req, timeout=30) as response:
                    status, headers, body = response.status, response.headers, response.read()
            except error.HTTPError as exc:
                with exc:
                    status, headers, body = exc.code, exc.headers, exc.read()
            except (error.URLError, OSError, TimeoutError):
                if attempt == 5:
                    raise OpenRedactError("Network request failed after retries. Run OpenRedact again to retry remaining messages.") from None
                self.pending_delay = 2 ** attempt
                self.output("Network request failed; retrying.")
                continue
            data = self.decode(body)
            if status == 401:
                raise OpenRedactError("Discord rejected the token (HTTP 401). Restart and enter a valid token.")
            if status == 429:
                if attempt == 5:
                    raise OpenRedactError("Discord is still rate limiting requests. Stopping; try again later.")
                waits = [self.seconds(data.get("retry_after")), self.seconds(headers.get("Retry-After"))]
                waits = [wait for wait in waits if wait is not None]
                self.pending_delay = max(waits, default=2 ** attempt) + 0.25
                self.output(f"Rate limited; waiting {self.pending_delay:.2f}s before retrying.")
                continue
            if 500 <= status <= 599:
                if attempt == 5:
                    raise OpenRedactError("Discord is unavailable after retries. Stopping; try again later.")
                self.pending_delay = 2 ** attempt
                self.output("Discord server error; retrying.")
                continue
            if headers.get("X-RateLimit-Remaining") == "0":
                reset = self.seconds(headers.get("X-RateLimit-Reset-After"))
                if reset is not None:
                    self.pending_delay = reset + 0.25
            return status, data
        raise OpenRedactError("Request could not be completed.")

    def current_user(self):
        status, data = self.send("GET", "/users/@me")
        if status != 200 or "id" not in data:
            raise OpenRedactError(f"Could not verify the Discord account (HTTP {status}).")
        return snowflake(data["id"])

    def delete(self, message):
        status, data = self.send("DELETE", f"/channels/{message.channel_id}/messages/{message.message_id}")
        if status == 204:
            return "deleted"
        if status == 404 and data.get("code") == 10008:
            return "already absent"
        code = data.get("code")
        suffix = f", code {code}" if isinstance(code, int) else ""
        reasons = {
            10003: "channel unavailable (Unknown Channel)",
            50001: "no access to this channel (Missing Access)",
            50013: "missing permission to delete this message",
            50021: "this system message cannot be deleted",
            50083: "the thread is archived",
        }
        reason = reasons.get(code) if isinstance(code, int) else None
        explanation = f": {reason}" if reason else ""
        return f"failed (HTTP {status}{suffix}){explanation}"


def delete_messages(client, messages, delay=1.0, console=None, verbose=False):
    console = console or Console(no_color=True)
    original_output = client.output
    try:
        with DeletionProgress(len(messages), console, verbose=verbose) as progress:
            client.output = progress.notice
            for index, message in enumerate(messages, start=1):
                if index > 1:
                    client.pending_delay = max(client.pending_delay, delay)
                result = client.delete(message)
                progress.record(index, message, result)
    finally:
        client.output = original_output
    return 1 if progress.failed else 0


def run(argv=None, console=None):
    parser = argparse.ArgumentParser(description="OpenRedact: delete exported Discord messages containing blacklist substrings.")
    parser.add_argument("--dry-run", action="store_true", help="scan and preview locally without asking for a token or contacting Discord")
    parser.add_argument("--no-color", action="store_true", help="disable terminal colors")
    parser.add_argument("--verbose", action="store_true", help="show each deletion attempt and retry notice")
    parser.add_argument("--keep-open", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    console = console or Console(no_color=args.no_color)
    total_steps = 2 if args.dry_run else 4
    console.splash(args.dry_run)
    config = load_config(CONFIG_PATH)
    console.step(1, total_steps, "Select your Discord data package", "Paste the path to package.zip with right-click. Quoted paths are welcome.")
    package_path = prompt_path("Path to package.zip", console, saved=config.get("package_path", ""))
    config["package_path"] = str(package_path)
    if not args.dry_run:
        save_config(CONFIG_PATH, config)
    console.step(2, total_steps, "Choose your blacklist", "Use comma-separated strings, for example: fuck, shit. Matching ignores case.")
    blacklist_path = prompt_path("Path to blacklist.txt", console, saved=config.get("blacklist_path", ""))
    config["blacklist_path"] = str(blacklist_path)
    if not args.dry_run:
        save_config(CONFIG_PATH, config)
    words = load_blacklist(blacklist_path)
    console.write()
    console.write("  Scanning package...", "muted")
    scan = scan_package(package_path, words)
    console.write()
    message_count = len(scan.messages)
    channel_count = len({message.channel_id for message in scan.messages})
    message_label = "message" if message_count == 1 else "messages"
    channel_label = "channel" if channel_count == 1 else "channels"
    console.write(
        f"  Found {console.paint(f'{message_count:,}', 'accent')} {message_label} "
        f"in {console.paint(f'{channel_count:,}', 'accent')} {channel_label}."
    )
    if args.dry_run:
        console.write()
        console.write("  Dry run complete. No messages deleted.", "success")
        return 0
    console.step(3, total_steps, "Connect your Discord account", "Paste your token with right-click. Your input is visible.")
    token = prompt_token(config.get("token", ""), console)
    config["token"] = token
    save_config(CONFIG_PATH, config)
    console.write("  Token saved to config.json successfully!", "success")
    if not scan.messages:
        console.write("  No matching messages to delete.", "success")
        return 0
    console.step(4, total_steps, "Review and confirm")
    if not scan.account_id:
        console.write("  Package has no account/user.json; account ownership cannot be cross-checked.", "warning")
    console.write(f"  {len(scan.messages)} messages will be permanently deleted. This cannot be undone.", "danger")
    confirmation = read_input("  Type DELETE to begin > ", console, "warning")
    if confirmation.strip() != "DELETE":
        console.write("  Cancelled. No messages deleted.", "warning")
        return 0
    console.write("  Verifying your account...", "muted")
    client = DiscordClient(token, output=lambda text: console.write(f"  {text}", "warning"))
    account_id = client.current_user()
    if scan.account_id and account_id != scan.account_id:
        raise OpenRedactError("The token belongs to a different account than this package. No messages deleted.")
    return delete_messages(client, scan.messages, console=console, verbose=args.verbose)


def main():
    console = Console(no_color="--no-color" in sys.argv[1:])
    try:
        status = run(console=console)
    except (KeyboardInterrupt, EOFError):
        console.write()
        console.write("  Stopped. No further deletions will be requested.", "warning")
        status = 130
    except (OpenRedactError, OSError) as exc:
        sys.stdout.flush()
        Console(no_color=console.no_color, stream=sys.stderr).write(f"  Error: {exc}", "danger")
        status = 1
    except SystemExit as exc:
        status = exc.code
    if console.progress is not None and console.progress.live and not console.reviewed:
        with Keyboard() as keyboard:
            console.progress.keyboard = keyboard
            try:
                console.progress.review()
            except (KeyboardInterrupt, EOFError):
                console.write()
    if "--keep-open" in sys.argv[1:] and not console.reviewed and sys.stdin.isatty():
        try:
            input(console.paint("  Press Enter to close > ", "muted"))
        except (KeyboardInterrupt, EOFError):
            console.write()
    return status


if __name__ == "__main__":
    sys.exit(main())
