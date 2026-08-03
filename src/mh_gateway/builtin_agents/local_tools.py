from __future__ import annotations

import asyncio
import difflib
import json
import os
import shutil
import sys
from typing import Any, AsyncIterator
from uuid import uuid4

from mh_gateway.context import get_current_request, get_current_user_id

_IS_WINDOWS = sys.platform == "win32" or sys.platform == "cygwin"
_PLATFORM_NAME = "Windows" if _IS_WINDOWS else "macOS/Linux"
_SHELL_NAME = "PowerShell" if _IS_WINDOWS else "bash"
_CMD_SYNTAX_HINT = (
    "PowerShell syntax (e.g., echo, Get-ChildItem, for ($i=0; ...))"
    if _IS_WINDOWS
    else "bash syntax (e.g., echo, ls, for i in ...)"
)


async def bash_fn(
    command: str = "",
    workdir: str = "",
    timeout_ms: int = 60000,
) -> AsyncIterator[Any]:
    if not command:
        yield {"status": "error", "message": "command is required"}
        return

    _cmd = (
        "$OutputEncoding=[System.Text.UTF8Encoding]::new(); [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        + command
        if _IS_WINDOWS
        else command
    )
    shell_cmd = (
        ["powershell", "-NoProfile", "-Command", _cmd]
        if _IS_WINDOWS
        else ["bash", "-c", command]
    )

    yield {
        "status": "progress",
        "message": f"Executing command on {_PLATFORM_NAME} ({_SHELL_NAME}): {command[:200]}{'...' if len(command) > 200 else ''}",
    }

    cwd = workdir if workdir else None
    if cwd and not os.path.isdir(cwd):
        yield {
            "status": "error",
            "message": f"Working directory does not exist: {cwd}. Use local_file_operator with mkdir to create it first, or omit workdir to use the default directory.",
        }
        return

    timeout_s = timeout_ms / 1000.0

    try:
        process = await asyncio.create_subprocess_exec(
            *shell_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        queue: asyncio.Queue = asyncio.Queue()
        timed_out = False

        async def _pump(stream, bufs, label: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                bufs.append(text)
                await queue.put(("chunk", label, text.rstrip("\n\r"), "".join(bufs)))
            await queue.put(("done", label, None, "".join(bufs)))

        stdout_bufs: list[str] = []
        stderr_bufs: list[str] = []

        t_stdout = asyncio.create_task(_pump(process.stdout, stdout_bufs, "stdout"))
        t_stderr = asyncio.create_task(_pump(process.stderr, stderr_bufs, "stderr"))

        try:
            done_count = 0
            while done_count < 2:
                try:
                    typ, label, content, partial = await asyncio.wait_for(
                        queue.get(), timeout=timeout_s
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    break

                if typ == "done":
                    done_count += 1
                else:
                    yield {
                        "status": "progress",
                        "type": "stream",
                        "stream": label,
                        "content": content,
                        "partial_stdout": partial if label == "stdout" else None,
                        "partial_stderr": partial if label == "stderr" else None,
                    }
        finally:
            if process.returncode is None:
                process.terminate()
            t_stdout.cancel()
            t_stderr.cancel()
            await asyncio.gather(t_stdout, t_stderr, return_exceptions=True)
            await process.wait()

        stdout_str = "".join(stdout_bufs)
        stderr_str = "".join(stderr_bufs)
        returncode = process.returncode if not timed_out else -1

        line_count = stdout_str.count("\n")

        if timed_out:
            yield {
                "status": "error",
                "message": f"Command timed out after {timeout_ms}ms. Try increasing timeout_ms or simplifying the command.",
                "command": command[:500],
                "timed_out": True,
                "suggestion": "Increase timeout_ms or break the command into smaller steps",
                "stdout": stdout_str,
                "stderr": stderr_str,
            }
        elif returncode == 0:
            yield {
                "status": "ok",
                "message": f"Command completed successfully ({line_count} lines of output)"
                if line_count
                else "Command completed successfully (no output)",
                "stdout": stdout_str,
                "stderr": stderr_str,
                "exit_code": 0,
                "command": command[:500],
            }
        else:
            yield {
                "status": "error",
                "message": f"Command exited with code {returncode}. Check stderr for error details. You may need to fix the command syntax or install missing dependencies.",
                "stdout": stdout_str,
                "stderr": stderr_str,
                "exit_code": returncode,
                "command": command[:500],
                "suggestion": "Review stderr output and fix the command",
            }
    except FileNotFoundError:
        yield {
            "status": "error",
            "message": f"Shell not found: {_SHELL_NAME}. Is it installed and available in PATH?",
            "suggestion": "Verify the shell is installed, or use a different command syntax",
        }
    except OSError as e:
        yield {
            "status": "error",
            "message": f"Failed to execute command: {e}. Check that the command syntax is correct for {_SHELL_NAME}.",
            "suggestion": "Verify command syntax for the current platform",
        }


async def submit_feedback_fn(
    type: str = "",
    comment: str = "",
    category: str = "",
    target_type: str = "",
    target_id: str = "",
) -> AsyncIterator[Any]:
    if not type:
        yield {"status": "error", "message": "type is required (praise/blame)"}
        return

    request = get_current_request()
    if request is None:
        yield {"status": "error", "message": "No active request context"}
        return

    user_id = get_current_user_id()
    if not user_id:
        yield {"status": "error", "message": "No authenticated user"}
        return

    # Extract session_id from request path — chat endpoint uses {memory_id}
    path_params = request.scope.get("path_params", {})
    session_id = path_params.get("memory_id", "")
    if not session_id:
        yield {"status": "error", "message": "Could not determine session_id"}
        return

    adapters = request.app.state.adapters
    if adapters.feedback is None:
        yield {"status": "error", "message": "Feedback storage not configured"}
        return

    feedback_type = "thumbs_up" if type == "praise" else "thumbs_down"

    from mh_gateway.adapters import Feedback

    # resolve agent_name from the session
    agent_name = ""
    try:
        session = await adapters.sessions.get_session(session_id)
        if session:
            agent_name = getattr(session, "agent_name", "") or ""
    except Exception:
        pass

    feedback = Feedback(
        feedback_id=f"fb_{uuid4().hex[:12]}",
        session_id=session_id,
        target_type=target_type or "message",
        target_id=target_id or "",
        user_id=user_id,
        feedback_type=feedback_type,
        comment=comment or None,
        category=category or None,
        source="agent_tool",
        agent_name=agent_name,
        metadata={},
        created_at="",
    )
    saved = await adapters.feedback.save(feedback)
    yield {
        "status": "ok",
        "message": f"Feedback {saved.feedback_id} recorded",
        "feedback_id": saved.feedback_id,
    }


async def _write_chunked(f, content: str) -> AsyncIterator[dict[str, Any]]:
    """Write *content* in blocks, yielding progress between blocks.

    A single ``f.write(content)`` gives the UI nothing to show for large
    payloads — the tool card just spins until it finishes. Chunking makes
    progress visible ("Writing: N/M chars") while bounding the yield
    count (~20 events regardless of size) so the persisted progress list
    stays small.
    """
    total = len(content)
    if total == 0:
        f.write("")
        return
    chunk_size = max(1024, total // 20)
    if total <= chunk_size:
        # Small payloads stay single-shot — no progress noise.
        f.write(content)
        return
    written = 0
    while written < total:
        f.write(content[written : written + chunk_size])
        written = min(total, written + chunk_size)
        yield {
            "status": "progress",
            "message": f"Writing: {written}/{total} chars",
        }


async def local_file_operator_fn(
    operation: str = "",
    path: str = "",
    content: str = "",
    old_string: str = "",
    new_string: str = "",
    source: str = "",
    destination: str = "",
    recursive: bool = False,
    limit: int = 0,
) -> AsyncIterator[Any]:
    if not operation:
        yield {"status": "error", "message": "operation is required"}
        return

    valid_operations = [
        "read",
        "write",
        "append",
        "edit",
        "delete",
        "list_dir",
        "mkdir",
        "move",
        "copy",
        "exists",
    ]
    if operation not in valid_operations:
        yield {
            "status": "error",
            "message": f"Invalid operation '{operation}'. Valid operations: {', '.join(valid_operations)}",
        }
        return

    try:
        if operation == "read":
            yield {"status": "progress", "message": f"Reading file: {path}"}
            if not os.path.isfile(path):
                yield {"status": "error", "message": f"File not found: {path}"}
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            yield {
                "status": "ok",
                "message": f"Read {len(data)} characters from {path}",
                "content": data,
                "path": path,
                "size": len(data),
            }

        elif operation == "write":
            yield {"status": "progress", "message": f"Writing to file: {path}"}
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
                yield {
                    "status": "progress",
                    "message": f"Created parent directory: {parent}",
                }
            with open(path, "w", encoding="utf-8") as f:
                async for p in _write_chunked(f, content):
                    yield p
            yield {
                "status": "ok",
                "message": f"Successfully wrote {len(content)} characters to {path}",
                "path": path,
                "size": len(content),
            }

        elif operation == "append":
            yield {"status": "progress", "message": f"Appending to file: {path}"}
            if os.path.isfile(path):
                with open(path, "a", encoding="utf-8") as f:
                    async for p in _write_chunked(f, content):
                        yield p
                yield {
                    "status": "ok",
                    "message": f"Successfully appended {len(content)} characters to {path}",
                    "path": path,
                    "size": len(content),
                }
            else:
                yield {
                    "status": "error",
                    "message": f"File not found for appending: {path}. Use operation='write' to create the file first, or check the path with operation='exists'.",
                    "suggestion": "Use write (not append) to create a new file",
                }

        elif operation == "edit":
            yield {"status": "progress", "message": f"Editing file: {path}"}
            if not os.path.isfile(path):
                yield {"status": "error", "message": f"File not found: {path}"}
                return
            if not old_string:
                yield {
                    "status": "error",
                    "message": "old_string is required for edit operation",
                }
                return
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
            if old_string not in data:
                yield {
                    "status": "error",
                    "message": f"old_string not found in {path}. The exact text to replace must match the file content exactly (including whitespace and line endings). Use operation='read' first to verify the actual content, then retry edit with the precise text.",
                    "file_content_preview": data[:500],
                    "suggestion": "Use read to see the actual file content, then copy the exact text into old_string",
                }
                return
            count = data.count(old_string)
            new_data = data.replace(old_string, new_string)
            diff_lines = list(
                difflib.unified_diff(
                    data.splitlines(keepends=True),
                    new_data.splitlines(keepends=True),
                    fromfile=path,
                    tofile=path,
                    lineterm="",
                )
            )
            with open(path, "w", encoding="utf-8") as f:
                async for p in _write_chunked(f, new_data):
                    yield p
            yield {
                "status": "ok",
                "message": f"Replaced {count} occurrence(s) in {path}",
                "path": path,
                "replacement_count": count,
                "diff": diff_lines,
            }

        elif operation == "delete":
            yield {"status": "progress", "message": f"Deleting: {path}"}
            if os.path.isfile(path):
                os.remove(path)
                yield {"status": "ok", "message": f"Deleted file: {path}"}
            elif os.path.isdir(path):
                shutil.rmtree(path)
                yield {"status": "ok", "message": f"Deleted directory: {path}"}
            else:
                yield {"status": "error", "message": f"Path not found: {path}"}

        elif operation == "list_dir":
            yield {"status": "progress", "message": f"Listing directory: {path}"}
            if os.path.isfile(path):
                yield {
                    "status": "error",
                    "message": f"Path is a file, not a directory: {path}. Use operation='read' to read the file content.",
                    "suggestion": "Use read to read this file, or provide a directory path",
                }
                return
            if not os.path.isdir(path):
                yield {"status": "error", "message": f"Directory not found: {path}"}
                return
            entries = []
            try:
                for entry in os.scandir(path):
                    info = {
                        "name": entry.name,
                        "type": "directory" if entry.is_dir() else "file",
                        "size": entry.stat().st_size if entry.is_file() else 0,
                    }
                    entries.append(info)
            except PermissionError:
                yield {"status": "error", "message": f"Permission denied: {path}"}
                return
            entries_sorted = sorted(entries, key=lambda x: (x["type"], x["name"]))
            if limit > 0 and len(entries_sorted) > limit:
                entries_sorted = entries_sorted[:limit]
            yield {
                "status": "ok",
                "message": f"Listed {len(entries_sorted)} entries in {path}"
                + (f" (showing first {limit})" if 0 < limit < len(entries) else ""),
                "entries": entries_sorted,
                "path": path,
                "total": len(entries),
            }

        elif operation == "mkdir":
            yield {"status": "progress", "message": f"Creating directory: {path}"}
            if recursive:
                os.makedirs(path, exist_ok=True)
            else:
                try:
                    os.mkdir(path)
                except FileExistsError:
                    yield {
                        "status": "error",
                        "message": f"Directory already exists: {path}. If you want to create parent directories as well, use recursive=True.",
                        "suggestion": "Use recursive=True to create parent directories, or use a different path",
                    }
                    return
            yield {"status": "ok", "message": f"Created directory: {path}"}

        elif operation == "move":
            yield {"status": "progress", "message": f"Moving {source} -> {destination}"}
            if not source:
                yield {
                    "status": "error",
                    "message": "source is required for move operation",
                }
                return
            if not destination:
                yield {
                    "status": "error",
                    "message": "destination is required for move operation",
                }
                return
            parent = os.path.dirname(destination)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            shutil.move(source, destination)
            yield {
                "status": "ok",
                "message": f"Moved {source} -> {destination}",
                "source": source,
                "destination": destination,
            }

        elif operation == "copy":
            yield {
                "status": "progress",
                "message": f"Copying {source} -> {destination}",
            }
            if not source:
                yield {
                    "status": "error",
                    "message": "source is required for copy operation",
                }
                return
            if not destination:
                yield {
                    "status": "error",
                    "message": "destination is required for copy operation",
                }
                return
            parent = os.path.dirname(destination)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(source):
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
            yield {
                "status": "ok",
                "message": f"Copied {source} -> {destination}",
                "source": source,
                "destination": destination,
            }

        elif operation == "exists":
            exists = os.path.exists(path)
            is_file = os.path.isfile(path) if exists else False
            is_dir = os.path.isdir(path) if exists else False
            yield {
                "status": "ok",
                "message": f"Path {'exists' if exists else 'does not exist'}: {path}",
                "path": path,
                "exists": exists,
                "is_file": is_file,
                "is_dir": is_dir,
            }

    except FileNotFoundError:
        yield {
            "status": "error",
            "message": f"Path not found: {path}. Verify the path with operation='exists' or create it with write/mkdir first.",
            "suggestion": "Check if the path exists using exists operation, or create it",
        }
    except PermissionError:
        yield {
            "status": "error",
            "message": f"Permission denied: {path}. The process does not have the required permissions.",
            "suggestion": "Use a path the user has access to, or change file permissions",
        }
    except IsADirectoryError:
        yield {
            "status": "error",
            "message": f"Expected a file but path is a directory: {path}. Use operation='list_dir' to list its contents instead.",
            "suggestion": "Use list_dir to browse the directory, or include a filename in the path",
        }
    except NotADirectoryError:
        yield {
            "status": "error",
            "message": f"Expected a directory but path is a file: {path}. Use operation='read' to read the file instead.",
            "suggestion": "Use read to read the file, or provide a directory path for this operation",
        }
    except OSError as e:
        yield {
            "status": "error",
            "message": f"File operation failed: {e}",
            "suggestion": "Check that the path is valid and the operation is appropriate for this path type",
        }


BUILTIN_TOOL_METADATA: list[dict[str, Any]] = [
    {
        "name": "submit_feedback",
        "display_name": "Submit Feedback",
        "display_name_locale": json.dumps(
            {"zh": "提交反馈", "en": "Submit Feedback"},
            ensure_ascii=False,
        ),
        "description": (
            "Collect user praise or criticism from the conversation. "
            "Only call when the user explicitly expresses strong satisfaction "
            "or dissatisfaction in natural language."
        ),
        "description_locale": json.dumps(
            {
                "zh": "从对话中收集用户的表扬或批评意见。仅在用户用自然语言明确表达了强烈不满或大力赞扬时调用。",
                "en": "Collect user praise or criticism from the conversation. Only call when the user explicitly expresses strong satisfaction or dissatisfaction in natural language.",
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["praise", "blame"],
                    "description": "Feedback type",
                },
                "comment": {
                    "type": "string",
                    "description": "User's exact comment or summary",
                },
                "category": {
                    "type": "string",
                    "description": "Category tag e.g. clarity, accuracy, speed",
                },
                "target_type": {
                    "type": "string",
                    "enum": ["message", "tool_call"],
                    "description": "Target entity type",
                },
                "target_id": {
                    "type": "string",
                    "description": "Target entity ID",
                },
            },
            "required": ["type"],
        },
        "_fn": submit_feedback_fn,
    },
    {
        "name": "bash",
        "display_name": "Bash",
        "display_name_locale": json.dumps(
            {"zh": "Bash 命令", "en": "Bash"}, ensure_ascii=False
        ),
        "description": (
            f"Execute shell commands on the current system ({_PLATFORM_NAME}, using {_SHELL_NAME}). "
            f"Always use {_CMD_SYNTAX_HINT}. Returns stdout, stderr, and exit code."
        ),
        "description_locale": json.dumps(
            {
                "zh": (
                    f"在当前系统（{_PLATFORM_NAME}，使用 {_SHELL_NAME}）上执行 shell 命令。"
                    f"请始终使用 {_CMD_SYNTAX_HINT}。返回标准输出、标准错误和退出码。"
                ),
                "en": (
                    f"Execute shell commands on the current system ({_PLATFORM_NAME}, using {_SHELL_NAME}). "
                    f"Always use {_CMD_SYNTAX_HINT}. Returns stdout, stderr, and exit code."
                ),
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"The shell command to execute. Use {_CMD_SYNTAX_HINT}.",
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory for the command (optional). Defaults to the process working directory.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Timeout in milliseconds (default: 60000, max: 300000).",
                    "default": 60000,
                },
            },
            "required": ["command"],
        },
        "_fn": bash_fn,
    },
    {
        "name": "local_file_operator",
        "display_name": "Local File Operator",
        "display_name_locale": json.dumps(
            {"zh": "本地文件操作", "en": "Local File Operator"}, ensure_ascii=False
        ),
        "description": (
            "Read, write, append, edit (find/replace), delete, list directories, "
            "create directories, move, copy, and check existence of local files and directories."
        ),
        "description_locale": json.dumps(
            {
                "zh": "对本地文件和目录进行读取、写入、追加、编辑（查找替换）、删除、列出目录、创建目录、移动、复制和检查存在性等操作。",
                "en": (
                    "Read, write, append, edit (find/replace), delete, list directories, "
                    "create directories, move, copy, and check existence of local files and directories."
                ),
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "File operation to perform. One of: read, write, append, edit, delete, list_dir, mkdir, move, copy, exists.",
                    "enum": [
                        "read",
                        "write",
                        "append",
                        "edit",
                        "delete",
                        "list_dir",
                        "mkdir",
                        "move",
                        "copy",
                        "exists",
                    ],
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path to operate on. Required for: read, write, append, edit, delete, list_dir, mkdir, exists.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write or append. Required for: write, append.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace. Required for: edit.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text. Used with old_string for: edit.",
                },
                "source": {
                    "type": "string",
                    "description": "Source path. Required for: move, copy.",
                },
                "destination": {
                    "type": "string",
                    "description": "Destination path. Required for: move, copy.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Create parent directories recursively. Used with: mkdir.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return. Used with: list_dir. 0 means no limit.",
                    "default": 0,
                },
            },
            "required": ["operation"],
        },
        "_fn": local_file_operator_fn,
    },
]
