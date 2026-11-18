"""Re-run every evidence command and save the output to evidence/*.txt.

    python evidence/generate_evidence.py

Runs in offline mode (PAWPAL_DISABLE_LLM=1) so the results are the same on
any machine and don't need an API key. Logs go to a fresh directory so the
log excerpt only contains this run.
"""

import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evidence"
LOG_DIR = OUT / "run_logs"  # demo + ask runs (excerpted into log_excerpt.txt)
TEST_LOG_DIR = OUT / "test_logs"  # pytest/evaluation logs, discarded

QUESTIONS = [
    "How much does Mochi eat for breakfast?",
    "Where is Luna's pill?",
    "Can I give Luna an extra pill tonight?",
    "Is Tylenol safe for Mochi?",
    "Mochi ate chocolate, what do I do?",
    "What is the wifi password?",
]


def run(name: str, args: list, log_dir: Path = LOG_DIR) -> None:
    env = dict(os.environ, PAWPAL_DISABLE_LLM="1", PAWPAL_LOG_DIR=str(log_dir))
    result = subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    command = "python " + " ".join(f'"{a}"' if " " in a else a for a in args)
    header = (
        f"$ {command}\n"
        f"# generated {datetime.now():%Y-%m-%d %H:%M} · Python {platform.python_version()} · "
        f"offline mode · exit code {result.returncode}\n\n"
    )
    output = (result.stdout + result.stderr).replace(str(ROOT), ".")  # no local paths in evidence
    output = output.replace(str(Path.home()), "~")
    (OUT / name).write_text(header + output)
    print(f"{name:<28} exit {result.returncode}")


def main() -> None:
    shutil.rmtree(LOG_DIR, ignore_errors=True)
    run("pytest.txt", ["-m", "pytest", "-v"], TEST_LOG_DIR)
    run("evaluation.txt", ["-m", "evaluation.evaluate"], TEST_LOG_DIR)
    shutil.rmtree(TEST_LOG_DIR, ignore_errors=True)
    run("sitter_demo.txt", ["sitter_demo.py"])
    asks = []
    for q in QUESTIONS:
        run("_ask.txt", ["ask.py", q])
        asks.append((OUT / "_ask.txt").read_text())
    (OUT / "ask_examples.txt").write_text("\n" + ("-" * 70 + "\n").join(asks))
    (OUT / "_ask.txt").unlink()
    log = LOG_DIR / "pawpal.log"
    lines = log.read_text().splitlines() if log.exists() else []
    keep = [l for l in lines if any(k in l for k in ("guardrails", "Agent tool", "Conflict [", "Moved", "Resolving"))]
    (OUT / "log_excerpt.txt").write_text(
        "# Lines from logs/pawpal.log written while running sitter_demo.py and ask.py above\n"
        "# (conflicts, agent tool calls, guardrail triggers)\n\n"
        + "\n".join(keep) + "\n"
    )
    shutil.rmtree(LOG_DIR, ignore_errors=True)
    print("wrote evidence/*.txt")


if __name__ == "__main__":
    main()
