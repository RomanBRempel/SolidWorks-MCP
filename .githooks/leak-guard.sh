#!/bin/sh
# Leak guard, shared by the pre-commit and commit-msg hooks (see Docs/LEAK_GUARD.md).
#   leak-guard.sh staged            the lines the staged changes add
#   leak-guard.sh message <file>    the commit message
# Runs the scanner by file path (stdlib only; the package itself needs pywin32) and prints
# `file:line kind` - never the text that matched. Anything but a clean scan refuses the
# commit: no interpreter, a scan that did not finish and a broken settings file included.

mode=$1
file=$2
scanner=src/solidworks_mcp/rbr/leak.py

root=$(git rev-parse --show-toplevel 2>/dev/null) && cd "$root" || {
    echo "leak guard: the repository root was not found; commit refused." >&2
    exit 1
}
if [ ! -f "$scanner" ]; then
    echo "leak guard: $scanner is missing; commit refused." >&2
    exit 1
fi
case "$file" in
    "" | /* | ?:*) ;;
    *) file="$root/$file" ;;
esac

# A bare `python` / `python3` on Windows may be the Store stub, which waits forever: every
# probe gets a time limit, and without GNU timeout the names that may be stubs are skipped.
limit=""
if timeout --version >/dev/null 2>&1; then
    limit="timeout"
fi
windows=""
case "$(uname -s 2>/dev/null)" in
    MINGW* | MSYS* | CYGWIN*) windows=1 ;;
esac

works() {  # does this command start a Python 3.10 or newer?
    ${limit:+$limit 30} "$@" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >/dev/null 2>&1
}

scan() {  # "$@" = the interpreter command
    if [ "$mode" = message ]; then
        ${limit:+$limit 300} "$@" "$scanner" message "$file"
    else
        ${limit:+$limit 300} "$@" "$scanner" staged
    fi
}

for attempt in 1 2 3 4; do
    case $attempt in
        1) [ -n "$SWMCP_PYTHON" ] || continue
           set -- "$SWMCP_PYTHON" ;;
        2) set -- py -3 ;;
        3) [ -z "$windows" ] || [ -n "$limit" ] || continue
           set -- python3 ;;
        4) set -- uv run --no-project python ;;
    esac
    works "$@" || continue
    scan "$@"
    status=$?
    if [ "$status" -eq 0 ]; then
        exit 0
    fi
    if [ "$status" -eq 1 ]; then
        echo "leak guard: commit refused - private text in the places listed above (file:line kind)." >&2
        echo "leak guard: to see the lines with the matches masked, run the scanner with --show in your own terminal." >&2
    else
        echo "leak guard: the scan did not finish (exit $status); commit refused." >&2
    fi
    exit 1
done

echo "leak guard: no Python 3.10+ found (tried \$SWMCP_PYTHON, py -3, python3, uv run); commit refused." >&2
echo "leak guard: set SWMCP_PYTHON to the full path of a Python interpreter." >&2
exit 1
