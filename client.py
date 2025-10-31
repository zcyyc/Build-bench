import asyncio
import os
import json
import time
import traceback
from typing import Dict, List, Optional, Tuple
from contextlib import AsyncExitStack
import yaml
from dotenv import load_dotenv
from openai import OpenAI
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters

load_dotenv(".env")
with open("config/info.yaml", "r") as f:
    info = yaml.safe_load(f)


class LLMConfig:
    """
    Unified config so we can swap providers/models without changing client logic.

    Priority for credentials per provider:
      - OPENAI_* for native OpenAI (e.g., gpt-5-mini)
      - DASHSCOPE_* for Qwen (DashScope)
      - CHATANYWHERE_* for Claude (OpenAI-compatible facade)

    You can also override via explicit arguments when constructing AutoRepairClientUnified.
    """

    def __init__(
        self,
        provider: str,  # "openai", "qwen", or "claude"
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.provider = provider.lower()
        self.model = model

        if api_key and base_url:
            self.api_key = api_key
            self.base_url = base_url
        else:
            self.api_key = os.getenv("API_KEY")
            self.base_url = os.getenv("API_BASE_URL")

        if not self.api_key:
            raise RuntimeError(
                f"Missing API key for provider={self.provider}. Check your environment.")

    def make_client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)


def make_args_key(tool_name: str, tool_args: dict) -> str:
    return f"{tool_name}::{json.dumps(tool_args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"


class AutoRepairClient:
    """
    - Connects to MCP server
    - Lists tools (OpenAI function-call format)
    - Iterates packages; for each package, performs N build attempts
    - Within each attempt, runs an LLM<->Tools loop handling tool calls
    - Features from both originals:
        * repeat-call guard + caching of tool results
        * upload-before-check-build enforcement
        * fallback auto-upload if model doesn't upload
    """

    def __init__(
        self,
        llm: Optional[LLMConfig] = None,
        base_dir: Optional[str] = None,
        server_script: str = "server.py",
        max_retries: int = 2,
        max_build_attempts: int = 3,
        max_tool_rounds: int = 20,
    ) -> None:
        self.exit_stack = AsyncExitStack()
        self.session: Optional[ClientSession] = None
        self.is_session_active = False

        # LLM adapter
        self.llm_cfg = llm
        self.client = self.llm_cfg.make_client()

        # Paths (choose sensible default if not given)
        
        self.base_dir = base_dir or info["paths"]["base_dir"]
        self.result_dir = info["paths"]["result_dir"]
        self.log_dir = info["paths"]["log_dir"]
        self.temp_work_dir = info["paths"]["temp_work_dir"]

        os.makedirs(self.result_dir, exist_ok=True)
        os.makedirs(self.temp_work_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        self.server_script = server_script
        self.max_retries = max_retries
        self.max_build_attempts = max_build_attempts
        self.max_tool_rounds = max_tool_rounds

        # Per-package state
        self.upload_status: Dict[str, bool] = {}

    def _log(self, tag: str, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        path = os.path.join(self.log_dir, f"{tag}.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
        print(msg)

    async def connect(self, attempt: int = 1) -> bool:
        self._log("global", f"Connecting to MCP server... (attempt {attempt})")
        try:
            params = StdioServerParameters(
                command="uv", args=["run", self.server_script]
            )
            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(params)
            )
            stdio, write = stdio_transport
            self.session = await self.exit_stack.enter_async_context(
                ClientSession(stdio, write)
            )
            await self.session.initialize()
            self.is_session_active = True
            self._log("global", "Connected to MCP server.")
            return True
        except Exception as e:
            self._log("global", f"Connect failed: {e}")
            if attempt < self.max_retries:
                await asyncio.sleep(3)
                return await self.connect(attempt + 1)
            return False

    async def list_tools_openai_format(self) -> List[Dict]:
        assert self.session is not None
        resp = await self.session.list_tools()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema,
                    "input_schema": t.inputSchema,
                },
            }
            for t in resp.tools
        ]
        return tools

    async def process_all_packages(self):
        if not self.is_session_active and not await self.connect():
            self._log("global", "Cannot connect to MCP server, exit.")
            return

        assert self.session is not None
        # Query server for packages to process
        pkg_resp = await self.session.call_tool(
            "get_packages_to_process",
            {"base_dir": self.base_dir, "result_dir": self.result_dir},
        )
        pkg_info = json.loads(pkg_resp.content[0].text)
        if not pkg_info.get("success"):
            self._log("global", f"Get packages failed: {pkg_info.get('message')}")
            return

        packages = pkg_info.get("packages", [])
        self._log("global", f"Found {len(packages)} packages.")

        tools = await self.list_tools_openai_format()
        # Prevent LLM from re-invoking init tool (we invoke it explicitly below)
        tools = [
            t
            for t in tools
            if t.get("function", {}).get("name") != "init_package_environment_tool"
        ]

        for idx, pkg in enumerate(packages, 1):
            self._log("global", f"\n=== [{idx}/{len(packages)}] {pkg} ===")
            try:
                await self.process_one_package(pkg, tools)
            except Exception as e:
                self._log(pkg, f"Fatal error: {e}\n{traceback.format_exc()}")

    async def process_one_package(self, package_name: str, tools: List[Dict]):
        assert self.session is not None

        # Reset upload status for hard dependency enforcement
        self.upload_status[package_name] = False

        # Initialize temp env (copy package -> temp dir)
        init_ret = await self.session.call_tool(
            "init_package_environment_tool",
            {
                "base_dir": self.base_dir,
                "package_name": package_name,
                "temp_work_dir": self.temp_work_dir,
                "result_dir": self.result_dir,
            },
        )
        init_data = json.loads(init_ret.content[0].text)
        if not init_data.get("success"):
            self._log(package_name, f"Init failed: {init_data.get('message')}")
            return

        package_path = init_data["package_path"]
        result_file = init_data["result_file"]

        # Load system prompt template
        with open("prompts/full_file_generation.txt", "r", encoding="utf-8") as f:
            system_prompt_tpl = f.read()

        build_succeeded = False
        final_text = ""

        # Multiple build attempts
        for attempt in range(1, self.max_build_attempts + 1):
            self._log(
                package_name,
                f"--- Build attempt {attempt}/{self.max_build_attempts} ---",
            )
            try:
                await self.session.call_tool(
                    "reset_package_cache_tool", {"package_name": package_name}
                )
            except Exception as e:
                self._log(package_name, f"Cache clear failed on attempt {attempt}: {e}")

            upd = await self.session.call_tool(
                "update_prompt_with_history_tool",
                {
                    "package_name": package_name,
                    "package_path": package_path,
                    "build_attempt": attempt,
                    "formatted_prompt": system_prompt_tpl.format(
                        package_name=package_name,
                        file_name=result_file,
                        temp_dir=package_path,
                    ),
                },
            )
            messages = json.loads(upd.content[0].text)["messages"]

            # Ensure the first message is a system message for OpenAI-chat format
            if messages and messages[0].get("role") != "system":
                messages.insert(0, {"role": "system", "content": system_prompt_tpl})

            content, ok = await self._llm_tools_loop(
                package_name, package_path, messages, tools
            )
            if ok:
                build_succeeded = True
                final_text = f"Build succeeded on attempt {attempt}.\n{content or ''}"
                break
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": f"Build failed after attempt {attempt}. Continue analyzing and repairing, then retry.",
                    }
                )
                final_text = f"Build failed on attempt {attempt}.\n{content or ''}"

        with open(result_file, "w", encoding="utf-8") as f:
            f.write(final_text)
        self._log(package_name, f"Final saved to {result_file}")
        if not build_succeeded:
            self._log(package_name, "Max attempts reached without success.")

    async def _llm_tools_loop(
        self,
        package_name: str,
        package_path: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> Tuple[str, bool]:
        choice = None
        latest_text = ""
        rounds = 0
        did_upload = False

        # Initial model step
        try:
            resp = self.client.chat.completions.create(
                model=self.llm_cfg.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            choice = resp.choices[0]
            latest_text = choice.message.content or ""
        except Exception as e:
            self._log(package_name, f"Model call failed: {e}")
            return f"Model call failed: {e}", False

        while rounds < self.max_tool_rounds and choice.finish_reason in (
            "tool_calls",
            None,
        ):
            rounds += 1
            self._log(package_name, f"== Tool round {rounds} ==")

            for tc in choice.message.tool_calls or []:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                self._log(
                    package_name,
                    f"Tool call: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:500]})",
                )

                # Repeat guard
                args_key = make_args_key(tool_name, tool_args)
                repeat_check = await self.session.call_tool(
                    "check_repeat_tool_call",
                    {
                        "tool_name": tool_name,
                        "args_key": args_key,
                        "max_repeat": 3,
                        "package_name": package_name,
                    },
                )
                repeat_allowed = json.loads(repeat_check.content[0].text).get(
                    "allowed", True
                )

                # Enforce upload-before-check rule (from client_claude)
                if tool_name == "check_build_result" and not self.upload_status.get(
                    package_name, False
                ):
                    tool_ret = (
                        "ERROR: Cannot call check_build_result before uploading. "
                        "You must call upload_file_to_obs_tool first."
                    )
                    # Feed back the error as tool result
                    messages.append(
                        {
                            "role": "assistant",
                            "content": choice.message.content,
                            "tool_calls": [
                                t.model_dump()
                                for t in (choice.message.tool_calls or [])
                            ],
                        }
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_ret}
                    )
                    continue

                if not repeat_allowed:
                    # Block and nudge
                    tool_ret = json.loads(repeat_check.content[0].text).get(
                        "message", "repeated call blocked"
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Do not call {tool_name} again in this attempt. Continue with code changes or other tools.",
                        }
                    )
                else:
                    # Cache lookup
                    cache = await self.session.call_tool(
                        "check_tool_cache",
                        {
                            "call_key": args_key,
                            "tool_name": tool_name,
                            "package_name": package_name,
                        },
                    )
                    cache_data = json.loads(cache.content[0].text)
                    if cache_data.get("hit"):
                        tool_ret = cache_data["result"]
                    else:
                        try:
                            res = await asyncio.wait_for(
                                self.session.call_tool(tool_name, tool_args),
                                timeout=600,
                            )
                            tool_ret = res.content[0].text
                            self._log(
                                package_name, f"Tool return text: {tool_ret[:1000]}"
                            )
                            # Cache only for safe/beneficial tools (example from original)
                            if (
                                tool_name in ["modify_file_tool"]
                                and "error" not in tool_ret.lower()
                            ):
                                await self.session.call_tool(
                                    "cache_tool_result",
                                    {
                                        "call_key": args_key,
                                        "result": tool_ret,
                                        "package_name": package_name,
                                    },
                                )
                        except asyncio.TimeoutError:
                            tool_ret = f"Error: Tool {tool_name} timed out"
                        except Exception as e:
                            tool_ret = f"Error: Tool {tool_name} failed: {e}"

                    # Record history of tool calls regardless of cache
                    await self.session.call_tool(
                        "record_tool_call_history",
                        {"call_key": args_key, "package_name": package_name},
                    )

                if tool_name in [
                    "upload_file_to_obs_tool",
                    "upload_file_to_obs_tool_deepseek",
                ]:
                    if (
                        "successful" in tool_ret.lower()
                        or "success" in tool_ret.lower()
                    ):
                        self.upload_status[package_name] = True
                        did_upload = True
                        self._log(package_name, "✓ Upload marked as successful")

                # Feed back results to the model
                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.message.content,
                        "tool_calls": [
                            t.model_dump() for t in (choice.message.tool_calls or [])
                        ],
                    }
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_ret}
                )

                # If it's a build verification, parse immediately
                if tool_name == "check_build_result":
                    parsed = await self.session.call_tool(
                        "parse_build_result_tool",
                        {"result_content": tool_ret, "package_name": package_name},
                    )
                    if json.loads(parsed.content[0].text).get("success"):
                        return latest_text, True

            # next model step
            try:
                resp = self.client.chat.completions.create(
                    model=self.llm_cfg.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                choice = resp.choices[0]
                latest_text = choice.message.content or latest_text
            except Exception as e:
                self._log(package_name, f"Model continuation failed: {e}")
                break

        # Fallback: enforce upload & (optionally) check if model forgot
        if not did_upload:
            try:
                up_res = await self.session.call_tool(
                    "upload_file_to_obs_tool", {"package_path": package_path}
                )
                up_txt = up_res.content[0].text
                self._log(
                    package_name,
                    f"[fallback] upload_file_to_obs_tool => {up_txt[:300]}",
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": "fallback_upload",
                        "content": up_txt,
                    }
                )
            except Exception as e:
                self._log(package_name, f"[fallback] upload failed: {e}")

        return latest_text, False

    async def cleanup(self):
        try:
            await self.exit_stack.aclose()
        except Exception as e:
            self._log("global", f"Cleanup error: {e}")
        self.is_session_active = False
        self.session = None
        self._log("global", "Cleanup completed.")


async def main():
    # Choose provider + model here.
    provider = info["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": "qwen3-max",
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)

    llm_cfg = LLMConfig(provider=provider, model=default_model)
    cli = AutoRepairClient(llm=llm_cfg)
    try:
        await cli.process_all_packages()
    finally:
        await cli.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
