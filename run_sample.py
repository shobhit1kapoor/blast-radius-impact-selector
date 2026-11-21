"""Build the entry image and score it against the official local sample."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "sample"
IMAGE = "blast-radius-entry"
OUTPUT = ROOT / "output.jsonl"


def run(command: list[str], **kwargs: object) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def main() -> int:
    try:
        run(["docker", "info", "--format", "{{.ServerVersion}}"], stdout=subprocess.DEVNULL)
        run(["docker", "build", "--platform", "linux/amd64", "--tag", IMAGE, str(ROOT)])

        docker_sample = str(SAMPLE).replace("\\", "/")
        docker_run = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m",
            "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
            "--env", "HOME=/tmp", "--volume", f"{docker_sample}/commits:/data/commits:ro",
            "--volume", f"{docker_sample}/tests.json:/data/tests.json:ro", IMAGE,
        ]
        with OUTPUT.open("wb") as output:
            run(docker_run, stdout=output)

        run([
            sys.executable, str(ROOT / "score.py"), str(SAMPLE),
            str(SAMPLE / "answers.json"), str(OUTPUT),
        ])
    except FileNotFoundError as error:
        print(f"Missing prerequisite: {error.filename}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"Command failed with exit code {error.returncode}", file=sys.stderr)
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
