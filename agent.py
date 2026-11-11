"""AI support agent: an LLM tool loop over the project-support MCP server.

Usage:  uv run python agent.py --setup                          (pick provider, paste key, pick model)
        uv run python agent.py /path/to/project ["first message"]

Works with Claude, OpenAI, Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together, Ollama,
or any OpenAI-compatible server (see llm.py). The agent's playbook (system prompt) comes from
the MCP server's `instructions`, so every MCP client follows the same process.
Any tool the server does not mark read-only waits for your y/n before it runs.
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import llm

HERE = Path(__file__).parent
MAX_STEPS = 40  # tool-call rounds per user message; stops a model that loops forever


def approve(name: str, args: dict) -> bool:
    print(f"\n  [needs approval] {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
    try:
        return input("  run it? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


async def main():
    if "--setup" in sys.argv:
        llm.setup()
        return
    project = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    first = sys.argv[2] if len(sys.argv) > 2 else None
    cfg = llm.resolve(interactive=sys.stdin.isatty())
    server = StdioServerParameters(command=sys.executable, args=[str(HERE / "mcp_server.py")],
                                   env={**os.environ, "PROJECT_ROOT": str(project)})

    async with stdio_client(server) as (read, write), ClientSession(read, write) as mcp:
        init = await mcp.initialize()
        listed = (await mcp.list_tools()).tools
        tools = [{"name": t.name, "description": t.description or "", "schema": t.input_schema} for t in listed]
        gated = {t.name for t in listed if not (t.annotations and t.annotations.read_only_hint)}
        model = llm.make_backend(cfg, f"{init.instructions}\n\nProject root: {project}", tools)
        print(f"Support agent for {project.name} | {cfg.provider}:{cfg.model} | {len(tools)} tools. "
              f"Describe your problem. Ctrl+D to quit.")

        while True:
            try:
                user = first or input("\nyou> ")
            except (EOFError, KeyboardInterrupt):
                return
            first = None
            if not user.strip():
                continue
            model.add_user(user)

            for _ in range(MAX_STEPS):  # agent turn: keep calling tools until the model answers
                try:
                    texts, calls, status = model.step()
                except Exception as e:  # both SDKs name auth failures the same way
                    if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                        print(f"The {cfg.provider} key was rejected. Run `python agent.py --setup`.")
                        return
                    print(f"\nagent> LLM error ({type(e).__name__}: {e}). Send your message again to retry.")
                    break
                for t in texts:
                    print(f"\nagent> {t}")
                if status == "refused":
                    print("\nagent> (the model declined this request)")
                    break
                if status == "again":
                    continue
                if status == "done":
                    break

                results = []
                for c in calls:
                    if c.error:
                        results.append((c, c.error, True))
                    elif c.name in gated and not approve(c.name, c.args):
                        results.append((c, "The user declined this action. Ask what they prefer or try another way.", True))
                    else:
                        print(f"  [tool] {c.name}")
                        try:
                            r = await mcp.call_tool(c.name, c.args)
                            text = "\n".join(x.text for x in r.content if x.type == "text") or "(no output)"
                            results.append((c, text, bool(r.is_error)))
                        except Exception as e:  # a broken tool call must not kill the support session
                            results.append((c, f"Tool error: {e}", True))
                model.add_results(results)
            else:
                print(f"\nagent> Stopped after {MAX_STEPS} tool rounds. Tell me how to continue.")


if __name__ == "__main__":
    asyncio.run(main())
