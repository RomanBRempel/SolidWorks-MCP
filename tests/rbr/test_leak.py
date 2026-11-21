"""leak: private strings are found before they reach git, and never printed.

Synthetic strings only. What the scanner forbids is put together from pieces here, so this
file passes its own check whatever the path allowlist says.
"""

import os
import shutil
import subprocess
import sys
import types

import pytest

from solidworks_mcp.rbr import leak

BS = chr(92)
USERS = "Us" + "ers"
SECRET = "ACME" + "-SECRET"
SHORT = "Zq" + "x"
DENY = [SECRET, "Q:" + BS, SHORT]
MAIL = "jane.roe" + "@" + "corp-mail.io"
FOLDER = "\u041f\u0430\u043f\u043a\u0430"   # a Cyrillic folder name


def user_path(name="jsmith", sep=BS, drive="C:"):
    return sep.join([drive, USERS, name, "Documents", "part.txt"])


def kinds(text, denylist=()):
    return [f.kind for f in leak.scan_text(text, list(denylist))]


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path, monkeypatch):
    """The user's own denylist never takes part in a test."""
    monkeypatch.setenv("SWMCP_PRIVATE_DIR", str(tmp_path / "private"))
    monkeypatch.delenv(leak.ENV_DENYLIST, raising=False)


# --- generic classes ------------------------------------------------------------------------

@pytest.mark.parametrize("sep", [BS, BS + BS, "/"])
def test_user_profile_path_in_every_spelling(sep):
    assert kinds("opened " + user_path(sep=sep) + " today") == [leak.USER_PATH]
    assert kinds('{"path": "' + user_path(sep=sep) + '"}') == [leak.USER_PATH]


def test_user_profile_path_other_forms():
    assert kinds("cd /c/" + USERS + "/jsmith/work") == [leak.USER_PATH]
    assert kinds("d:" + BS + USERS.lower() + BS + "jsmith") == [leak.USER_PATH]
    assert kinds("the " + USERS + " folder and C:" + BS + "Temp" + BS + "x.txt") == []


@pytest.mark.parametrize("name", ["<user>", "USERNAME", "%USERNAME%", "Public", "Default", "you", "me", "name",
                                  "$env:USERNAME", "..."])
def test_user_profile_placeholders_are_not_findings(name):
    assert kinds("see " + user_path(name=name)) == []
    assert kinds("see " + user_path(name=name, sep="/")) == []


def test_cyrillic_path():
    assert kinds("saved to X:" + BS + FOLDER + BS + "part.sldprt") == [leak.CYRILLIC_PATH]
    assert kinds("saved to X:" + BS + BS + "work" + BS + BS + FOLDER + BS + BS + "a.txt") == [leak.CYRILLIC_PATH]
    assert kinds("saved to x:/work/" + FOLDER) == [leak.CYRILLIC_PATH]
    assert kinds("saved to X:" + BS + "work" + BS + "part.sldprt") == []
    assert kinds(FOLDER + " " + FOLDER + ": a note with no drive in it") == []


def test_email_and_its_allowlist():
    assert kinds("write to " + MAIL + ".") == [leak.EMAIL]
    assert kinds("<" + MAIL + ">") == [leak.EMAIL]
    for allowed in ("noreply" + "@" + "anthropic.com", "someone" + "@" + "example.com",
                    "a.b" + "@" + "example.org", "x" + "@" + "mail.example.net",
                    "1234+dev" + "@" + "users.noreply.github.com", "git" + "@" + "github.com"):
        assert kinds("Co-Authored-By: Some One <" + allowed + ">") == [], allowed
    assert kinds("clone git" + "@" + "github.com:owner/repo.git") == []
    assert kinds("other" + "@" + "anthropic.com") == [leak.EMAIL]


def test_what_only_looks_like_an_email():
    assert kinds("set D1" + "@" + "Sketch1" + "@" + "Part1.SLDPRT to 5") == []
    assert kinds("width" + "@" + "Sketch2" + "@" + "Bracket.Part") == []
    assert kinds("@mcp.tool() and uses: actions/checkout" + "@" + "v4 and x = R" + "@" + "v.T") == []


# --- the denylist ---------------------------------------------------------------------------

def test_denylist_long_entries_match_any_case_short_ones_exactly():
    assert kinds("the " + SECRET.lower() + " job", DENY) == [leak.DENYLIST]
    assert kinds("the " + SECRET.title() + " job", DENY) == [leak.DENYLIST]
    assert kinds("code " + SHORT, DENY) == [leak.DENYLIST]
    assert kinds("code " + SHORT.lower(), DENY) == []
    assert kinds("code " + SHORT.upper(), DENY) == []
    assert kinds("nothing here", DENY) == []


def test_denylist_separators_match_either_spelling():
    for sep in (BS, BS + BS, "/"):
        assert kinds("on Q:" + sep + "jobs", DENY) == [leak.DENYLIST], sep
    assert kinds("on q:" + BS + "jobs", DENY) == []     # three characters: exact case
    assert kinds("the Q: drive", DENY) == []


def test_a_finding_names_the_entry_by_position_only():
    (finding,) = leak.scan_text("a\nb " + SHORT + " c\n", DENY)
    assert (finding.line, finding.kind, finding.entry) == (2, leak.DENYLIST, 2)
    assert SHORT not in repr(finding)


def test_one_finding_per_line_kind_and_entry():
    found = leak.scan_text(SECRET + " and " + SECRET + " and " + SHORT + " " + MAIL, DENY)
    assert [(f.kind, f.entry) for f in found] == [(leak.DENYLIST, 0), (leak.DENYLIST, 2), (leak.EMAIL, None)]


def test_unusable_entries_are_skipped_and_keep_the_positions():
    (finding,) = leak.scan_text("x " + SECRET, ["", "  ", BS, None, SECRET])
    assert finding.entry == 4


def test_load_denylist_from_private_settings_and_environment(tmp_path, monkeypatch):
    assert leak.load_denylist() == []
    private = tmp_path / "private"
    private.mkdir()
    (private / "cfd_private.json").write_text('{"leak_denylist": ["one-entry", 7, "Ab"]}', encoding="utf-8")
    monkeypatch.setenv(leak.ENV_DENYLIST, "from-env|second\n third \n\n")
    assert leak.load_denylist() == ["one-entry", "", "Ab", "from-env", "second", "third"]


def test_broken_private_settings_are_an_error_not_an_empty_list(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "cfd_private.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        leak.load_denylist()
    assert leak.main(["text", str(private / "cfd_private.json")]) == 2


# --- diffs ----------------------------------------------------------------------------------

def _diff():
    return "\n".join([
        "diff --git a/docs/notes.md b/docs/notes.md",
        "index 1111111..2222222 100644",
        "--- a/docs/notes.md",
        "+++ b/docs/notes.md",
        "@@ -2,0 +3,2 @@ heading",
        "+plain line",
        "+uses " + SECRET.lower(),
        "@@ -10 +12 @@",
        "-old " + SECRET + " goes away",
        "+++ " + SHORT + " on a line that begins with plus signs",
        "diff --git a/src/app.py b/src/app.py",
        "--- a/src/app.py",
        "+++ b/src/app.py",
        "@@ -5,2 +5,3 @@",
        " context " + SECRET,
        "+mail " + MAIL,
        " more context",
        "\\ No newline at end of file",
        ""])


def test_added_lines_with_file_and_new_line_number():
    found = [(f.path, f.line, f.kind, f.entry) for f in leak.scan_added_lines(_diff(), DENY)]
    assert found == [("docs/notes.md", 4, leak.DENYLIST, 0),
                     ("docs/notes.md", 12, leak.DENYLIST, 2),
                     ("src/app.py", 6, leak.EMAIL, None)]


def test_added_lines_yields_only_additions():
    assert list(leak.added_lines(_diff().split("\n"))) == [
        ("docs/notes.md", 3, "plain line"),
        ("docs/notes.md", 4, "uses " + SECRET.lower()),
        ("docs/notes.md", 12, "++ " + SHORT + " on a line that begins with plus signs"),
        ("src/app.py", 6, "mail " + MAIL)]


def test_a_new_or_renamed_file_name_is_scanned_and_masked():
    name = "jobs/" + SECRET.lower() + "/plan.md"
    diff = "\n".join(["diff --git a/" + name + " b/" + name, "new file mode 100644", "--- /dev/null",
                      "+++ b/" + name, "@@ -0,0 +1,2 @@", "+clean", "+also " + SHORT,
                      "diff --git a/old.txt b/" + SECRET + ".txt", "similarity index 100%",
                      "rename from old.txt", "rename to " + SECRET + ".txt"])
    found = leak.scan_added_lines(diff, DENY)
    assert [(f.path, f.line, f.entry) for f in found] == [
        ("jobs/<private>/plan.md", 0, 0), ("jobs/<private>/plan.md", 2, 2), ("<private>.txt", 0, 0)]
    assert SECRET.lower() not in leak.report(found, show=True).lower()


def test_a_deleted_file_adds_nothing():
    diff = "\n".join(["diff --git a/gone.txt b/gone.txt", "deleted file mode 100644", "--- a/gone.txt",
                      "+++ /dev/null", "@@ -1,2 +0,0 @@", "-" + SECRET, "-" + MAIL])
    assert leak.scan_added_lines(diff, DENY) == []


def test_combined_diff_of_a_merge():
    diff = "\n".join(["diff --cc notes.txt", "index 1111111,2222222..3333333", "--- a/notes.txt",
                      "+++ b/notes.txt", "@@@ -1,2 -1,2 +1,3 @@@", "  kept " + SECRET,
                      "- theirs " + SECRET, " -ours " + SECRET, "++merged " + SECRET, " +from one side " + SHORT])
    assert [(f.path, f.line, f.entry) for f in leak.scan_added_lines(diff, DENY)] == [
        ("notes.txt", 2, 0), ("notes.txt", 3, 2)]


def test_generic_patterns_skip_the_scanner_own_files_but_the_denylist_does_not():
    for path in leak.GENERIC_EXEMPT:
        diff = "\n".join(["diff --git a/" + path + " b/" + path, "--- a/" + path, "+++ b/" + path,
                          "@@ -0,0 +1,3 @@", "+" + user_path(), "+" + MAIL, "+" + SECRET])
        assert [(f.line, f.kind) for f in leak.scan_added_lines(diff, DENY)] == [(3, leak.DENYLIST)]
    assert all(os.path.isfile(os.path.join(os.path.dirname(__file__), "..", "..", p)) for p in leak.GENERIC_EXEMPT)


# --- messages -------------------------------------------------------------------------------

def test_message_skips_comment_lines_and_what_follows_the_scissors():
    message = "\n".join(["Fix the thing", "", "about " + SECRET, "# On branch " + SECRET,
                         "# ------------------------ >8 ------------------------",
                         "diff --git a/x b/x", "+" + SECRET, MAIL])
    assert [(f.path, f.line, f.kind) for f in leak.scan_message(message, DENY)] == [
        (leak.MESSAGE, 3, leak.DENYLIST)]
    assert [f.line for f in leak.scan_message("ok\n# a note\n" + MAIL, DENY)] == [3]


# --- sanitize and report --------------------------------------------------------------------

def test_sanitize_replaces_every_match():
    text = ("job " + SECRET + " at " + user_path() + ", mail " + MAIL + ", disk Q:" + BS + "x, "
            + "X:" + BS + FOLDER + BS + "a.txt, twice " + SECRET.upper())
    clean = leak.sanitize(text, DENY)
    assert clean == "job <private> at <path>, mail <email>, disk <private>x, <path>, twice <private>"
    assert leak.sanitize(clean, DENY) == clean
    assert leak.sanitize("nothing private", DENY) == "nothing private"
    assert leak.sanitize(None, DENY) is None


def test_sanitize_overlapping_matches_leave_nothing_behind():
    inside = user_path(name="jsmith" + BS + SECRET)
    assert leak.sanitize("at " + inside + " now", DENY) == "at <private> now"


def test_sanitize_uses_the_users_denylist_by_default(monkeypatch):
    monkeypatch.setenv(leak.ENV_DENYLIST, SECRET)
    assert leak.sanitize("the " + SECRET.lower() + " job") == "the <private> job"
    assert leak.sanitize_obj({"note": SECRET}) == {"note": "<private>"}


def test_sanitize_obj_walks_dicts_lists_and_keys():
    obj = {"path": user_path(), "items": [SECRET, 3, {"mail": MAIL, "ok": True}], SECRET: 1, SECRET.lower(): 2,
           "n": None, "pair": ("a", SHORT)}
    assert leak.sanitize_obj(obj, DENY) == {
        "path": "<path>", "items": ["<private>", 3, {"mail": "<email>", "ok": True}], "<private>": 1,
        "<private> (2)": 2, "n": None, "pair": ["a", "<private>"]}
    assert obj["items"][0] == SECRET   # a copy: the original is untouched


def test_report_never_holds_the_matched_text():
    text = "\n".join(["first", "job " + SECRET + " for " + MAIL, "at " + user_path() + " and X:" + BS + FOLDER,
                      "code " + SHORT + " " * 200 + SECRET])
    found = leak.scan_text(text, DENY, path="notes.txt", commit="0123456789abcdef")
    plain, shown = leak.report(found), leak.report(found, show=True)
    assert plain.split("\n")[0] == "0123456789 notes.txt:2 denylist"
    assert {line.split(" ", 2)[2] for line in plain.split("\n")} == {"denylist", "email", "user-path", "cyrillic-path"}
    for out in (plain, shown):
        for secret in (SECRET, SECRET.lower(), MAIL, "jsmith", FOLDER, SHORT):
            assert secret not in out and secret.lower() not in out.lower()
    assert "<private>" in shown and "#0" in shown and "#2" in shown
    assert "<private>" not in plain and "#" not in plain
    assert leak.report([]) == ""


# --- command line ---------------------------------------------------------------------------

def test_cli_text_and_message_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(leak.ENV_DENYLIST, "|".join([SECRET, SHORT]))
    clean = tmp_path / "clean.txt"
    clean.write_text("# heading\nnothing private\n", encoding="utf-8")
    dirty = tmp_path / "dirty.txt"
    dirty.write_text("subject\n\n# " + SECRET + "\nbody " + SHORT + "\n", encoding="utf-8")
    assert leak.main(["text", str(clean)]) == 0
    assert leak.main(["message", str(clean)]) == 0
    assert capsys.readouterr().out == ""
    assert leak.main(["message", str(dirty)]) == 1
    assert capsys.readouterr().out.strip() == "<message>:4 denylist"
    assert leak.main(["text", str(dirty)]) == 1
    captured = capsys.readouterr()
    assert captured.out.split() == ["<text>:3", "denylist", "<text>:4", "denylist"]
    assert leak.main(["--show", "text", str(dirty)]) == 1
    captured2 = capsys.readouterr()
    for out in (captured.out, captured.err, captured2.out, captured2.err):
        assert SECRET not in out and SHORT not in out


def test_cli_usage_errors_exit_with_2(tmp_path, capsys):
    for argv in ([], ["nonsense"], ["message"], ["range"], ["range", "--all"]):
        with pytest.raises(SystemExit) as exc:
            leak.main(argv)
        assert exc.value.code == 2
    assert leak.main(["text", str(tmp_path / "missing.txt")]) == 2
    with pytest.raises(leak.LeakError):
        leak.scan_commits("--all", [])       # a revision, never an option for git
    capsys.readouterr()


def test_cli_sanitize_prints_the_cleaned_text(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(leak.ENV_DENYLIST, SECRET)
    source = tmp_path / "log.txt"
    source.write_text("job " + SECRET + "\nby " + MAIL + "\n", encoding="utf-8")
    assert leak.main(["sanitize", str(source)]) == 0
    assert capsys.readouterr().out.replace("\r\n", "\n") == "job <private>\nby <email>\n"


def test_runs_by_file_path_without_the_package(tmp_path):
    """The hooks and CI start the file itself: no site-packages, so no pywin32 and no package."""
    dirty = tmp_path / "dirty.txt"
    dirty.write_text("clean\njob " + SECRET + "\n", encoding="utf-8")
    env = {**os.environ, leak.ENV_DENYLIST: SECRET, "SWMCP_PRIVATE_DIR": str(tmp_path / "private")}
    private = tmp_path / "private"
    private.mkdir()
    (private / "cfd_private.json").write_text('{"leak_denylist": ["' + SHORT + '"]}', encoding="utf-8")
    run = subprocess.run([sys.executable, "-I", "-S", leak.__file__, "text", str(dirty)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert (run.returncode, run.stdout.strip()) == (1, "<text>:2 denylist"), run.stderr
    assert "2 denylist entries" in run.stderr      # private.py was loaded next to the file
    assert SECRET not in run.stdout + run.stderr
    dirty.write_text("clean\n", encoding="utf-8")
    run = subprocess.run([sys.executable, "-I", "-S", leak.__file__, "text", str(dirty)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert (run.returncode, run.stdout) == (0, "")


# --- against a real repository --------------------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository with three commits: a clean one, one adding a denylisted line and an
    address, one with a denylisted message."""
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        done = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test" + "@" + "example.com",
                               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false",
                               "-c", "core.hooksPath=" + str(tmp_path / "no-hooks"), *args],
                              cwd=root, capture_output=True, text=True, timeout=60, check=True)
        return done.stdout.strip()

    def write(name, text):
        (root / name).write_text(text, encoding="utf-8", newline="\n")

    git("init", "-q")
    write("notes.txt", "one\ntwo\nthree\n")
    git("add", "notes.txt")
    git("commit", "-q", "-m", "Start")
    first = git("rev-parse", "HEAD")
    write("notes.txt", "one\njob " + SECRET.lower() + "\ntwo\nthree\nask " + MAIL + "\n")
    git("add", "notes.txt")
    git("commit", "-q", "-m", "Add notes")
    write("other.txt", "fine\n")
    git("add", "other.txt")
    git("commit", "-q", "-m", "Work on " + SHORT + "\n\n# kept by git: " + SECRET)
    monkeypatch.chdir(root)
    monkeypatch.setenv(leak.ENV_DENYLIST, "|".join(DENY))
    return types.SimpleNamespace(path=root, git=git, write=write, first=first)


@needs_git
def test_cli_staged(repo, capsys):
    assert leak.main(["staged"]) == 0
    repo.write("notes.txt", "one\njob " + SECRET.lower() + "\ntwo\nnew " + SHORT + "\nthree\nask " + MAIL
               + "\nlast " + user_path() + "\n")
    assert leak.main(["staged"]) == 0            # not staged yet
    repo.git("add", "notes.txt")
    capsys.readouterr()
    assert leak.main(["staged"]) == 1
    captured = capsys.readouterr()
    assert captured.out.split("\n")[:2] == ["notes.txt:4 denylist", "notes.txt:7 user-path"]
    assert SHORT not in captured.out + captured.err and "jsmith" not in captured.out + captured.err


@needs_git
def test_cli_range(repo, capsys):
    head = repo.git("rev-parse", "HEAD")
    middle = repo.git("rev-parse", "HEAD~1")
    assert leak.main(["range", repo.first + ".." + repo.first]) == 0
    capsys.readouterr()
    assert leak.main(["range", repo.first + "..HEAD"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip().split("\n") == [
        head[:10] + " <message>:1 denylist", head[:10] + " <message>:3 denylist",
        middle[:10] + " notes.txt:2 denylist", middle[:10] + " notes.txt:5 email"]
    assert leak.main(["range", "HEAD~1..HEAD"]) == 1
    assert "notes.txt" not in capsys.readouterr().out
    assert leak.main(["range", "no-such-branch..HEAD"]) == 2
    for out in (captured.out, captured.err):
        assert SECRET.lower() not in out.lower() and SHORT not in out and MAIL not in out


@needs_git
def test_cli_history_prints_totals_only(repo, tmp_path, capsys):
    assert leak.main(["history", "--details", str(repo.path / "details.txt")]) == 2    # inside the repository
    assert not (repo.path / "details.txt").exists()
    capsys.readouterr()
    details = tmp_path / "details.txt"
    assert leak.main(["history", "--details", str(details)]) == 1
    out = capsys.readouterr().out
    assert out.strip().split("\n") == [
        "commits scanned: 3", "findings: 4", "  denylist: 3", "  email: 1", "commits affected: 2",
        "files affected: 1", "commit messages affected: 1", "details: written to the file given"]
    listed = details.read_text(encoding="utf-8")
    assert "notes.txt:2 denylist #0" in listed and "<message>:1 denylist #2" in listed
    for text in (out, listed):
        assert SECRET.lower() not in text.lower() and SHORT not in text and MAIL not in text
    assert leak.main(["history", repo.first]) == 0
    assert capsys.readouterr().out.split("\n")[:2] == ["commits scanned: 1", "findings: 0"]
