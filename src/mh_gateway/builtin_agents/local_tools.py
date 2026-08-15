from __future__ import annotations

import asyncio
import collections
import difflib
import json
import os
import signal
import subprocess
import sys
from itertools import islice
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


def _kill_tree(process: Any) -> None:
    """Kill the shell and its whole descendant tree.

    ``terminate()`` only signals the direct child; grandchildren (uv ->
    python) inherit the pipes and survive, keeping them open so
    ``process.wait()`` never returns (observed on Windows).  Kill the tree
    instead: ``taskkill /T /F`` on Windows, the whole process group on POSIX
    (the shell was spawned with ``start_new_session``).
    """
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass


async def bash_fn(
    command: str = "",
    workdir: str = "",
    timeout: float = 300.0,
) -> AsyncIterator[Any]:
    if not command:
        yield {"status": "error", "message": "command is required"}
        return

    _cmd = (
        "$OutputEncoding=[System.Text.UTF8Encoding]::new(); [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        # Windows PowerShell 里 `curl` 是 Invoke-WebRequest 的 alias(慢、行为不同),
        # 移除后解析到系统 curl.exe。
        "Remove-Item Alias:curl -ErrorAction SilentlyContinue; " + command
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
            "message": f"Working directory does not exist: {cwd}. Use write_file (it creates parent directories automatically) or create the directory first, or omit workdir to use the default directory.",
        }
        return

    # No-output timeout in seconds: a silent command is stopped after this
    # window and reports a timeout error, letting the agent decide how to
    # recover.  The model picks the value (default 300s when omitted).
    timeout_s = float(timeout)

    # 滚动窗口：partial_stdout/partial_stderr 只保留尾部，避免逐行携带
    # 全量输出 —— 大目录遍历等长输出命令下，逐行 join 全量 + 序列化 +
    # 持久化 + 前端累积是 O(n²) 拷贝，会打满事件循环并撑爆内存。
    WINDOW_LIMIT = 64 * 1024
    # 批量冲刷：每 50 行或 100ms 才发一个进度事件，海量行输出
    # （如遍历数万文件）不会产生海量 SSE 事件。
    FLUSH_LINES = 50
    FLUSH_INTERVAL = 0.1

    try:
        # POSIX: start a new session so the whole process group (bash + its
        # children) can be killed with os.killpg on timeout.  Without it,
        # terminate() only signals the direct child and grandchildren keep
        # the pipes open.  Windows handles this via taskkill /T instead.
        spawn_kwargs: dict[str, Any] = {}
        if not _IS_WINDOWS:
            spawn_kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *shell_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # StreamReader.readline() 默认 limit 64KB：单行超过会抛 ValueError 让
            # pump 静默死亡（表现为 30s 后命令超时、输出为空）。提到 1MB 以容忍
            # 罕见的长单行（如无换行的超长输出）；更大的行退化为超时报错而非挂死。
            limit=1024 * 1024,
            **spawn_kwargs,
        )

        queue: asyncio.Queue = asyncio.Queue()
        timed_out = False
        loop = asyncio.get_running_loop()
        total_out = 0  # 命令实际总输出字节数（用于截断提示）

        async def _pump(stream, bufs, label: str) -> None:
            nonlocal total_out
            batch: list[str] = []
            last_flush = loop.time()
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                total_out += len(text)
                batch.append(text)
                now = loop.time()
                if len(batch) >= FLUSH_LINES or now - last_flush >= FLUSH_INTERVAL:
                    joined = "".join(batch)
                    batch = []
                    last_flush = now
                    if len(joined) > WINDOW_LIMIT:
                        joined = joined[-WINDOW_LIMIT:]
                    bufs.append(joined)
                    while sum(len(s) for s in bufs) > WINDOW_LIMIT:
                        bufs.popleft()
                    await queue.put(
                        ("chunk", label, joined.rstrip("\n\r"), "".join(bufs))
                    )
            if batch:
                joined = "".join(batch)
                batch = []
                if len(joined) > WINDOW_LIMIT:
                    joined = joined[-WINDOW_LIMIT:]
                bufs.append(joined)
                while sum(len(s) for s in bufs) > WINDOW_LIMIT:
                    bufs.popleft()
                await queue.put(("chunk", label, joined.rstrip("\n\r"), "".join(bufs)))
            await queue.put(("done", label, None, "".join(bufs)))

        stdout_bufs: collections.deque[str] = collections.deque()
        stderr_bufs: collections.deque[str] = collections.deque()

        t_stdout = asyncio.create_task(_pump(process.stdout, stdout_bufs, "stdout"))
        t_stderr = asyncio.create_task(_pump(process.stderr, stderr_bufs, "stderr"))

        def _stream_event(
            typ: str, label: str, content: str, partial: str
        ) -> dict | None:
            """构造 progress 事件;done 事件不再作为结束信号,返回 None 忽略。"""
            if typ == "done":
                return None
            return {
                "status": "progress",
                "type": "stream",
                "stream": label,
                "content": content,
                "partial_stdout": partial if label == "stdout" else None,
                "partial_stderr": partial if label == "stderr" else None,
            }

        background = False
        try:
            while True:
                # 命令结束 = 直接子进程(shell)退出,不再死等双管道 EOF:
                # 后台子进程(Start-Process / sleep &)继承管道写端时管道永不
                # 关闭,旧逻辑会把已完成的前台命令(如 curl)误判为超时。
                if process.returncode is not None:
                    break
                try:
                    typ, label, content, partial = await asyncio.wait_for(
                        queue.get(), timeout=timeout_s
                    )
                except asyncio.TimeoutError:
                    if process.returncode is not None:
                        break  # shell 刚退出且队列已空 → 正常收尾
                    timed_out = True
                    break
                ev = _stream_event(typ, label, content, partial)
                if ev is not None:
                    yield ev
                else:
                    # 管道 EOF ⇒ shell(管道写端)已退出,但 Windows 上 returncode
                    # 回调可能晚于 EOF 通知——显式 wait() 让 returncode 就绪,
                    # 否则下一轮会在空队列上误等整个 timeout_s。已退出则立即返回。
                    try:
                        await asyncio.wait_for(process.wait(), timeout=timeout_s)
                    except asyncio.TimeoutError:
                        pass  # 不应发生(EOF ⇒ shell 已退);兜底回到循环继续
        finally:
            if process.returncode is None:
                _kill_tree(process)
            # shell 退出后给 pump 短窗口排空最后输出;后台子进程持有管道时
            # pump 挂住,此时取消并标记 background(不杀树,保留刚起的服务)。
            # 每个 await 都有上限,卡住的 pump 只延迟我们,不会阻塞 forever。
            try:
                await asyncio.wait_for(
                    asyncio.gather(t_stdout, t_stderr, return_exceptions=True),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                background = True
                t_stdout.cancel()
                t_stderr.cancel()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                pass

        # drain 队列剩余事件(shell 退出瞬间已入队但尚未消费的)
        while True:
            try:
                typ, label, content, partial = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            ev = _stream_event(typ, label, content, partial)
            if ev is not None:
                yield ev

        stdout_str = "".join(stdout_bufs)
        stderr_str = "".join(stderr_bufs)
        returncode = process.returncode if not timed_out else -1

        line_count = stdout_str.count("\n")
        truncated = total_out > WINDOW_LIMIT

        if timed_out:
            yield {
                "status": "error",
                "message": f"Command timed out after {timeout_s:g}s with no output. Try increasing timeout or simplifying the command.",
                "command": command[:500],
                "timed_out": True,
                "suggestion": "Increase timeout or break the command into smaller steps",
                "stdout": stdout_str,
                "stderr": stderr_str,
                "truncated": truncated,
                "total_output_bytes": total_out,
            }
        elif returncode == 0:
            message = (
                f"Command completed successfully, output truncated (showing last {WINDOW_LIMIT} of {total_out} bytes)"
                if truncated
                else (
                    f"Command completed successfully ({line_count} lines of output)"
                    if line_count
                    else "Command completed successfully (no output)"
                )
            )
            if background:
                message += " Background process(es) are still running; output may be incomplete."
            yield {
                "status": "ok",
                "message": message,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "exit_code": 0,
                "command": command[:500],
                "truncated": truncated,
                "total_output_bytes": total_out,
                "background_processes": background,
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
                "truncated": truncated,
                "total_output_bytes": total_out,
                "background_processes": background,
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

    # resolve agent_name from the session + ownership check (same as POST /api/v1/feedback)
    try:
        session = await adapters.sessions.get_session(session_id)
    except Exception:
        session = None
    if session is None:
        yield {"status": "error", "message": "Session not found"}
        return
    if getattr(session, "user_id", None) != user_id:
        yield {
            "status": "error",
            "message": "Access denied: session does not belong to current user",
        }
        return
    agent_name = getattr(session, "agent_name", "") or ""

    # The agent cannot know the msg-{seq} id of in-flight messages, so when
    # no target was given, auto-link the feedback to the user message that
    # carried the opinion — that's what the replay page highlights.
    if not target_id:
        msgs = getattr(session, "get_all_messages", lambda: [])() or []
        for m in reversed(msgs):
            if m.get("role") == "user" and m.get("id"):
                target_id = m["id"]
                target_type = "message"
                break

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


async def read_file_fn(
    path: str = "",
    offset: int = 1,
    limit: int = 2000,
) -> AsyncIterator[Any]:
    if not path:
        yield {"status": "error", "message": "path is required"}
        return
    yield {"status": "progress", "message": f"Reading file: {path}"}
    if not os.path.isfile(path):
        yield {"status": "error", "message": f"File not found: {path}"}
        return
    start = max(int(offset), 1) - 1
    max_lines = max(int(limit), 1)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        skipped = sum(1 for _ in islice(f, start))
        selected = list(islice(f, max_lines))
        rest = sum(1 for _ in f)
    total = skipped + len(selected) + rest
    truncated = rest > 0
    yield {
        "status": "ok",
        "message": (
            f"Read lines {skipped + 1}-{skipped + len(selected)} of {total} from {path}"
            + (" (truncated - use offset to read the next page)" if truncated else "")
        ),
        "content": "".join(selected),
        "path": path,
        "start_line": skipped + 1,
        "end_line": skipped + len(selected),
        "total_lines": total,
        "truncated": truncated,
    }


async def write_file_fn(path: str = "", content: str = "") -> AsyncIterator[Any]:
    if not path:
        yield {"status": "error", "message": "path is required"}
        return
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


async def append_file_fn(path: str = "", content: str = "") -> AsyncIterator[Any]:
    if not path:
        yield {"status": "error", "message": "path is required"}
        return
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
            "message": f"File not found for appending: {path}. Use write_file to create the file first.",
            "suggestion": "Use write_file (not append_file) to create a new file",
        }


async def edit_file_fn(
    path: str = "",
    old_string: str = "",
    new_string: str = "",
) -> AsyncIterator[Any]:
    if not path:
        yield {"status": "error", "message": "path is required"}
        return
    if not old_string:
        yield {"status": "error", "message": "old_string is required"}
        return
    yield {"status": "progress", "message": f"Editing file: {path}"}
    if not os.path.isfile(path):
        yield {"status": "error", "message": f"File not found: {path}"}
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    if old_string not in data:
        yield {
            "status": "error",
            "message": f"old_string not found in {path}. The exact text to replace must match the file content exactly (including whitespace and line endings). Use read_file first to verify the actual content, then retry edit_file with the precise text.",
            "file_content_preview": data[:500],
            "suggestion": "Use read_file to see the actual file content, then copy the exact text into old_string",
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


BUILTIN_TOOL_METADATA: list[dict[str, Any]] = [
    {
        "name": "submit_feedback",
        "display_name": "Submit Feedback",
        "display_name_locale": json.dumps(
            {"zh": "提交反馈", "en": "Submit Feedback"},
            ensure_ascii=False,
        ),
        "description": (
            "Record user feedback about your previous response. Call when the "
            "user criticizes or corrects what you just said, or asks you to "
            "improve/redo it — even if their words are also a new task. You "
            "MUST record the feedback BEFORE continuing to fix the issue. "
            "Examples: 'you got it wrong', 'this approach won't work, use X "
            "instead', 'you should have shown me a demo', 'well done'. Do NOT "
            "call for neutral or vague statements. target_id is optional — "
            "leave it empty and the system auto-links the feedback to the "
            "user's current message."
        ),
        "description_locale": json.dumps(
            {
                "zh": "记录用户对您上一条回答的反馈。当用户批评或纠正您刚说的内容，或要求改进/重做时调用——即使他们的话同时也是一条新任务。您必须先记录反馈（type=blame），再继续补救问题。示例：‘你答错了’、‘这个方案不行，改用X’、‘你至少应该给我展示一下吧’、‘回答得很好’。对中性或含糊的表述不要调用。target_id 为可选项——留空时系统会自动关联到用户当前这条消息。",
                "en": "Record user feedback about your previous response. Call when the user criticizes or corrects what you just said, or asks you to improve/redo it — even if their words are also a new task. You MUST record the feedback BEFORE continuing to fix the issue. Examples: 'you got it wrong', 'this approach won't work, use X instead', 'you should have shown me a demo', 'well done'. Do NOT call for neutral or vague statements. target_id is optional — leave it empty and the system auto-links the feedback to the user's current message.",
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
                    "description": "Optional. Leave empty to auto-link to the user's current message",
                },
                "target_id": {
                    "type": "string",
                    "description": "Optional. Leave empty to auto-link to the user's current message",
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
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds. If the command produces no output for this long it is stopped and an error is returned. Required — choose a value appropriate for the command (defaults to 300 if omitted).",
                },
            },
            "required": ["command", "timeout"],
        },
        "_fn": bash_fn,
    },
    {
        "name": "read_file",
        "display_name": "Read File",
        "display_name_locale": json.dumps(
            {"zh": "读取文件", "en": "Read File"}, ensure_ascii=False
        ),
        "description": (
            "Read the content of a text file, optionally paging by line range "
            "(offset/limit). Large files are truncated by default - use offset "
            "to read further pages. Use this instead of bash cat/type/head/tail/sed "
            "for file reads."
        ),
        "description_locale": json.dumps(
            {
                "zh": "读取文本文件内容，支持按行分页（offset/limit）。大文件默认截断——使用 offset 继续读取下一页。读取文件请用本工具，不要用 bash 的 cat/type/head/tail/sed。",
                "en": "Read the content of a text file, optionally paging by line range (offset/limit). Large files are truncated by default - use offset to read further pages. Use this instead of bash cat/type/head/tail/sed for file reads.",
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the text file to read",
                },
                "offset": {
                    "type": "integer",
                    "description": "1-based line number to start reading from (default: 1)",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default: 2000). Set to 0 for no limit (large files will consume many tokens).",
                    "default": 2000,
                },
            },
            "required": ["path"],
        },
        "_fn": read_file_fn,
    },
    {
        "name": "write_file",
        "display_name": "Write File",
        "display_name_locale": json.dumps(
            {"zh": "写入文件", "en": "Write File"}, ensure_ascii=False
        ),
        "description": (
            "Create a new file or overwrite an existing file with the given "
            "content. Parent directories are created automatically. Use this "
            "instead of bash redirection (echo > file) for writing files."
        ),
        "description_locale": json.dumps(
            {
                "zh": "用给定内容创建新文件或覆盖已有文件。父目录会自动创建。写文件请用本工具，不要用 bash 重定向（echo > file）。",
                "en": "Create a new file or overwrite an existing file with the given content. Parent directories are created automatically. Use this instead of bash redirection (echo > file) for writing files.",
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to create or overwrite",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
        "_fn": write_file_fn,
    },
    {
        "name": "append_file",
        "display_name": "Append File",
        "display_name_locale": json.dumps(
            {"zh": "追加文件", "en": "Append File"}, ensure_ascii=False
        ),
        "description": (
            "Append content to the end of an existing file. Use this instead of "
            "bash '>>' redirection. If the file does not exist, use write_file "
            "to create it first."
        ),
        "description_locale": json.dumps(
            {
                "zh": "将内容追加到已有文件末尾。追加请用本工具，不要用 bash 的 '>>' 重定向。若文件不存在，先用 write_file 创建。",
                "en": "Append content to the end of an existing file. Use this instead of bash '>>' redirection. If the file does not exist, use write_file to create it first.",
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to append to",
                },
                "content": {
                    "type": "string",
                    "description": "Content to append to the end of the file",
                },
            },
            "required": ["path", "content"],
        },
        "_fn": append_file_fn,
    },
    {
        "name": "edit_file",
        "display_name": "Edit File",
        "display_name_locale": json.dumps(
            {"zh": "编辑文件", "en": "Edit File"}, ensure_ascii=False
        ),
        "description": (
            "Replace exact text in an existing file (find-and-replace), reading "
            "and writing in one call. If old_string is not found, use read_file "
            "first and copy the exact text - whitespace and line endings must "
            "match. Use this instead of sed/perl one-liners."
        ),
        "description_locale": json.dumps(
            {
                "zh": "在已有文件中做精确查找替换，一次调用完成读写。若 old_string 未命中，先用 read_file 查看真实内容再精确复制（空白和换行必须一致）。修改文件请用本工具，不要用 sed/perl 单行命令。",
                "en": "Replace exact text in an existing file (find-and-replace), reading and writing in one call. If old_string is not found, use read_file first and copy the exact text - whitespace and line endings must match. Use this instead of sed/perl one-liners.",
            },
            ensure_ascii=False,
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace (must match the file exactly, including whitespace and line endings)",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        "_fn": edit_file_fn,
    },
]
