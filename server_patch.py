import os
import json
import shutil
import difflib
import time
import re
from mcp.server.fastmcp import FastMCP
from tools.auto_repair.get_repo_structure import get_project_structure_from_local
from tools.auto_repair.check_build_res import check_main
from tools.auto_repair.upload_files import main_upload
import tarfile
import zipfile
from typing import List, Tuple

mcp = FastMCP("auto_repair_server_patch")

# Server-side state storage
server_state = {
    "modification_history": {},  # {package_name: [{file_path, diff, timestamp}, ...]}
    "tool_call_history": {},  # {package_name: [(tool_name, args_key), ...]}
    "tool_cache": {},  # {package_name: {call_key: result, ...}}
}


@mcp.tool()
def init_package_environment_tool(
    base_dir: str, package_name: str, temp_work_dir: str, result_dir: str
) -> str:
    """
    Initializes the package's temporary working environment and copies the original files to a temporary directory.
    Args:
        base_dir: Base directory
        package_name: Package name
        temp_work_dir: Temporary working directory
        result_dir: Result storage directory
    Returns:
        JSON string containing the initialization result
    """
    try:
        package_temp_dir = os.path.join(temp_work_dir, package_name)
        if os.path.exists(package_temp_dir):
            shutil.rmtree(package_temp_dir)
        os.makedirs(package_temp_dir, exist_ok=True)

        original_package_path = os.path.join(base_dir, package_name)
        if not os.path.exists(original_package_path):
            return json.dumps(
                {
                    "success": False,
                    "message": f"Original package path not found: {original_package_path}",
                }
            )

        # Copy the file to a temporary directory
        for item in os.listdir(original_package_path):
            src = os.path.join(original_package_path, item)
            dst = os.path.join(package_temp_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        result_file = os.path.join(result_dir, f"{package_name}_result.txt")

        return json.dumps(
            {
                "success": True,
                "package_temp_dir": package_temp_dir,
                "package_path": package_temp_dir,
                "result_file": result_file,
                "message": f"Initialized package environment: {package_temp_dir}",
            }
        )
    except Exception as e:
        return json.dumps(
            {"success": False, "message": f"Initialization failed: {str(e)}"}
        )


@mcp.tool()
def track_file_modification_tool(
    package_name: str,
    file_path: str,
    package_path: str,
    old_content: str,
    new_content: str,
) -> str:
    """
    Tracks file modification history and records differences.
    Args:
        package_name: Package name
        file_path: File path (relative path)
        package_path: Package path
        old_content: Content before modification
        new_content: Content after modification
    Returns:
        Tracking results
    """
    try:
        if package_name not in server_state["modification_history"]:
            server_state["modification_history"][package_name] = []

        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)

        # Calculate the difference
        diff = []
        for i, line in enumerate(
            difflib.unified_diff(old_lines, new_lines, lineterm="")
        ):
            if i < 3:
                continue
            if line.startswith(""):
                diff.append(
                    {
                        "operation": "add",
                        "line_number": i - 2,
                        "content": line[1:].rstrip("\n"),
                    }
                )
            elif line.startswith("-"):
                diff.append(
                    {
                        "operation": "delete",
                        "line_number": i - 2,
                        "content": line[1:].rstrip("\n"),
                    }
                )
            elif line.startswith(" "):
                diff.append(
                    {
                        "operation": "keep",
                        "line_number": i - 2,
                        "content": line[1:].rstrip("\n"),
                    }
                )

        # Storage differences
        server_state["modification_history"][package_name].append(
            {
                "file_path": file_path,
                "diff": diff,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        return f"Successfully tracked modification to {file_path}"
    except Exception as e:
        return f"Error tracking modification: {str(e)}"


@mcp.tool()
def parse_build_result_tool(result_content: str, package_name: str) -> str:
    """
    Parse the build results to determine success.
    Args:
        result_content: Build result content
        package_name: Package name
    Returns:
        JSON string containing the parsed results
    """
    try:
        status = result_content.split(": ", 1)[-1]
        low = status.lower()
        success = any(
            k in low for k in ["success", "succeeded", "successfully", "passed", "ok"]
        )
        return json.dumps({"success": success, "status": status})
    except Exception:
        return json.dumps({"success": False, "status": "Unknown (parse error)"})


@mcp.tool()
def update_prompt_with_history_tool(
    package_name: str, package_path: str, build_attempt: int, formatted_prompt: str
) -> str:
    """
    Update prompt, including modification history.
    Args:
        package_name: Package name
        package_path: Package path
        build_attempt: Number of build attempts
        formatted_prompt: Formatted system prompt
    Returns:
        JSON string containing the updated message list
    """
    current_prompt = (
        f"Please analyze and repair package {package_name} in: {package_path}. "
    )
    current_prompt = "All modifications must be done in the temporary directory. "

    # Add historical modification records
    if build_attempt > 1:
        prev_modifications = server_state["modification_history"].get(package_name, [])
        if prev_modifications:
            current_prompt = "\n\nPrevious modifications:\n"
            for mod in prev_modifications:
                current_prompt = f"File: {mod['file_path']}\n"
                current_prompt = "Changes:\n"
                for change in mod["diff"]:
                    op = change["operation"]
                    line = change["line_number"]
                    content = change["content"][:200]
                    current_prompt = f"- Line {line} ({op}): {content}\n"
                current_prompt = "\n"
        current_prompt = f"After {build_attempt - 1} attempts, build still failed. "
        current_prompt = (
            "Analyze previous modifications and failures, then provide new repair plan."
        )

    return json.dumps(
        {
            "messages": [
                {"role": "system", "content": formatted_prompt},
                {"role": "user", "content": current_prompt},
            ]
        }
    )


@mcp.tool()
def get_file_content_tool(file_path: str) -> str:
    """
    Get file contents
    Args:
        file_path: Absolute file path
    Returns:
        File contents
    """
    try:
        if not os.path.exists(file_path):
            return f"Error: File not found - {file_path}"
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"


@mcp.tool()
def check_repeat_tool_call(
    tool_name: str, args_key: str, max_repeat: int, package_name: str
) -> str:
    """
    Check if the tool call is repeated
    Args:
        tool_name: Tool name
        args_key: Argument key
        max_repeat: Maximum repeat count
        package_name: Package name
    Returns:
        JSON string containing the check results
    """
    call_key = args_key
    if package_name not in server_state["tool_call_history"]:
        server_state["tool_call_history"][package_name] = []

    repeat_count = server_state["tool_call_history"][package_name].count(call_key)
    if repeat_count >= max_repeat:
        return json.dumps(
            {
                "allowed": False,
                "message": f"Tool call {tool_name} exceeded max repeat count ({max_repeat})",
            }
        )
    return json.dumps({"allowed": True, "message": "Tool call allowed"})


@mcp.tool()
def check_tool_cache(call_key: str, tool_name: str, package_name: str) -> str:
    """
    Check tool call result cache
    Args:
        call_key: Call key
        tool_name: Tool name
        package_name: Package name
    Returns:
        JSON string containing the cache check results
    """
    if package_name not in server_state["tool_cache"]:
        server_state["tool_cache"][package_name] = {}

    if call_key in server_state["tool_cache"][package_name]:
        return json.dumps(
            {"hit": True, "result": server_state["tool_cache"][package_name][call_key]}
        )
    return json.dumps({"hit": False, "result": ""})


@mcp.tool()
def cache_tool_result(call_key: str, result: str, package_name: str) -> str:
    """
    Cache tool call result
    Args:
        call_key: Call key
        result: Result content
        package_name: Package name
    Returns:
        Cache result
    """
    if package_name not in server_state["tool_cache"]:
        server_state["tool_cache"][package_name] = {}
    server_state["tool_cache"][package_name][call_key] = result
    return f"Successfully cached result for {call_key}"


@mcp.tool()
def reset_package_cache_tool(package_name: str) -> str:
    """
    Clear per-package caches for a new attempt.
    - Clears: tool_cache and tool_call_history
    - Keeps:  modification_history (so dynamic prompt can use it)
    """
    # initialize keys if missing
    server_state.setdefault("tool_cache", {})
    server_state.setdefault("tool_call_history", {})
    server_state.setdefault("modification_history", {})

    # clear cache  call history for this package
    if package_name in server_state["tool_cache"]:
        server_state["tool_cache"][package_name].clear()
    if package_name in server_state["tool_call_history"]:
        server_state["tool_call_history"][package_name].clear()

    return json.dumps(
        {
            "success": True,
            "message": f"Cleared tool_cache and tool_call_history for package '{package_name}'.",
        }
    )


@mcp.tool()
def record_tool_call_history(call_key: str, package_name: str) -> str:
    """
    Record tool call history
    Args:
        call_key: Call key
        package_name: Package name
    Returns:
        Record result
    """
    if package_name not in server_state["tool_call_history"]:
        server_state["tool_call_history"][package_name] = []
    server_state["tool_call_history"][package_name].append(call_key)
    return f"Recorded tool call history for {package_name}"


@mcp.tool()
def get_structure_of_files(package_path) -> dict:
    """
    Retrieve project structure diagram.
    Args:
        package_path (str): Package path.
    """
    return get_project_structure_from_local(package_path)


def _parse_git_unified_patch(patch_text: str):
    """
    Parse unified diff (supports multi-file).
    Returns: List[{"old":old_path,"new":new_path,"hunks":[(a_l,a_s,b_l,b_s,lines)],"is_new":bool,"is_delete":bool}]
    where lines are original '+/-/ ' lines (no newline), maintaining order.
    """
    lines = patch_text.splitlines()
    i, n = 0, len(lines)
    files = []

    def read_file_block(start):
        """Read a single file block from start index
        Hope start is at 'diff --git a/foo b/foo' line.
        @@ -l,s +l,s @@
        """
        old_path = new_path = None
        is_new = is_delete = False
        hunks = []

        # skip until see --- / +++
        while start < n and not lines[start].startswith("--- "):
            start += 1
        if start >= n:
            return start, None

        # line ---
        m = re.match(r"^---\s+(.*)$", lines[start])
        start += 1
        old_tok = m.group(1).strip() if m else None

        # line +++
        if start >= n or not lines[start].startswith("+++ "):
            raise ValueError("Bad patch: expected '+++' after '---'")
        m = re.match(r"^\+\+\+\s+(.*)$", lines[start])
        start += 1
        new_tok = m.group(1).strip() if m else None

        if old_tok == "/dev/null":
            is_new = True
            old_path = None
        else:
            old_path = old_tok[2:] if old_tok.startswith("a/") else old_tok

        if new_tok == "/dev/null":
            is_delete = True
            new_path = None
        else:
            new_path = new_tok[2:] if new_tok.startswith("b/") else new_tok

        # read hunks
        while start < n and lines[start].startswith("@@"):
            hdr = lines[start]
            start += 1
            m = re.match(
                r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@.*$",
                hdr.rstrip("\r"),
            )
            if not m:
                raise ValueError(f"Bad hunk header: {hdr}")
            a_l = int(m.group(1))
            a_s = int(m.group(2) or 1)
            b_l = int(m.group(3))
            b_s = int(m.group(4) or 1)
            body = []
            while (
                start < n
                and not lines[start].startswith("@@")
                and not lines[start].startswith("diff --git")
                and not lines[start].startswith("--- ")
            ):
                body.append(lines[start])
                start += 1
            hunks.append((a_l, a_s, b_l, b_s, body))

        return start, {
            "old": old_path,
            "new": new_path,
            "hunks": hunks,
            "is_new": is_new,
            "is_delete": is_delete,
        }

    while i < n:
        if lines[i].startswith("diff --git "):
            i += 1
            # --- / +++ / @@
            i, f = read_file_block(i)
            if f:
                files.append(f)
        elif lines[i].startswith("--- "):
            i, f = read_file_block(i)
            if f:
                files.append(f)
        else:
            i += 1
    return files


def _apply_hunks_strict(
    orig_lines: List[str], hunks: List[Tuple[int, int, int, int, List[str]]]
) -> Tuple[List[str], bool]:
    """
    Strictly apply hunks (no fuzzy matching). orig_lines/returned lines contain newline characters.
    """
    new_lines = orig_lines[:]
    line_offset = 0
    for a_l, a_s, b_l, b_s, body in hunks:
        start = a_l - 1 + line_offset
        cur = start
        replacement = []
        for raw in body:
            if raw.startswith("+"):
                replacement.append(raw[1:] + "\n")
            elif raw.startswith(" "):
                if cur >= len(new_lines):
                    return orig_lines, False
                if new_lines[cur].rstrip("\n") != raw[1:]:
                    return orig_lines, False
                replacement.append(new_lines[cur])
                cur += 1
            elif raw.startswith("-"):
                if cur >= len(new_lines):
                    return orig_lines, False
                if new_lines[cur].rstrip("\n") != raw[1:]:
                    return orig_lines, False
                cur += 1
            else:
                if cur >= len(new_lines):
                    return orig_lines, False
                replacement.append(new_lines[cur])
                cur += 1
        before_len = len(new_lines)
        new_lines[start:cur] = replacement
        after_len = len(new_lines)
        line_offset += (after_len - before_len) - (cur - start)
    return new_lines, True


@mcp.tool()
def apply_git_unified_patch_tool(
    repo_root: str, patch_text: str, record_history: bool = True
) -> str:
    """
    Apply GitHub-style unified diff (multi-file, supports new/delete).
    - repo_root: Repository root directory (e.g., {temp_dir})
    - patch_text: Full patch text (includes diff --git / --- / +++ / @@)

    Notes:
    1. **Strict Hunk Matching**: No fuzzy apply—context lines in patch must exactly match target file.
       - Fix "Hunk failed": Check context lines ( ' ' prefix) for typos/line mismatches, or add more context (3-5 lines).
    2. **Valid Hunk Header**: Must follow `@@ -<start>[,<len>] +<start>[,<len>] @@` format.
       - Fix "Bad hunk header": Never use empty `@@`; specify line ranges (e.g., `@@ -41,11 +41,16 @@`).
    3. **Path Check**: Ensure patch paths are relative to repo_root (no absolute paths).
    """
    try:
        root = os.path.abspath(repo_root)
        if not os.path.isdir(root):
            return f"Error: repo_root not found: {repo_root}"

        files = _parse_git_unified_patch(patch_text)
        if not files:
            head = "\n".join(patch_text.splitlines()[:8])
            return "Error: No file blocks detected in patch.\nHead:\n" + head

        applied = []
        for f in files:
            old_p = f["old"]
            new_p = f["new"]
            is_new = f["is_new"]
            is_delete = f["is_delete"]
            target_rel = new_p if not is_delete else old_p
            if not target_rel:
                return "Error: invalid file block (both paths /dev/null?)"

            target_abs = os.path.abspath(os.path.join(root, target_rel))
            if not target_abs.startswith(root + os.sep):
                return f"Error: path escapes repo_root: {target_rel}"

            # Read original content
            if is_new:
                orig = ""
            else:
                if not os.path.exists(target_abs):
                    return f"Error: target not found for patch: {target_rel}"
                with open(target_abs, "r", encoding="utf-8") as fr:
                    orig = fr.read()
            orig_lines = orig.splitlines(keepends=True)

            # Apply/Delete
            if is_delete:
                # Delete file
                os.remove(target_abs)
                new_lines = []
                ok = True
            else:
                new_lines, ok = _apply_hunks_strict(orig_lines, f["hunks"])

            if not ok:
                return f"Error: Hunk failed for {target_rel}. Ensure exact context lines and correct ranges."

            # Write back new/modified content
            if not is_delete:
                os.makedirs(os.path.dirname(target_abs), exist_ok=True)
                with open(target_abs, "w", encoding="utf-8") as fw:
                    fw.write("".join(new_lines))

            applied.append(
                f"{'A' if is_new else ('D' if is_delete else 'M')} {target_rel}"
            )

            # Record history
            if record_history:
                pkg = os.path.basename(root.rstrip(os.sep))
                server_state.setdefault("modification_history", {})
                server_state["modification_history"].setdefault(pkg, []).append(
                    {
                        "file_path": target_rel,
                        "operation": "git_unified_patch",
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

        return "Success: applied patch\n" + "\n".join(applied)
    except Exception as e:
        return f"Error: {str(e)}"


SUPPORTED_FORMATS = {
    "tar.gz": {"extensions": [".tar.gz", ".tgz"], "mode": "r:gz"},
    "tar.xz": {"extensions": [".tar.xz", ".txz"], "mode": "r:xz"},
    "tar.bz2": {"extensions": [".tar.bz2", ".tbz"], "mode": "r:bz2"},
    "zip": {"extensions": [".zip"], "mode": "r"},
}


def get_archive_format(file_path: str) -> tuple:
    """Determine archive format"""
    for fmt, info in SUPPORTED_FORMATS.items():
        for ext in info["extensions"]:
            if file_path.lower().endswith(ext):
                return (fmt, info["mode"])
    return (None, None)


@mcp.tool()
def extract_archive_tool(package_path: str):
    """Extract archive files in various formats"""
    try:
        archive_path = None
        package_dir = None
        archive_file = None

        if os.path.isfile(package_path):
            fmt, _ = get_archive_format(package_path)
            if fmt:
                archive_path = package_path
                package_dir = os.path.dirname(archive_path)
                archive_file = os.path.basename(archive_path)
            else:
                return f"Error: Unsupported archive format for '{package_path}'"

        elif os.path.isdir(package_path):
            found = False
            for item in os.listdir(package_path):
                item_path = os.path.join(package_path, item)
                if os.path.isfile(item_path):
                    fmt, _ = get_archive_format(item_path)
                    if fmt:
                        archive_path = item_path
                        archive_file = item
                        package_dir = package_path
                        found = True
                        break

            if not found:
                return f"Error: No supported archive in '{package_path}'"

        else:
            return f"Error: Invalid path '{package_path}'"

        extract_dir = os.path.join(package_dir, "extracted")
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        os.makedirs(extract_dir, exist_ok=True)

        fmt, mode = get_archive_format(archive_path)
        if fmt in ["tar.gz", "tar.xz", "tar.bz2"]:
            with tarfile.open(archive_path, mode) as tar:
                tar.extractall(extract_dir)
        elif fmt == "zip":
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(extract_dir)

        return f"Successfully extracted {archive_file} to {extract_dir}"

    except Exception as e:
        return f"Extraction failed: {str(e)}"


@mcp.tool()
def compress_to_archive_tool(package_path: str):
    """Compress extracted directory back to original format"""
    try:
        if not package_path or not isinstance(package_path, str):
            return "Error: package_path must be valid string"

        if "extracted" in package_path:
            return f"Error: package_path should not contain 'extracted'"

        extracted_dir = os.path.join(package_path, "extracted")
        if not os.path.exists(extracted_dir):
            return f"Error: Extracted directory '{extracted_dir}' not found"

        original_archive = None
        original_fmt = None
        for item in os.listdir(package_path):
            item_path = os.path.join(package_path, item)
            if os.path.isfile(item_path):
                fmt, _ = get_archive_format(item_path)
                if fmt:
                    original_archive = item_path
                    original_fmt = fmt
                    break

        if not original_archive:
            return f"Error: No original archive in '{package_path}'"

        original_filename = os.path.basename(original_archive)
        output_archive = os.path.join(package_path, f"{original_filename}")

        if os.path.exists(output_archive):
            os.remove(output_archive)

        if original_fmt in ["tar.gz", "tar.xz", "tar.bz2"]:
            mode = SUPPORTED_FORMATS[original_fmt]["mode"].replace("r", "w")
            with tarfile.open(output_archive, mode) as tar:
                for item in os.listdir(extracted_dir):
                    item_path = os.path.join(extracted_dir, item)
                    tar.add(item_path, arcname=item)
        elif original_fmt == "zip":
            with zipfile.ZipFile(output_archive, "w", zipfile.ZIP_DEFLATED) as zip_ref:
                for root, _, files in os.walk(extracted_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, extracted_dir)
                        zip_ref.write(file_path, arcname)

        shutil.rmtree(extracted_dir)
        return f"Success: Compressed to {output_archive}"

    except Exception as e:
        return f"Compression failed: {str(e)}"


@mcp.tool()
def upload_file_to_obs_tool(package_path: str):
    """Upload repaired package to OBS"""
    if not os.path.isdir(package_path):
        return f"Error: '{package_path}' is not a directory"

    has_spec = any(
        f.endswith(".spec")
        for f in os.listdir(package_path)
        if os.path.isfile(os.path.join(package_path, f))
    )
    if not has_spec:
        return f"Error: No .spec file in '{package_path}'"

    package_name = os.path.basename(package_path)
    try:
        obs_result = main_upload(package_name, package_path)
        if "error" in str(obs_result).lower():
            return f"Upload failed: {obs_result}"
        return f"Upload successful. Result: {obs_result}"
    except Exception as e:
        return f"Upload error: {str(e)}"


@mcp.tool()
def check_build_result(input_dir: str, package_name: str):
    """Check build result in OBS"""
    try:
        obs_result = check_main(input_dir, package_name)
        return f"Build result: {obs_result}"
    except Exception as e:
        return f"Build check error: {str(e)}"


@mcp.tool()
def get_packages_to_process(base_dir: str, result_dir: str) -> str:
    """
    obtain the packages to process
    Args:
        base_dir: the base directory
        result_dir: the result storge directory
    Returns:
        a JSON string, containing the package list
    """
    try:
        if not os.path.exists(base_dir):
            return json.dumps(
                {
                    "success": False,
                    "message": f"Base directory not found: {base_dir}",
                    "packages": [],
                }
            )

        packages = [
            item
            for item in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, item))
        ]

        return json.dumps(
            {
                "success": True,
                "message": f"Found {len(packages)} packages",
                "packages": packages,
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "message": f"Error getting packages: {str(e)}",
                "packages": [],
            }
        )


if __name__ == "__main__":
    mcp.run(transport="stdio")
