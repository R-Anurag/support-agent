"""Support MCP server: exposes ANY project's knowledge and actions as MCP tools.

Point it at a project with PROJECT_ROOT=/path/to/project. Nothing is cached or
pre-indexed: every tool reads the project as it is on disk right now, so the
answers change the moment the code, configs or release notes change.

Run:  PROJECT_ROOT=../demo-project python mcp_server.py           (stdio MCP server)
      PROJECT_ROOT=../demo-project python mcp_server.py --docs     (print generated docs)
"""
import datetime
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # message reaches the model, unlike other exceptions
from mcp.types import ToolAnnotations

ROOT = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
             ".next", "target", ".tox", ".mypy_cache", ".pytest_cache", ".support-agent"}
TEXT_EXT = {".md", ".rst", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
            ".rb", ".php", ".sh", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ""}
DOC_NAMES = re.compile(r"(readme|changelog|changes|history|release|contributing|install|faq|docs?/)", re.I)
SECRET_LINE = re.compile(r"(?i)^(\s*[\w.-]*(secret|token|password|passwd|api[_-]?key|private[_-]?key)[\w.-]*\s*[=:]\s*)(.+)$")
SECRET_INLINE = re.compile(r"(?i)\b([\w.-]*(?:secret|token|password|passwd|api[_-]?key|private[_-]?key)[\w.-]*[\"']?\s*[=:]\s*[\"']?)([^\s,;'\"&]+)")
CONFIG_EXT = {".ini", ".yaml", ".yml", ".toml", ".json", ".cfg", ".conf", ".properties"}

PLAYBOOK = """You are a support engineer for the project this server points at. Take the user from
"I have a problem" to "problem solved" yourself. Do not hand off to a human.

How to work:
1. Understand: restate the goal or symptom in one line. Ask a question only if you are blocked.
2. Gather evidence BEFORE you guess: check_environment, read_logs, generate_docs("changes"),
   search_docs, read_file. Docs are generated live from the code, so trust them over memory.
3. Diagnose: name the root cause and the evidence that proves it (file:line, log line, version).
   Recent breaking changes in release notes are a common cause after an upgrade.
4. Fix: prefer the smallest change. For install, follow install_plan and run_install_step one
   step at a time, and read each result before the next step. Use edit_env_file for .env
   files (their values are hidden from you), edit_file for other files, run_command otherwise.
5. Verify: re-run the failing command or check_environment. Never say "fixed" without proof.
6. Report: what was wrong, what you changed, how you verified it. Keep it short.

Rules: never ask the user to paste secrets. Never run destructive commands (rm -rf, force push,
dropping data) unless the user asked for exactly that. If a step fails, diagnose it, do not retry
blindly."""

mcp = MCPServer("project-support", instructions=PLAYBOOK)
READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DANGER = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


# ---------- helpers ----------

def _safe(path: str) -> Path:
    """Resolve a project-relative path and refuse anything outside the project."""
    p = (ROOT / path).resolve()
    if not p.is_relative_to(ROOT):
        raise ToolError(f"{path!r} is outside the project root")
    return p


def _walk():
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = Path(dirpath) / f
            if p.resolve().is_relative_to(ROOT):  # a symlink may point outside the project
                yield p


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def _sh(cmd: str, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def _nums(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def satisfies(version: str, spec: str) -> bool:
    """Small version-range check that covers npm and PEP 440 basics:
    '>=18', '^20.1', '~3.11', '~=3.10', '>=3.10,<4', '18 || 20', '20' (prefix match).
    A clause it cannot parse counts as satisfied: a wrong 'broken' verdict costs more than a miss."""
    # ponytail: not full semver (no prereleases, no x-ranges inside clauses); use `packaging`/`semver` if that matters
    v = _nums(version)
    if not v:
        return True

    def clause_ok(c: str) -> bool:
        m = re.match(r"(>=|<=|>|<|==|=|\^|~=|~)?\s*v?(\d+(?:\.\d+)*)", c)
        if not m:
            return True
        op, w = m.group(1) or "", _nums(m.group(2))
        n = len(w)
        pv, pw = v + (0,) * (3 - len(v)), w + (0,) * (3 - n)
        return {
            ">=": pv >= pw, ">": pv > pw, "<=": pv <= pw, "<": pv < pw,
            "": v[:n] == w, "=": v[:n] == w, "==": v[:n] == w,
            "^": pv >= pw and v[0] == w[0],
            "~": pv >= pw and v[:min(n, 2)] == w[:min(n, 2)],
            "~=": pv >= pw and v[:max(n - 1, 1)] == w[:max(n - 1, 1)],
        }[op]

    alts = [re.sub(r"([<>=^~])\s+", r"\1", a.strip()) for a in spec.split("||")]
    return any(all(clause_ok(c) for c in re.split(r"[,\s]+", a) if c) for a in alts)


def _tool_version(tool: str) -> str | None:
    if not shutil.which(tool):
        return None
    flag = {"go": "version", "java": "-version"}.get(tool, "--version")
    _, out = _sh(f"{tool} {flag}", timeout=15)
    m = re.search(r"\d+\.\d+(\.\d+)?", out)
    return m.group(0) if m else "unknown"


def _read_env(path: Path) -> dict[str, tuple[str, str]]:
    """KEY -> (value, comment above it). Handles `export KEY=...` and quotes."""
    out, comment = {}, []
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if s.startswith("#"):
            comment.append(s.lstrip("# "))
        elif "=" in s:
            k, v = s.removeprefix("export ").split("=", 1)
            out[k.strip()] = (v.strip().strip("'\""), " ".join(comment))
            comment = []
        else:
            comment = []
    return out


def _is_secret_env(p: Path) -> bool:
    return p.name.startswith(".env") and not p.name.endswith((".example", ".sample", ".template", ".dist"))


def _redact(p: Path, text: str) -> str:
    if _is_secret_env(p):
        return "\n".join(
            re.sub(r"^(\s*(?:export\s+)?[\w.-]+\s*=\s*)(.+)$", lambda m: m.group(1) + "<hidden>", l)
            for l in text.splitlines())
    if p.suffix in CONFIG_EXT:
        return "\n".join(SECRET_LINE.sub(lambda m: m.group(1) + "<hidden>", l) for l in text.splitlines())
    return text


def _scrub(text: str) -> str:
    """Hide secrets in free-form output (logs, command output): `password=...`-style pairs, plus the
    literal values of secret-named or long .env keys, so `cat .env` or an app echoing config can't leak them."""
    # ponytail: only values of secret-named keys or 16+ chars are matched literally; short secrets under
    # neutral names (e.g. DB_PASS_2=abc) still rely on the inline pattern and the approval gate
    vals = {v for f in ROOT.glob(".env*") if f.is_file() and _is_secret_env(f)
            for k, (v, _) in _read_env(f).items() if len(v) >= 4 and (SECRET_INLINE.match(k + "=x") or len(v) >= 16)}
    for v in sorted(vals, key=len, reverse=True):
        text = text.replace(v, "<hidden>")
    return SECRET_INLINE.sub(lambda m: m.group(1) + "<hidden>", text)


def detect() -> dict:
    """Work out what kind of project this is from the files that exist right now."""
    d = {"name": ROOT.name, "version": "", "stacks": [], "requires": {}, "scripts": {}, "manifests": []}
    has = lambda f: (ROOT / f).exists()

    if has("package.json"):
        pkg = json.loads((ROOT / "package.json").read_text())
        d.update(name=pkg.get("name", d["name"]), version=pkg.get("version", ""))
        d["stacks"].append("node")
        d["scripts"].update({k: v for k, v in pkg.get("scripts", {}).items()})
        d["requires"]["node"] = pkg.get("engines", {}).get("node") or (
            (ROOT / ".nvmrc").read_text().strip().lstrip("v") if has(".nvmrc") else "")
        pm = next((m for m, lock in [("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock"),
                                     ("bun", "bun.lockb"), ("npm", "package-lock.json")] if has(lock)), "npm")
        d["package_manager"] = pm
        d["requires"].setdefault(pm, "")
    if has("pyproject.toml") or has("requirements.txt") or has("setup.py"):
        d["stacks"].append("python")
        proj = tomllib.loads((ROOT / "pyproject.toml").read_text()).get("project", {}) if has("pyproject.toml") else {}
        d["name"], d["version"] = proj.get("name", d["name"]), proj.get("version", d["version"])
        d["requires"]["python3"] = proj.get("requires-python") or (
            (ROOT / ".python-version").read_text().strip() if has(".python-version") else "")
        d["python_deps"] = bool(proj.get("dependencies")) or has("setup.py")
        d["scripts"].update({k: v for k, v in proj.get("scripts", {}).items()})
    if has("go.mod"):
        d["stacks"].append("go")
        m = re.search(r"^go (\S+)", (ROOT / "go.mod").read_text(), re.M)
        d["requires"]["go"] = f">={m.group(1)}" if m else ""
    if has("Cargo.toml"):
        d["stacks"].append("rust")
        pkg = tomllib.loads((ROOT / "Cargo.toml").read_text()).get("package", {})
        d["requires"]["cargo"] = f">={pkg['rust-version']}" if "rust-version" in pkg else ""
    compose = next((f for f in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml") if has(f)), None)
    if has("Dockerfile") or compose:
        d["stacks"].append("docker")
        d["requires"]["docker"] = ""
        d["compose_file"] = compose
    if has("Makefile"):
        d["requires"]["make"] = ""
        targets = re.findall(r"^([A-Za-z0-9][\w.-]*):(?!=)", (ROOT / "Makefile").read_text(), re.M)
        d["scripts"].update({f"make {t}": "Makefile target" for t in targets})
    d["manifests"] = [f for f in ("package.json", "pyproject.toml", "requirements.txt", "setup.py", "go.mod",
                                  "Cargo.toml", "Dockerfile", compose, "Makefile", ".env.example") if f and has(f)]
    return d


def _documented_steps() -> list[str]:
    """Shell commands under the README's install / setup / quick start heading."""
    readme = next((p for p in ROOT.glob("README*") if p.is_file()), None)
    if not readme:
        return []
    text = readme.read_text(errors="replace")
    m = re.search(r"^(#+)\s*(install|installation|setup|getting started|quick ?start)\b.*$", text, re.I | re.M)
    if not m:
        return []
    level = len(m.group(1))
    end = re.search(rf"^#{{1,{level}}}\s", text[m.end():], re.M)
    section = text[m.end(): m.end() + end.start()] if end else text[m.end():]
    cmds = []
    for block in re.findall(r"```(?:bash|sh|shell|console|zsh)?\n(.*?)```", section, re.S):
        cmds += [l.strip().removeprefix("$ ") for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
    return cmds


def _derived_steps(d: dict) -> list[str]:
    """Install steps inferred from manifests, used when the README has none."""
    steps = []
    if (ROOT / ".env.example").exists() and not (ROOT / ".env").exists():
        steps.append("cp .env.example .env")
    if "node" in d["stacks"]:
        pm = d["package_manager"]
        steps.append({"npm": "npm ci" if (ROOT / "package-lock.json").exists() else "npm install",
                      "pnpm": "pnpm install --frozen-lockfile", "yarn": "yarn install --frozen-lockfile",
                      "bun": "bun install"}[pm])
        if "build" in d["scripts"]:
            steps.append(f"{pm} run build")
    if "python" in d["stacks"]:
        pip = ".venv\\Scripts\\pip" if os.name == "nt" else ".venv/bin/pip"
        steps.append(f"{'python' if os.name == 'nt' else 'python3'} -m venv .venv")
        if (ROOT / "requirements.txt").exists():
            steps.append(f"{pip} install -r requirements.txt")
        elif d.get("python_deps"):
            steps.append(f"{pip} install -e .")
    if "go" in d["stacks"]:
        steps += ["go mod download", "go build ./..."]
    if "rust" in d["stacks"]:
        steps.append("cargo build")
    if not steps or steps == ["cp .env.example .env"]:
        if d.get("compose_file"):
            steps.append("docker compose up -d --build")
        elif "make install" in d["scripts"]:
            steps.append("make install")
    return steps


def _plan() -> tuple[list[str], str]:
    doc = _documented_steps()
    return (doc, "README") if doc else (_derived_steps(detect()), "derived from manifests")


def _stamp() -> str:
    code, head = _sh("git rev-parse --short HEAD", 10)
    _, dirty = _sh("git status --porcelain", 10)
    rev = f"commit {head}{' + uncommitted changes' if dirty else ''}" if code == 0 else "no git repo"
    return f"_Generated {datetime.datetime.now():%Y-%m-%d %H:%M} from the working tree ({rev})._"


# ---------- knowledge tools (read-only) ----------

@mcp.tool(annotations=READ)
def generate_docs(topic: Literal["overview", "install", "configuration", "commands", "changes", "all"] = "overview") -> str:
    """Generate documentation for the project from its CURRENT code, manifests, configs, release notes
    and git history. Never stale: it is rebuilt on every call.
    topic: overview | install | configuration | commands | changes (release notes + unreleased commits) | all"""
    d = detect()
    parts = [_stamp()]
    if topic in ("overview", "all"):
        docs = sorted(_rel(p) for p in _walk() if DOC_NAMES.search(_rel(p)) and p.suffix in (".md", ".rst", ".txt"))
        parts.append(f"# {d['name']} {d['version']}\n\nStack: {', '.join(d['stacks']) or 'unknown'}\n"
                     f"Manifests: {', '.join(d['manifests']) or 'none'}\nDoc files: {', '.join(docs[:30]) or 'none'}")
    if topic in ("install", "all"):
        steps, src = _plan()
        req = "\n".join(f"| {t} | {s or 'any'} |" for t, s in d["requires"].items())
        parts.append(f"## Install\n\n| Tool | Required version |\n|---|---|\n{req}\n\n"
                     f"Steps ({src}):\n" + "\n".join(f"{i}. `{s}`" for i, s in enumerate(steps, 1)))
    if topic in ("configuration", "all"):
        env = _read_env(ROOT / ".env.example")
        rows = "\n".join(f"| `{k}` | `{v}` | {c.replace('|', '\\|')} |" for k, (v, c) in env.items())
        cfg = [_rel(p) for p in _walk() if p.suffix in CONFIG_EXT and "config" in _rel(p).lower()]
        parts.append("## Configuration\n\n" + (f"Environment variables (from .env.example):\n\n| Variable | Example | Notes |\n"
                     f"|---|---|---|\n{rows}" if env else "No .env.example found.")
                     + (f"\n\nConfig files: {', '.join(cfg[:20])}" if cfg else ""))
    if topic in ("commands", "all"):
        parts.append("## Commands\n\n" + ("\n".join(f"- `{k}`: {v}" for k, v in d["scripts"].items()) or "None declared."))
    if topic in ("changes", "all"):
        log = next((p for p in ROOT.iterdir() if re.match(r"(changelog|changes|history|releases?)(\.md|\.rst|\.txt)?$", p.name, re.I)), None)
        notes = ""
        if log:
            text = log.read_text(errors="replace")
            if m := re.search(r"^(#{1,3}) .*\d", text, re.M):  # level of the release headings, e.g. "## 1.2.0"
                h = m.group(1)
                notes = "".join([s for s in re.split(rf"(?=^{h} )", text, flags=re.M) if re.match(rf"{h} .*\d", s)][:3])
        code, tag = _sh("git describe --tags --abbrev=0", 10)
        ok, commits = _sh(f"git log {tag + '..HEAD' if code == 0 else '-n 15'} --oneline --no-merges", 10)
        commits = commits if ok == 0 else ""
        parts.append(f"## Recent changes\n\n### Release notes ({log.name if log else 'no changelog'})\n{notes or 'none'}\n\n"
                     f"### Commits {'since ' + tag if code == 0 else '(latest)'}\n{commits or 'none / not a git repo'}")
    return "\n\n".join(parts)


@mcp.tool(annotations=READ)
def search_docs(query: str, max_results: int = 8) -> str:
    """Keyword search across the project's docs, configs and source code. Returns file:line hits with
    context. Doc files rank above code. Use it to answer 'how do I...' and to find where an error string comes from."""
    # ponytail: keyword scoring over a full scan; switch to an embeddings index if the repo is too big to scan per query
    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2] or [query.lower()]
    hits = []
    for p in _walk():
        if p.suffix not in TEXT_EXT or p.stat().st_size > 1_000_000:
            continue
        lines = p.read_text(errors="replace").splitlines()
        weight = 2 if DOC_NAMES.search(_rel(p)) else 1
        for i, line in enumerate(lines):
            score = sum(t in line.lower() for t in terms)
            if score:
                ctx = "\n".join(lines[max(0, i - 1): i + 3])
                hits.append((score * weight, f"{_rel(p)}:{i + 1}\n{_redact(p, ctx)}"))
    hits.sort(key=lambda h: -h[0])
    return "\n\n".join(h for _, h in hits[:max_results]) or f"No matches for {query!r}."


@mcp.tool(annotations=READ)
def read_file(path: str, start_line: int = 1, max_lines: int = 300) -> str:
    """Read a project file (code, config, docs). Secret values in .env and config files are replaced with <hidden>."""
    p = _safe(path)
    if not p.is_file():
        raise ToolError(f"{path} is not a file. Use list_files to find the right path.")
    lines = p.read_text(errors="replace").splitlines()
    chunk = "\n".join(lines[start_line - 1: start_line - 1 + max_lines])
    more = f"\n... ({len(lines)} lines total)" if len(lines) > start_line - 1 + max_lines else ""
    return _redact(p, chunk) + more


@mcp.tool(annotations=READ)
def list_files(pattern: str = "*", limit: int = 200) -> str:
    """List project files matching a glob (e.g. '*.py', 'docs/*'). Skips .git, node_modules, venvs, build output."""
    files = sorted(_rel(p) for p in _walk() if p.match(pattern))
    return "\n".join(files[:limit]) + (f"\n... {len(files) - limit} more" if len(files) > limit else "")


# ---------- diagnosis tools (read-only) ----------

@mcp.tool(annotations=READ)
def check_environment() -> str:
    """Inspect the user's machine against what the project needs: OS, required tools and versions,
    installed dependencies, .env vs .env.example (missing, empty, renamed keys), ports in use, disk space.
    Each line starts with OK, FAIL or WARN."""
    d = detect()
    out = [f"INFO {platform.system()} {platform.release()} {platform.machine()}, project {d['name']} {d['version']} "
           f"({', '.join(d['stacks']) or 'unknown stack'})"]
    for tool, spec in d["requires"].items():
        v = _tool_version(tool)
        if v is None:
            out.append(f"FAIL {tool} not installed (required {spec or 'any version'})")
        elif spec and not satisfies(v, spec):
            out.append(f"FAIL {tool} {v} does not satisfy {spec}")
        else:
            out.append(f"OK   {tool} {v}" + (f" (needs {spec})" if spec else ""))

    if "node" in d["stacks"]:
        nm, lock = ROOT / "node_modules", next((ROOT / f for f in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock") if (ROOT / f).exists()), None)
        if not nm.exists():
            out.append("FAIL node_modules missing: dependencies not installed")
        elif lock and lock.stat().st_mtime > nm.stat().st_mtime:
            out.append(f"WARN {lock.name} is newer than node_modules: dependencies may be stale")
        else:
            out.append("OK   node_modules present")
    if "python" in d["stacks"]:
        out.append("OK   .venv present" if (ROOT / ".venv").exists() else "WARN no .venv: dependencies may not be installed in an isolated env")

    example, env_path = ROOT / ".env.example", ROOT / ".env"
    if example.exists():
        want, have = _read_env(example), _read_env(env_path)
        if not env_path.exists():
            out.append("FAIL .env missing (template: .env.example)")
        else:
            missing = [k for k in want if k not in have and k not in os.environ]
            empty = [k for k, (v, _) in have.items() if k in want and not v]
            unknown = [k for k in have if k not in want]
            out.append(f"FAIL .env missing keys: {', '.join(missing)}" if missing else "OK   .env has every key from .env.example")
            if empty:
                out.append(f"WARN .env keys with empty values: {', '.join(empty)}")
            if unknown:
                out.append(f"WARN .env keys not in .env.example (renamed or removed?): {', '.join(unknown)}")
        env = {**want, **have}
        for k, (v, _) in env.items():
            if "PORT" in k and v.isdigit():
                with socket.socket() as s:
                    try:
                        s.bind(("127.0.0.1", int(v)))
                        out.append(f"OK   port {v} ({k}) is free")
                    except OSError:
                        who = _sh(f"lsof -nP -iTCP:{v} -sTCP:LISTEN | tail -n +2", 10)[1] if shutil.which("lsof") else ""
                        out.append(f"WARN port {v} ({k}) is already in use {who}".strip())

    free = shutil.disk_usage(ROOT).free / 1e9
    out.append(f"{'WARN' if free < 1 else 'OK  '} {free:.1f} GB free disk")
    return "\n".join(out)


@mcp.tool(annotations=READ)
def read_logs(lines: int = 80, grep: str = "") -> str:
    """Tail the project's most recent log files (*.log in the project, the output of commands this server
    ran, and any globs in the LOG_PATHS env var). grep filters lines (regex, case-insensitive).
    Error-looking lines are counted so you can spot failures fast."""
    files = [p for p in _walk() if p.suffix == ".log"] + list((ROOT / ".support-agent").glob("*.log"))
    for g in filter(None, os.environ.get("LOG_PATHS", "").split(os.pathsep)):
        files += [p for p in Path(g).parent.expanduser().glob(Path(g).name) if p.is_file()]
    files = sorted(set(files), key=lambda p: -p.stat().st_mtime)[:5]
    if not files:
        return "No log files found. Run the failing command with run_command to capture its output."
    err = re.compile(r"error|exception|traceback|fatal|panic|refused|denied|EADDRINUSE|not found", re.I)
    out = []
    for p in files:
        text = p.read_text(errors="replace").splitlines()
        sel = [l for l in text if re.search(grep, l, re.I)] if grep else text
        out.append(f"=== {p} ({sum(bool(err.search(l)) for l in text)} error-like lines) ===\n" + _scrub("\n".join(sel[-lines:])))
    return "\n\n".join(out)


# ---------- action tools (change the machine; clients should ask the user first) ----------

@mcp.tool(annotations=READ)
def install_plan() -> str:
    """The ordered install steps for this project: taken from the README's install section if it has one,
    otherwise derived from the manifests. Run them with run_install_step."""
    steps, src = _plan()
    return f"Install plan ({src}):\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) if steps else "No install steps found."


@mcp.tool(annotations=WRITE)
def run_install_step(step: int) -> str:
    """Run one step (1-based) from install_plan in the project directory and return its output."""
    steps, _ = _plan()
    if not 1 <= step <= len(steps):
        return f"No step {step}; the plan has {len(steps)} steps."
    cmd = steps[step - 1]
    if (m := re.fullmatch(r"cp\s+(\S+)\s+(\S+)", cmd)) and _safe(m.group(2)).exists():
        return f"Skipped `{cmd}`: {m.group(2)} already exists and would be overwritten (it may hold the user's secrets)."
    return run_command(cmd, timeout=900)


@mcp.tool(annotations=DANGER)
def run_command(command: str, timeout: int = 300) -> str:
    """Run a shell command in the project directory (reproduce an error, install, start or test the app).
    Output is also saved to .support-agent/last_run.log so read_logs can see it later."""
    code, out = _sh(command, timeout)
    log = ROOT / ".support-agent" / "last_run.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(f"$ {command}\n{out}\n[exit {code}]\n")
    return _scrub(f"$ {command}\n[exit {code}]\n{out[-8000:]}")


@mcp.tool(annotations=WRITE)
def edit_file(path: str, old: str, new: str) -> str:
    """Replace the exact text `old` with `new` in a project file (old must appear exactly once).
    With old="" and a file that does not exist, create it. The previous version is saved as <file>.bak.
    Not for .env files: use edit_env_file."""
    p = _safe(path)
    if _is_secret_env(p):
        return "Refused: .env values are hidden from you, so edit them with edit_env_file."
    if not p.exists():
        if old:
            return f"{path} does not exist."
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new)
        return f"Created {path}."
    text = p.read_text()
    if text.count(old) != 1:
        return f"`old` must appear exactly once in {path}; found {text.count(old)}."
    shutil.copy2(p, p.with_name(p.name + ".bak"))
    p.write_text(text.replace(old, new))
    return f"Edited {path} (backup: {p.name}.bak)."


@mcp.tool(annotations=WRITE)
def edit_env_file(key: str, value: str = "", rename_from: str = "", file: str = ".env") -> str:
    """Change one variable in a .env-style file without seeing the other secrets.
    rename_from="OLD": rename OLD to `key` and KEEP its current value (for renamed settings).
    Otherwise set key=value (replaces the line, or appends it). Backup saved as <file>.bak."""
    if not re.fullmatch(r"[A-Za-z_][\w.]*", key) or (rename_from and not re.fullmatch(r"[A-Za-z_][\w.]*", rename_from)):
        return "Refused: keys must be letters, digits, _ or . only."
    if "\n" in value or "\r" in value:
        return "Refused: value must be a single line."
    p = _safe(file)
    lines = p.read_text().splitlines() if p.exists() else []
    if p.exists():
        shutil.copy2(p, p.with_name(p.name + ".bak"))
    find = rename_from or key
    pat = re.compile(rf"^(\s*(?:export\s+)?){re.escape(find)}(\s*=)(.*)$")
    for i, l in enumerate(lines):
        if m := pat.match(l):
            lines[i] = f"{m.group(1)}{key}{m.group(2)}{m.group(3) if rename_from else value}"
            break
    else:
        if rename_from:
            return f"{rename_from} not found in {file}."
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n")
    return f"{'Renamed ' + rename_from + ' -> ' + key if rename_from else 'Set ' + key} in {file}."


if __name__ == "__main__":
    if "--docs" in sys.argv:
        print(generate_docs("all"))
    else:
        mcp.run()
