"""
Tools available to the coding agent. Every path-taking tool is sandboxed to
the active workspace directory — nothing outside it can be read or written.
"""
import difflib
import fnmatch
import os
import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build", ".next", ".cache"}
MAX_READ_CHARS = 12000
MAX_SEARCH_MATCHES = 150
MAX_SEARCH_LINE_LEN = 200


class UnsafePathError(Exception):
    pass


def safe_path(workspace: str, rel_path: str) -> Path:
    """Resolve rel_path against workspace and guarantee it can't escape it."""
    workspace_root = Path(workspace).resolve()
    candidate = (workspace_root / rel_path).resolve()
    if candidate != workspace_root and workspace_root not in candidate.parents:
        raise UnsafePathError(f"Path '{rel_path}' resolves outside the workspace and was blocked.")
    return candidate


def rel(workspace: str, abs_path: Path) -> str:
    try:
        return str(abs_path.relative_to(Path(workspace).resolve()))
    except ValueError:
        return str(abs_path)


# ---------------------------------------------------------------------------
# Read-only tools (auto-executed, no approval needed)
# ---------------------------------------------------------------------------

def list_directory(workspace: str, path: str = ".") -> str:
    target = safe_path(workspace, path)
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_dir():
        return f"Error: '{path}' is not a directory."

    entries = []
    for item in sorted(target.iterdir()):
        if item.name in SKIP_DIRS or item.name.startswith("."):
            continue
        if item.is_dir():
            entries.append(f"{item.name}/")
        else:
            size = item.stat().st_size
            entries.append(f"{item.name} ({size} bytes)")

    if not entries:
        return f"'{path}' is empty."
    return f"Contents of '{path}':\n" + "\n".join(entries)


def read_file(workspace: str, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    target = safe_path(workspace, path)
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if target.is_dir():
        return f"Error: '{path}' is a directory, not a file."

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading '{path}': {e}"

    lines = text.splitlines()
    total = len(lines)

    if start_line or end_line:
        s = max((start_line or 1) - 1, 0)
        e = end_line or total
        selected = lines[s:e]
        header = f"'{path}' lines {s + 1}-{min(e, total)} of {total}:\n"
    else:
        selected = lines
        header = f"'{path}' ({total} lines):\n"

    body = "\n".join(f"{i + 1}\t{l}" for i, l in enumerate(selected, start=(start_line or 1) - 1 if start_line else 0))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + f"\n... [truncated, {total} total lines — request a line range to see more]"

    return header + body


def search_code(workspace: str, pattern: str, path: str = ".") -> str:
    target = safe_path(workspace, path)
    if not target.exists():
        return f"Error: '{path}' does not exist."

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Error: invalid regex pattern — {e}"

    matches = []
    search_root = target if target.is_dir() else target.parent
    files_iter = [target] if target.is_file() else None

    def iter_files():
        if files_iter:
            yield from files_iter
            return
        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                yield Path(root) / fname

    for fpath in iter_files():
        if len(matches) >= MAX_SEARCH_MATCHES:
            break
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    if regex.search(line):
                        snippet = line.strip()[:MAX_SEARCH_LINE_LEN]
                        matches.append(f"{rel(workspace, fpath)}:{lineno}: {snippet}")
                        if len(matches) >= MAX_SEARCH_MATCHES:
                            break
        except (UnicodeDecodeError, IsADirectoryError, PermissionError):
            continue

    if not matches:
        return f"No matches for /{pattern}/ under '{path}'."
    suffix = "\n... [more matches truncated]" if len(matches) >= MAX_SEARCH_MATCHES else ""
    return f"{len(matches)} match(es) for /{pattern}/:\n" + "\n".join(matches) + suffix


# ---------------------------------------------------------------------------
# Write tools — these are validated + diffed here, but NOT applied until the
# server layer gets explicit user approval.
# ---------------------------------------------------------------------------

def propose_write_file(workspace: str, path: str, content: str) -> dict:
    """Validate + diff a write_file call. Does not touch disk."""
    try:
        target = safe_path(workspace, path)
    except UnsafePathError as e:
        return {"ok": False, "error": str(e)}

    old_text = ""
    existed = target.exists()
    if existed:
        if target.is_dir():
            return {"ok": False, "error": f"'{path}' is a directory, cannot write a file there."}
        try:
            old_text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": f"Could not read existing file to diff: {e}"}

    diff = "\n".join(difflib.unified_diff(
        old_text.splitlines(), content.splitlines(),
        fromfile=path + (" (existing)" if existed else " (new file)"),
        tofile=path, lineterm=""
    )) or "(no textual changes)"

    return {"ok": True, "diff": diff, "target": str(target), "existed": existed}


def apply_write_file(target_path: str, content: str) -> str:
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to '{target.name}'."


def propose_edit_file(workspace: str, path: str, old_string: str, new_string: str) -> dict:
    """Validate + diff an edit_file (find-and-replace) call. Does not touch disk."""
    try:
        target = safe_path(workspace, path)
    except UnsafePathError as e:
        return {"ok": False, "error": str(e)}

    if not target.exists():
        return {"ok": False, "error": f"'{path}' does not exist. Use write_file to create a new file."}

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"Could not read '{path}': {e}"}

    count = text.count(old_string)
    if count == 0:
        return {"ok": False, "error": f"old_string was not found in '{path}'. Re-read the file to get exact text."}
    if count > 1:
        return {"ok": False, "error": f"old_string appears {count} times in '{path}' — it must be unique. Include more surrounding context."}

    new_text = text.replace(old_string, new_string, 1)
    diff = "\n".join(difflib.unified_diff(
        text.splitlines(), new_text.splitlines(),
        fromfile=path, tofile=path, lineterm=""
    )) or "(no textual changes)"

    return {"ok": True, "diff": diff, "target": str(target), "new_text": new_text}


def apply_edit_file(target_path: str, new_text: str) -> str:
    target = Path(target_path)
    target.write_text(new_text, encoding="utf-8")
    return f"Edit applied to '{target.name}'."


# ---------------------------------------------------------------------------
# OpenAI-style tool schemas
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories at a path within the workspace. Use this to explore the codebase structure before reading specific files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to the workspace root. Use '.' for the root."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents (with line numbers) from the workspace. Always read a file before editing it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace root."},
                    "start_line": {"type": "integer", "description": "Optional 1-indexed start line."},
                    "end_line": {"type": "integer", "description": "Optional 1-indexed end line (inclusive)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a regex pattern across files in the workspace (like grep). Use this to find where something is defined or used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for."},
                    "path": {"type": "string", "description": "Directory or file to search within. Defaults to the whole workspace.", "default": "."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or fully overwrite an existing one with new content. Requires user approval before it takes effect — you will get the result after the user approves or rejects it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace root."},
                    "content": {"type": "string", "description": "The full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact, unique block of text in an existing file with new text (like a find-and-replace). Requires user approval before it takes effect. old_string must match the file's current content exactly and appear only once — read the file first to get exact text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace root."},
                    "old_string": {"type": "string", "description": "The exact existing text to replace (must be unique in the file)."},
                    "new_string": {"type": "string", "description": "The text to replace it with."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
]

READ_ONLY_TOOLS = {"list_directory", "read_file", "search_code"}
WRITE_TOOLS = {"write_file", "edit_file"}

AGENT_SYSTEM_PROMPT = """You are an autonomous coding agent working inside a real codebase on the user's machine.

You have tools to explore and modify the workspace:
- list_directory, read_file, search_code — use freely to explore, no approval needed.
- write_file, edit_file — these require the user's explicit approval before they take effect. You will be told whether each was approved or rejected.

Guidelines:
- Always read a file before editing it, so your old_string matches exactly.
- Prefer edit_file for small changes; use write_file for new files or full rewrites.
- Make one focused tool call at a time and use the result before deciding the next step.
- If an edit is rejected, do not silently retry the same change — ask the user or try a different approach.
- Before finishing, briefly summarize what you explored and what changed (or didn't).
"""
