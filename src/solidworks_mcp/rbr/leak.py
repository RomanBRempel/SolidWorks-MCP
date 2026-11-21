"""Leak guard: nothing private to the user's own work may reach git or a report.

A commit diff, a commit message or a PR text is public the moment it is pushed, and a
tool answer pasted into a report travels just as far. Two things are looked for:

- the user's own denylist: `leak_denylist` of the private settings (private.py) plus the
  environment variable SWMCP_LEAK_DENYLIST (newline- or `|`-separated; how CI gets it from
  a secret). An entry of 5+ characters matches whatever its case, a shorter one exactly;
  `\\` and `/` in an entry match either separator, single or doubled;
- generic classes that are private whoever the user is (GENERIC): a Windows path holding a
  user profile name, a drive path with a Cyrillic name in it, an e-mail address.

What a scan answers is `file:line kind` - never the matched text, never a denylist entry:
its output lands in CI logs. A finding carries the entry's position in the list instead.

The same matching cleans texts that leave the machine: sanitize() / sanitize_obj() put
<private>, <path> and <email> in place of every match.

The module is stdlib only and never imports the package (whose __init__ needs pywin32):
the git hooks and CI run it by file path, `python src/solidworks_mcp/rbr/leak.py staged`;
`python -m solidworks_mcp.rbr.leak staged` works where the package imports.

Commands: staged | message <file> | text <file> | range <rev-range> |
history [<rev>] [--details <file outside the repository>] | sanitize <file>.
Exit code 0 = clean, 1 = something found, 2 = the check could not be made.
"""

import os
import sys

if not __package__ and sys.path and os.path.abspath(sys.path[0]) == os.path.dirname(os.path.abspath(__file__)):
    del sys.path[0]  # run by path: the neighbours here must not shadow modules the standard library imports

import argparse  # noqa: E402
import functools  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from dataclasses import dataclass  # noqa: E402

ENV_DENYLIST = "SWMCP_LEAK_DENYLIST"
CASE_INSENSITIVE_FROM = 5  # entries this long match whatever their case; shorter ones exactly

DENYLIST = "denylist"
USER_PATH, CYRILLIC_PATH, EMAIL = "user-path", "cyrillic-path", "email"
PLACEHOLDER = {DENYLIST: "<private>", USER_PATH: "<path>", CYRILLIC_PATH: "<path>", EMAIL: "<email>"}

MESSAGE = "<message>"   # the `file` of a finding in a commit message
TEXT = "<text>"

# Files that hold the generic patterns' own sources and examples. GENERIC is not applied to
# them; the denylist always is.
GENERIC_EXEMPT = ("src/solidworks_mcp/rbr/leak.py", "tests/rbr/test_leak.py")

_SEP = r"[\\/]+"
_SEG = r"""[^\\/\s"'`<>|*?:]"""  # a character of one path segment
_DRIVE = r"(?<![A-Za-z0-9])[A-Za-z]:"
_END = r"(?<![.,;)\]}])"         # a path does not end with the sentence's punctuation
_CYRILLIC = chr(0x400) + "-" + chr(0x52F)   # the Cyrillic and Cyrillic Supplement blocks

_USER_PATH = re.compile(
    r"(?:" + _DRIVE + r"|(?<![\w/.-])/[A-Za-z](?=/))" + _SEP + r"Users" + _SEP
    + r"""(?P<name>[^\\/\s"'`<>|*?:,;()\[\]{}]+)""" + r"(?:" + _SEP + _SEG + r"+)*" + r"[\\/]*" + _END,
    re.IGNORECASE)
_CYRILLIC_PATH = re.compile(
    _DRIVE + _SEP + r"""[^\s"'`<>|*?:]*[""" + _CYRILLIC + r"""][^\s"'`<>|*?:]*""" + _END)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"(?P<domain>(?:[A-Za-z0-9-]+\.)+(?P<tld>[A-Za-z]{2,24}))(?![A-Za-z0-9-])")

GENERIC = ((USER_PATH, _USER_PATH), (CYRILLIC_PATH, _CYRILLIC_PATH), (EMAIL, _EMAIL))

# A profile folder that names nobody: documentation placeholders and Windows' own folders.
PROFILE_PLACEHOLDERS = frozenset({"user", "username", "public", "default", "you", "me", "name"})
EMAIL_ALLOWED = frozenset({"noreply@anthropic.com", "git@github.com"})
EMAIL_DOMAINS_ALLOWED = ("example.com", "example.org", "example.net", "users.noreply.github.com")
# "D1@Sketch1@Part1.SLDPRT" is a full dimension name, not an address.
NOT_A_TLD = frozenset({"sldprt", "sldasm", "slddrw", "part", "assembly", "drawing"})


class LeakError(RuntimeError):
    """The check could not be made (git failed, a file is unreadable): never 'clean'."""


@dataclass(frozen=True)
class Finding:
    line: int                 # 1-based; 0 = the path of the file itself
    kind: str                 # "denylist" or the name of a GENERIC class
    entry: int | None = None  # position in the denylist - never the entry's text
    path: str = ""            # the file, with its own matches masked
    commit: str = ""
    excerpt: str = ""         # the line with every match masked; shown on request only


# --- what is forbidden ----------------------------------------------------------------------

def _private():
    """private.py, without importing the package when this file runs on its own."""
    if __package__:
        try:
            from . import private
            return private
        except ImportError:
            return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "private.py")
    if not os.path.isfile(path):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("_swmcp_private", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_denylist(value: str | None = None) -> list:
    value = os.environ.get(ENV_DENYLIST, "") if value is None else value
    return [part.strip() for part in re.split(r"[\r\n|]+", value) if part.strip()]


def load_denylist() -> list:
    """The private settings' leak_denylist, then the entries of SWMCP_LEAK_DENYLIST.

    The private entries keep their positions (an unusable one stays as ''), so the index in
    a finding is the index in the settings file. A settings file that cannot be read is an
    error, not an empty list."""
    private = _private()
    entries = private.get("leak_denylist", []) if private is not None else []
    if not isinstance(entries, (list, tuple)):
        entries = [entries]
    return [e if isinstance(e, str) else "" for e in entries] + env_denylist()


def _entry_pattern(entry: str):
    parts = re.split(_SEP, entry)
    if not any(part.strip() for part in parts):
        return None
    flags = re.IGNORECASE if len(entry) >= CASE_INSENSITIVE_FROM else 0
    return re.compile(_SEP.join(re.escape(part) for part in parts), flags)


@functools.lru_cache(maxsize=16)
def _compiled(entries: tuple) -> tuple:
    """(one pattern per entry or None, one pattern that tells whether any entry matches)."""
    patterns = tuple(_entry_pattern(entry) for entry in entries)
    sources = [f"(?i:{p.pattern})" if p.flags & re.IGNORECASE else f"(?:{p.pattern})" for p in patterns if p]
    return patterns, (re.compile("|".join(sources)) if sources else None)


def _real_profile(match) -> bool:
    name = match.group("name").rstrip(".").lower()
    return bool(name) and name[0] not in "%$" and name not in PROFILE_PLACEHOLDERS


def _real_address(match) -> bool:
    address, domain = match.group(0).lower(), match.group("domain").lower()
    if address in EMAIL_ALLOWED or match.group("tld").lower() in NOT_A_TLD:
        return False
    return not any(domain == allowed or domain.endswith("." + allowed) for allowed in EMAIL_DOMAINS_ALLOWED)


_ACCEPT = {USER_PATH: _real_profile, EMAIL: _real_address}


def _spans(text: str, denylist, generic: bool = True) -> list:
    """Every match in `text` as (start, end, kind, denylist index or None), by position."""
    found = []
    patterns, any_entry = _compiled(tuple(e if isinstance(e, str) else "" for e in denylist or ()))
    if any_entry is not None and any_entry.search(text):
        for index, pattern in enumerate(patterns):
            if pattern is not None:
                found.extend((m.start(), m.end(), DENYLIST, index) for m in pattern.finditer(text))
    if generic:
        for kind, pattern in GENERIC:
            accept = _ACCEPT.get(kind)
            found.extend((m.start(), m.end(), kind, None) for m in pattern.finditer(text)
                         if accept is None or accept(m))
    return sorted(found, key=lambda span: (span[0], -span[1]))


def _mask(text: str, spans: list):
    """`text` with every span replaced by its placeholder (overlapping spans by one), and
    where each replaced stretch went: [(start, end, offset in the result)]."""
    out, placed, pos, length = [], [], 0, 0
    merged = []
    for start, end, kind, _entry in spans:
        if merged and start < merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
            if kind == DENYLIST:
                merged[-1][2] = DENYLIST
        else:
            merged.append([start, end, kind])
    for start, end, kind in merged:
        out.append(text[pos:start])
        length += start - pos
        placed.append((start, end, length))
        out.append(PLACEHOLDER[kind])
        length += len(PLACEHOLDER[kind])
        pos = end
    out.append(text[pos:])
    return "".join(out), placed


def _excerpt(masked: str, placed: list, start: int, around: int = 60) -> str:
    at = next((offset for s, e, offset in placed if s <= start < e), 0)
    lo, hi = max(0, at - around), min(len(masked), at + around)
    return ("..." if lo else "") + masked[lo:hi].strip() + ("..." if hi < len(masked) else "")


# --- scanning -------------------------------------------------------------------------------

def _lines(text: str) -> list:
    """Split on LF alone: str.splitlines also breaks on form feeds and would shift the numbers."""
    return [line.rstrip("\r") for line in text.split("\n")]


def _scan_line(line: str, number: int, denylist, generic: bool = True, path: str = "", commit: str = "") -> list:
    spans = _spans(line, denylist, generic)
    if not spans:
        return []
    masked, placed = _mask(line, spans if generic else _spans(line, denylist, True))
    findings, seen = [], set()
    for start, _end, kind, entry in spans:
        if (kind, entry) not in seen:
            seen.add((kind, entry))
            findings.append(Finding(number, kind, entry, path, commit, _excerpt(masked, placed, start)))
    return findings


def scan_text(text: str, denylist, generic: bool = True, path: str = "", commit: str = "") -> list:
    """The findings in `text`, one per line, kind and denylist entry."""
    findings = []
    for number, line in enumerate(_lines(text), 1):
        findings.extend(_scan_line(line, number, denylist, generic, path, commit))
    return findings


def scan_message(text: str, denylist, path: str = MESSAGE, commit: str = "") -> list:
    """A commit message as the editor leaves it: lines starting with `#` are git's own
    comments, and everything from the scissors line down is dropped by git too."""
    findings = []
    for number, line in enumerate(_lines(text), 1):
        if line.startswith("#"):
            if ">8" in line and set(line.replace(">8", "")) <= set("# -"):
                break
            continue
        findings.extend(_scan_line(line, number, denylist, True, path, commit))
    return findings


_HUNK = re.compile(r"^(@@+) (.*?) @@+")
_COUNT = re.compile(r"^[-+]\d+(?:,(\d+))?$")


def _header_path(text: str):
    """The path of a `--- a/x` / `+++ b/x` / `rename to x` header; None for /dev/null."""
    text = text.split("\t")[0].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    if text == "/dev/null":
        return None
    return text[2:] if text[:2] in ("a/", "b/") else text


def added_lines(lines):
    """(path, line number in the new file, text) for every line a unified diff adds - `git
    diff`, `git show`, a merge's combined diff or plain `diff -u`. A file the diff creates
    or renames also gives (path, 0, path): its name is new text too."""
    path, number, parents, old_left, new_left, in_hunk = None, 0, 1, 0, 0, False
    for raw in lines:
        line = raw.rstrip("\r\n")
        if in_hunk and parents == 1 and old_left <= 0 and new_left <= 0:
            in_hunk = False
        if in_hunk and not (parents > 1 and line.startswith(("diff ", "@@"))):
            if line.startswith("\\"):   # "\ No newline at end of file"
                continue
            prefix = line[:parents]
            if "-" in prefix:
                old_left -= 1
                continue
            if "+" in prefix:
                if path is not None:
                    yield path, number, line[parents:]
            else:
                old_left -= 1
            number += 1
            new_left -= 1
            continue
        in_hunk = False
        hunk = _HUNK.match(line)
        if hunk:
            parents = len(hunk.group(1)) - 1
            ranges = hunk.group(2).split()
            new = _COUNT.match(ranges[-1]) if ranges else None
            old = _COUNT.match(ranges[0]) if ranges else None
            if new is None or old is None:
                continue
            number = int(ranges[-1][1:].split(",")[0])
            new_left = int(new.group(1)) if new.group(1) is not None else 1
            old_left = int(old.group(1)) if old.group(1) is not None else 1
            in_hunk = True
        elif line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            half = (len(rest) - 5) // 2   # "a/<path> b/<path>": the same path twice unless renamed
            same = len(rest) >= 7 and rest[:2] == "a/" and rest[2:2 + half] == rest[half + 5:]
            path = rest[2:2 + half] if same else None
        elif line.startswith(("diff --cc ", "diff --combined ")):
            path = line.split(" ", 2)[2]
        elif line.startswith(("rename to ", "copy to ")):
            path = _header_path(line.split(" to ", 1)[1])
            if path:
                yield path, 0, path
        elif line.startswith("new file mode") and path:
            yield path, 0, path
        elif line.startswith("+++ "):
            path = _header_path(line[4:])


def _scan_diff(lines, denylist, commit: str = ""):
    shown = {}
    for path, number, text in added_lines(lines):
        if path not in shown:
            shown[path] = sanitize(path, denylist)
        generic = path.replace("\\", "/") not in GENERIC_EXEMPT
        yield from _scan_line(text, number, denylist, generic, shown[path], commit)


def scan_added_lines(diff_text: str, denylist, commit: str = "") -> list:
    """The findings in the lines a unified diff adds, with the file and its new line number."""
    return list(_scan_diff(_lines(diff_text), denylist, commit))


# --- cleaning what goes into reports -----------------------------------------------------------

def sanitize(text, denylist=None):
    """`text` with every denylist and generic match replaced by <private>, <path>, <email>.
    denylist None = the user's own (load_denylist)."""
    if not isinstance(text, str) or not text:
        return text
    denylist = load_denylist() if denylist is None else denylist
    return _mask(text, _spans(text, denylist, True))[0]


def sanitize_obj(obj, denylist=None):
    """sanitize() for every string of a JSON-like value - dict keys included; a copy."""
    denylist = load_denylist() if denylist is None else denylist
    if isinstance(obj, str):
        return sanitize(obj, denylist)
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            clean = sanitize(key, denylist) if isinstance(key, str) else key
            base, n = clean, 1
            while clean in out:   # two keys that became one placeholder: keep both values
                n += 1
                clean = f"{base} ({n})"
            out[clean] = sanitize_obj(value, denylist)
        return out
    if isinstance(obj, (list, tuple)):
        return [sanitize_obj(item, denylist) for item in obj]
    return obj


def report(findings, show: bool = False) -> str:
    """One `file:line kind` per finding (with its commit when known) - never the matched
    text. show=True, for the user's own terminal, adds the denylist position and the line
    with its matches masked."""
    lines = []
    for f in findings:
        text = f"{f.path or TEXT}:{f.line} {f.kind}"
        if f.commit:
            text = f"{f.commit[:10]} {text}"
        if show:
            if f.entry is not None:
                text += f" #{f.entry}"
            if f.excerpt:
                text += f" | {f.excerpt}"
        lines.append(text)
    return "\n".join(lines)


# --- git ------------------------------------------------------------------------------------

_GIT = ("git", "-c", "core.quotepath=false")
_DIFF = ("--no-color", "--no-ext-diff", "--no-textconv", "-U0", "-M")
_COMMIT, _BODY_END = "\x01", "\x02"   # no diff line starts with these: each has a prefix character
_LOG_FORMAT = "--format=%x01%H%n%B%x02"


def _git_lines(*args):
    """The output lines of a git command, as UTF-8, split on LF alone. Git's own messages
    are not passed on: they may quote what the scan must not print."""
    try:
        proc = subprocess.Popen([*_GIT, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise LeakError(f"git cannot be started ({type(exc).__name__}).") from None
    with proc:
        for raw in proc.stdout:
            yield raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
    if proc.returncode:
        raise LeakError(f"git {args[0]} failed (exit {proc.returncode}): is this a repository, "
                        "and does the revision exist?")


def scan_staged(denylist) -> list:
    return list(_scan_diff(_git_lines("diff", "--cached", *_DIFF), denylist))


def scan_commits(revision: str, denylist):
    """(commits scanned, findings) over the commits `git log <revision>` lists: each one's
    stored message in full and the lines its diff adds (a merge: what the merge itself added)."""
    if not revision or revision.startswith("-"):
        raise LeakError("A revision or a range is expected, not an option.")
    count, findings = 0, []
    sha, message, diff, in_message = None, [], [], False

    def flush():
        if sha is not None:
            findings.extend(scan_text("\n".join(message), denylist, True, MESSAGE, sha))
            findings.extend(_scan_diff(diff, denylist, sha))

    for line in _git_lines("log", "-p", "--cc", *_DIFF, _LOG_FORMAT, revision, "--"):
        if line.startswith(_COMMIT):
            flush()
            sha, message, diff, in_message = line[1:].strip(), [], [], True
            count += 1
        elif in_message:
            if _BODY_END in line:
                message.append(line.split(_BODY_END)[0])
                in_message = False
            else:
                message.append(line)
        else:
            diff.append(line)
    flush()
    return count, findings


def _inside_repository(path: str) -> bool:
    try:
        top = next(iter(_git_lines("rev-parse", "--show-toplevel")), "")
    except LeakError:
        return False
    if not top:
        return False
    top, path = os.path.realpath(top), os.path.realpath(path)
    try:
        return os.path.commonpath([os.path.normcase(top), os.path.normcase(path)]) == os.path.normcase(top)
    except ValueError:   # another drive
        return False


# --- command line ---------------------------------------------------------------------------

def _read(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8-sig", "replace")
    except OSError as exc:
        raise LeakError(f"The file cannot be read ({type(exc).__name__}).") from None


def _totals(count: int, findings: list) -> str:
    kinds = {}
    for f in findings:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    lines = [f"commits scanned: {count}", f"findings: {len(findings)}"]
    lines += [f"  {kind}: {kinds[kind]}" for kind in sorted(kinds)]
    lines.append(f"commits affected: {len({f.commit for f in findings})}")
    lines.append(f"files affected: {len({f.path for f in findings if f.path != MESSAGE})}")
    lines.append(f"commit messages affected: {len({f.commit for f in findings if f.path == MESSAGE})}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leak", description="Leak guard: finds private strings before they reach git. "
                                 "Prints file:line kind, never the matched text.")
    parser.add_argument("--show", action="store_true",
                        help="local use only: add the denylist position and the line with its matches masked")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("staged", help="the lines the staged changes add (pre-commit)")
    sub.add_parser("message", help="a commit message file; lines starting with # are skipped (commit-msg)"
                   ).add_argument("file")
    sub.add_parser("text", help="any text file in full, e.g. a PR description; - reads stdin").add_argument("file")
    sub.add_parser("range", help="added lines and messages of every commit of a range, e.g. origin/rbr..HEAD"
                   ).add_argument("revisions")
    history = sub.add_parser("history", help="the whole history reachable from a revision; prints totals only")
    history.add_argument("revision", nargs="?", default="HEAD")
    history.add_argument("--details", metavar="FILE", help="write the list of findings to a file outside "
                                                           "the repository")
    sub.add_parser("sanitize", help="print a text file with every match replaced by a placeholder; - reads stdin"
                   ).add_argument("file")
    return parser


def _run(args) -> int:
    denylist = load_denylist()
    armed = sum(1 for entry in denylist if entry.strip())
    if args.command == "sanitize":
        sys.stdout.write(sanitize(_read(args.file), denylist))
        return 0
    print(f"leak guard: {armed} denylist entries, {len(GENERIC)} generic classes", file=sys.stderr)
    if args.command == "history":
        if args.details and _inside_repository(args.details):
            raise LeakError("--details must name a file outside the repository.")
        count, findings = scan_commits(args.revision, denylist)
        print(_totals(count, findings))
        if args.details:
            try:
                with open(args.details, "w", encoding="utf-8") as fh:
                    fh.write(f"# leak guard, history of {args.revision}: commit file:line kind #denylist-index\n")
                    for f in findings:   # the masked line only when asked for
                        fh.write(report([f if args.show else Finding(f.line, f.kind, f.entry, f.path, f.commit)],
                                        show=True) + "\n")
            except OSError as exc:
                raise LeakError(f"The details file cannot be written ({type(exc).__name__}).") from None
            print("details: written to the file given")
        return 1 if findings else 0
    if args.command == "staged":
        findings = scan_staged(denylist)
    elif args.command == "message":
        findings = scan_message(_read(args.file), denylist)
    elif args.command == "text":
        findings = scan_text(_read(args.file), denylist, True, TEXT)
    else:
        count, findings = scan_commits(args.revisions, denylist)
        print(f"leak guard: {count} commits scanned", file=sys.stderr)
    if findings:
        print(report(findings, show=args.show), flush=True)
        print(f"leak guard: {len(findings)} finding(s) - file:line kind above; the text itself is not shown.",
              file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = _parser().parse_args(argv)   # a usage error exits with 2
    try:
        return _run(args)
    except LeakError as exc:
        print(f"leak guard: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:   # PrivateError: the settings file is broken - the check was not made
        print(f"leak guard: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
