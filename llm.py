"""Pluggable LLM layer: plug in any provider's key and the agent works.

Claude uses the native Anthropic SDK. Every other provider goes through the OpenAI-compatible
Chat Completions API, which OpenAI, Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together,
Ollama, vLLM and LM Studio all serve. One adapter covers them all.

Config comes from environment variables, or from support-agent/.env written by `--setup`
(environment variables win):
  LLM_PROVIDER   anthropic | openai | gemini | ... | custom   (optional: auto-detected from the key you set)
  LLM_MODEL      model ID                                       (optional for anthropic)
  LLM_BASE_URL   any OpenAI-compatible endpoint                 (implies provider=custom)
  LLM_API_KEY    key for custom endpoints; otherwise the provider's own variable, e.g. OPENAI_API_KEY
"""
import getpass
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / ".env"
CLAUDE_DEFAULT = "claude-opus-5-5"

# name: (key env var, base URL). Order = auto-detect priority.
PROVIDERS = {
    "anthropic":  ("ANTHROPIC_API_KEY", None),
    "openai":     ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "gemini":     ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    "groq":       ("GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    "deepseek":   ("DEEPSEEK_API_KEY", "https://api.deepseek.com"),
    "mistral":    ("MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
    "xai":        ("XAI_API_KEY", "https://api.x.ai/v1"),
    "together":   ("TOGETHER_API_KEY", "https://api.together.xyz/v1"),
    "ollama":     (None, "http://localhost:11434/v1"),
    "custom":     ("LLM_API_KEY", None),
}
NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|image|audio|moderation|realtime|transcribe|rerank|guard|search", re.I)


@dataclass
class Config:
    provider: str
    model: str
    key: str | None
    base_url: str | None


@dataclass
class Call:
    id: str
    name: str
    args: dict = field(default_factory=dict)
    error: str = ""  # set when the model sent arguments that are not a JSON object


def _file_values() -> dict:
    vals = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("'\"")
    return vals


def load_config() -> Config | None:
    """Resolve the provider from env vars + .env. None means nothing is configured yet.
    Values stay in this object; they are NOT copied into os.environ, so the MCP server's
    shell commands (and the model reading their output) never see the agent's key."""
    vals = {**_file_values(), **os.environ}
    get = lambda k: vals.get(k) if k else None
    provider = get("LLM_PROVIDER") or ("custom" if get("LLM_BASE_URL") else None)
    if not provider:
        provider = next((p for p, (var, _) in PROVIDERS.items() if var and p != "custom" and get(var)), None)
    if not provider and get("GOOGLE_API_KEY"):
        provider = "gemini"
    if not provider:
        return None
    if provider not in PROVIDERS:
        sys.exit(f"Unknown LLM_PROVIDER {provider!r}. Choose one of: {', '.join(PROVIDERS)}")
    var, url = PROVIDERS[provider]
    key = get("LLM_API_KEY") or get(var) or (get("GOOGLE_API_KEY") if provider == "gemini" else None)
    model = get("LLM_MODEL") or (CLAUDE_DEFAULT if provider == "anthropic" else "")
    return Config(provider, model, key, get("LLM_BASE_URL") or url)


def list_models(cfg: Config) -> list[str]:
    """Live model list from the provider. Also proves the key works."""
    if cfg.provider == "anthropic":
        import anthropic
        return [m.id for m in anthropic.Anthropic(api_key=cfg.key).models.list()]
    import openai
    ids = [m.id for m in openai.OpenAI(api_key=cfg.key or "none", base_url=cfg.base_url).models.list()]
    return sorted(i for i in ids if not NOT_CHAT.search(i))


def setup() -> Config:
    """Interactive: pick a provider, paste a key, pick a model from the live list. Saved to .env."""
    names = list(PROVIDERS)
    print("Set up the LLM for the support agent.\n")
    for i, n in enumerate(names, 1):
        print(f"  {i:>2}. {n}")
    choice = input("\nProvider number: ").strip()
    provider = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) else sys.exit("No provider chosen.")
    var, url = PROVIDERS[provider]
    if provider == "custom":
        url = input("OpenAI-compatible base URL (e.g. http://localhost:8000/v1): ").strip()
    key = getpass.getpass(f"{var} (input hidden): ").strip() if var else None
    cfg = Config(provider, CLAUDE_DEFAULT if provider == "anthropic" else "", key or None, url)

    try:
        models = list_models(cfg)
        print(f"\nKey works. {len(models)} models available.")
    except Exception as e:
        print(f"\nCould not list models ({type(e).__name__}: {e}).")
        if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
            sys.exit("The key was rejected. Run --setup again with a valid key.")
        models = []
    if models and provider != "anthropic":
        for i, m in enumerate(models[:40], 1):
            print(f"  {i:>2}. {m}")
        if len(models) > 40:
            print(f"  ... {len(models) - 40} more (type the ID)")
    pick = input(f"\nModel number or ID{f' [Enter = {cfg.model}]' if cfg.model else ''}: ").strip()
    if pick.isdigit() and models and 1 <= int(pick) <= min(len(models), 40):
        cfg.model = models[int(pick) - 1]
    elif pick:
        cfg.model = pick
    if not cfg.model:
        sys.exit("No model chosen.")

    save = {"LLM_PROVIDER": provider, "LLM_MODEL": cfg.model}
    if var and key:
        save[var] = key
    if provider == "custom":
        save["LLM_BASE_URL"] = url
    vals = {**_file_values(), **save}
    CONFIG_FILE.write_text("".join(f"{k}={v}\n" for k, v in vals.items()))
    CONFIG_FILE.chmod(0o600)  # holds an API key
    print(f"Saved to {CONFIG_FILE} (readable only by you).\n")
    return cfg


def resolve(interactive: bool) -> Config:
    """Config from env/.env; runs setup when something is missing and a human is at the keyboard."""
    cfg = load_config()
    if cfg and cfg.model:
        return cfg
    if not interactive:
        if cfg:
            sys.exit(f"Found an API key for {cfg.provider} but no model. Set LLM_MODEL, or run `python agent.py --setup` to pick from the live list.")
        sys.exit("No LLM configured. Run `python agent.py --setup`, or set e.g. ANTHROPIC_API_KEY / "
                 "OPENAI_API_KEY (+ LLM_MODEL), or LLM_BASE_URL for a local server.")
    if cfg and not cfg.model:  # key found but no model chosen: offer the live list
        try:
            models = list_models(cfg)
            for i, m in enumerate(models[:40], 1):
                print(f"  {i:>2}. {m}")
            pick = input(f"{cfg.provider}: which model? (number or ID): ").strip()
            cfg.model = models[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= min(len(models), 40) else pick
        except Exception as e:
            cfg.model = input(f"Could not list models ({e}). Model ID: ").strip()
        if cfg.model:
            return cfg
    return setup()


def _strip_titles(schema):
    """Pydantic adds 'title' everywhere; some OpenAI-compatible servers reject unknown schema keys."""
    if isinstance(schema, dict):
        return {k: _strip_titles(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema


def _trim_old(results: list[dict], key: str, budget: int = 200_000, keep: int = 12):
    """Stop history growing without bound: once tool output passes `budget` chars, shrink all but the
    newest `keep` results. Trimming in one batch (not every step) keeps the cached prefix stable."""
    # ponytail: char budget, not tokens; count tokens if a model with a small window needs it
    if sum(len(r[key]) for r in results) <= budget:
        return
    for r in results[:-keep]:
        if len(r[key]) > 400:
            r[key] = r[key][:400] + "\n...[older output trimmed; call the tool again if you need it]"


class Claude:
    """Native Anthropic backend."""

    def __init__(self, cfg: Config, system: str, tools: list[dict]):
        import anthropic
        self.client = anthropic.Anthropic(api_key=cfg.key)  # key None -> SDK finds env / `ant auth login` profile
        self.model, self.system, self.messages = cfg.model, system, []
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
        # Tuned options are only sent for the default model; other Claude models may not accept them all.
        self.extra = dict(output_config={"effort": "high"}, betas=["server-side-fallback-2026-07-01"],
                          fallbacks="default") if cfg.model == CLAUDE_DEFAULT else {}

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def step(self) -> tuple[list[str], list[Call], str]:
        """One model call. Returns (texts, tool calls, status: tools | done | refused | again)."""
        r = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=self.system, tools=self.tools, messages=self.messages,
            cache_control={"type": "ephemeral"},  # tools + system + history are re-sent every step
            **self.extra)
        self.messages.append({"role": "assistant", "content": r.content})
        texts = [b.text for b in r.content if b.type == "text" and b.text.strip()]
        calls = [Call(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        status = {"refusal": "refused", "pause_turn": "again", "tool_use": "tools"}.get(r.stop_reason, "done")
        return texts, calls, status

    def add_results(self, results: list[tuple[Call, str, bool]]):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": out, "is_error": err} for c, out, err in results]})
        _trim_old([b for m in self.messages if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"], "content")


class OpenAICompatible:
    """Backend for any server that speaks OpenAI Chat Completions with tool calling."""

    def __init__(self, cfg: Config, system: str, tools: list[dict]):
        import openai
        # ponytail: fixed 3-minute timeout; some endpoints hang on bad model aliases instead of erroring
        self.client = openai.OpenAI(api_key=cfg.key or "none", base_url=cfg.base_url, timeout=180, max_retries=1)
        self.model = cfg.model
        self.messages = [{"role": "system", "content": system}]
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": _strip_titles(t["schema"])}} for t in tools]

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})

    def step(self) -> tuple[list[str], list[Call], str]:
        msg = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools).choices[0].message
        # Keep tool_call extras as sent (Gemini puts thought signatures there and needs them back).
        # Some servers reject "" next to tool_calls and want null.
        self.messages.append({"role": "assistant", "content": msg.content or (None if msg.tool_calls else ""),
                              **({"tool_calls": [tc.model_dump(exclude_none=True) for tc in msg.tool_calls]} if msg.tool_calls else {})})
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
                calls.append(Call(tc.id, tc.function.name, args) if isinstance(args, dict)
                             else Call(tc.id, tc.function.name, error="arguments must be a JSON object"))
            except json.JSONDecodeError as e:
                calls.append(Call(tc.id, tc.function.name, error=f"invalid JSON arguments: {e}"))
        texts = [msg.content] if msg.content and msg.content.strip() else []
        if getattr(msg, "refusal", None):
            return [msg.refusal], [], "refused"
        return texts, calls, "tools" if calls else "done"

    def add_results(self, results: list[tuple[Call, str, bool]]):
        # Chat Completions has no is_error flag, so failures are marked in the text.
        self.messages += [{"role": "tool", "tool_call_id": c.id, "content": f"ERROR: {out}" if err else out}
                          for c, out, err in results]
        _trim_old([m for m in self.messages if m["role"] == "tool"], "content")


def make_backend(cfg: Config, system: str, tools: list[dict]):
    return (Claude if cfg.provider == "anthropic" else OpenAICompatible)(cfg, system, tools)
