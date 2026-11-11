"""Self-check: diagnose and fix the demo project's broken upgrade without an LLM.
Run: uv run python test_server.py"""
import shutil
import tempfile
from pathlib import Path

import mcp_server as m

# version ranges
assert m.satisfies("20.11.0", ">=18") and not m.satisfies("16.2.0", ">=18")
assert m.satisfies("3.12.1", ">=3.10, <4") and not m.satisfies("4.0.0", ">=3.10,<4")
assert m.satisfies("20.1.0", "^20") and not m.satisfies("21.0.0", "^20")
assert m.satisfies("3.11.4", "~=3.11") and m.satisfies("18.3.0", "16 || 18") and not m.satisfies("17.0.0", "16 || 18")
assert m.satisfies("3.12.0", "3.12") and not m.satisfies("3.11.9", "3.12") and m.satisfies("20.0.0", ">= 18")

with tempfile.TemporaryDirectory() as tmp:
    m.ROOT = Path(shutil.copytree(Path(__file__).parent / "demo-project", Path(tmp) / "p")).resolve()

    # diagnose: env check + release notes point at the rename
    env = m.check_environment()
    assert "FAIL .env missing keys: APP_SECRET" in env and "SECRET_KEY" in env, env
    assert "renamed to `APP_SECRET`" in m.generate_docs("changes")
    assert "[exit 1]" in m.run_command("python3 app.py --check")
    assert "startup failed" in m.read_logs(grep="startup")

    # secrets never reach the model; paths can't escape the project
    assert "s3cr3t" not in m.read_file(".env") and "s3cr3t" not in m.search_docs("SECRET_KEY")
    try:
        m.read_file("../../etc/passwd")
        raise AssertionError("path escape allowed")
    except m.ToolError:
        pass
    assert "Refused" in m.edit_file(".env", "a", "b")
    assert "s3cr3t" not in m.run_command("cat .env")  # command output is scrubbed too
    (m.ROOT / "logs" / "app.log").write_text("db connect password=hunter22 failed\n")
    assert "hunter22" not in m.read_logs()
    assert "Refused" in m.edit_env_file("X", "1\nEVIL=1") and "Refused" in m.edit_env_file("A B", "1")
    assert "Skipped" in m.run_install_step(2)  # would overwrite the user's .env

    # fix + verify: rename keeps the value, app starts, env check is clean
    assert "Renamed" in m.edit_env_file("APP_SECRET", rename_from="SECRET_KEY")
    assert "APP_SECRET=s3cr3t-from-v1.1" in (m.ROOT / ".env").read_text()
    assert "[exit 0]" in m.run_command("python3 app.py --check")
    assert "FAIL" not in m.check_environment()

print("all checks passed")
