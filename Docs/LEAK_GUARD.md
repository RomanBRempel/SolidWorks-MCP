# Leak guard

Nothing private to your own work may reach this repository: not in a commit diff, not in
a commit message, not in a PR title or description. `src/solidworks_mcp/rbr/leak.py`
looks for two things:

- **your denylist** - strings you name yourself (project names, machine names, drive
  roots, customer names). An entry of 5 or more characters matches whatever its case, a
  shorter one exactly; `\` and `/` in an entry match either separator, single or doubled.
- **generic classes**, private whoever you are:
  - `user-path` - a Windows path holding a user profile name (`C:\Users\<name>\...`, also
    written with `\\`, `/` or as `/c/Users/<name>/...`). Placeholders pass: `<user>`,
    `USERNAME`, `%USERNAME%`, `Public`, `Default`, `you`, `me`, `name`.
  - `cyrillic-path` - a drive path with a Cyrillic folder or file name in it.
  - `email` - an e-mail address. These pass: the Claude co-author address, `git@github.com`,
    anything at `example.com` / `.org` / `.net` and at `users.noreply.github.com`.

A scan prints `file:line kind` and nothing else: never the text that matched and never a
denylist entry, so its output is safe in a CI log. Line 0 means the file's own name.

## Where the denylist lives

In the private settings file, outside the source tree:

    %LOCALAPPDATA%\solidworks-mcp\private\cfd_private.json

(the folder is overridden by the environment variable `SWMCP_PRIVATE_DIR`), under the key
`leak_denylist` - a JSON list of strings:

    {"leak_denylist": ["<project name>", "<machine name>", "<drive root>"]}

The other keys of that file are described in `src/solidworks_mcp/rbr/private.py`. The
environment variable `SWMCP_LEAK_DENYLIST` adds entries (one per line, or separated by
`|`); that is how CI gets the list. A settings file that cannot be read stops the check
with an error - it never counts as an empty list.

## Enable the hooks (once per clone)

    git config core.hooksPath .githooks

- `pre-commit` scans the lines the staged changes add, and the names of new or renamed files.
- `commit-msg` scans the message (git's own `#` comment lines are skipped).

The hooks need a Python 3.10+; they try `$SWMCP_PYTHON` (the full path of an interpreter),
then `py -3`, `python3`, `uv run --no-project python`. A bare `python` is never called: on
Windows it may be the Store stub, which hangs. With no interpreter, a scan that does not
finish or a broken settings file, the commit is refused.

On Linux and macOS the hook files must be executable. They are stored that way when they
were added with `git add --chmod=+x .githooks/pre-commit .githooks/commit-msg`.

`git commit --no-verify` skips the hooks, and a branch that has no `.githooks` folder has
none; the CI check is the second line.

## CI

`.github/workflows/leak-check.yml` runs on every push and pull request: the commits of the
PR (base..head) or of the push, each commit's stored message in full and the lines its
diff adds, then the PR title and description. It always applies the generic classes; add
the repository secret `LEAK_DENYLIST` (one entry per line) to apply your denylist too.
Secrets are not given to PRs from forks: those get the generic classes only.

## By hand

The scanner is stdlib only and runs by file path (no pywin32, no SolidWorks):

    python src/solidworks_mcp/rbr/leak.py staged
    python src/solidworks_mcp/rbr/leak.py message <file>
    python src/solidworks_mcp/rbr/leak.py text <file>            (any text, e.g. a PR description)
    python src/solidworks_mcp/rbr/leak.py range origin/rbr..HEAD  (before a push)
    python src/solidworks_mcp/rbr/leak.py sanitize <file>         (prints the text with matches replaced)

Exit code 0 = clean, 1 = something found, 2 = the check could not be made.
`--show` before the command adds, for your own terminal only, the position of the denylist
entry and the line with every match masked.

### The whole history

    python src/solidworks_mcp/rbr/leak.py history [<rev>] --details <file outside the repository>

scans every commit reachable from `<rev>` (default `HEAD`) and prints totals only: commits
scanned, findings per kind, how many commits, files and commit messages are affected. The
list itself (`commit file:line kind #index`) goes to the `--details` file, which must lie
outside the repository. What is already pushed stays public until the history is rewritten
and the old objects are gone from every copy - treat such a string as disclosed.

## Texts that leave the machine

`leak.sanitize(text)` and `leak.sanitize_obj(obj)` replace every match with `<private>`,
`<path>` or `<email>`. Use them on a log or a tool answer before it goes into a report, an
issue or a PR.

## Limits

- Lines that a diff adds are scanned, not binary files and not what a commit removes.
- A path with spaces is recognised up to the first space.
- The two files that hold the generic patterns and their examples (`leak.py`, its test)
  are exempt from the generic classes - never from the denylist.
- Branch and tag names are not scanned; they are public once pushed.
