# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail closed, output counts only. Real values are read only into memory.

Run from the repository root using its venv. Includes untracked publishable
workspace, index, staged diff, every reachable commit tree, and git log -p.
No matched value, filename or offending text is ever printed.
"""

import argparse
import fnmatch
import ipaddress
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args, optional=False):
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if proc.returncode and not optional:
        raise RuntimeError("Git audit input unavailable")
    return proc.stdout


def secret_tokens(path):
    from bambu_lan_cli.config import parse_env, secure_read

    values = parse_env(secure_read(path))
    if not all(values.get(k) for k in ("host", "serial", "access_code")):
        raise RuntimeError("Private comparison values unavailable")
    tokens = [v.encode() for v in values.values() if v]
    tokens += [
        str(Path.home()).encode(),
        Path.home().name.encode(),
        bytes([102, 110, 111, 115]),
    ]
    # Personal source/model basenames are discovered in memory, never logged.
    private = Path.home() / "Documents/maker"
    for suffix in ("*.3mf", "*.gcode", "*.stl"):
        for p in private.rglob(suffix):
            if (
                ".venv" not in p.parts
                and len(p.name) > 6
                and not re.fullmatch(r"plate_\d+\.gcode", p.name)
            ):
                tokens.append(p.name.encode())
    return set(tokens)


def forbidden_name(name):
    base = Path(name).name
    return base in (
        "config.yaml",
        "config.yml",
        "config.local.yaml",
        "config.local.yml",
        "printers.yaml",
        "run.log",
    ) or any(
        fnmatch.fnmatch(base, p)
        for p in ("*.env", ".env*", "*.bak*", "*.log", "*.local.yaml", "*.local.yml")
    )


def content_hits(data, tokens):
    hits = sum(t in data for t in tokens if t)
    text = data.decode("utf-8", errors="replace")
    if re.search(
        r"/Users/[A-Za-z0-9_.-]+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}",
        text,
    ):
        hits += 1
    # Only generic example address and CIDR network constants may be private IPv4.
    allowed = {"192.168.1.100", "10.0.0.0", "172.16.0.0", "192.168.0.0"}
    for match in re.finditer(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])", text):
        value = match.group()
        try:
            addr = ipaddress.IPv4Address(value)
        except ValueError:
            continue
        if (
            any(
                addr in ipaddress.ip_network(net)
                for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
            and value not in allowed
        ):
            hits += 1
    return hits


def audit(path):
    tokens = secret_tokens(path)
    names = set(git("ls-files", "-z").decode().split("\0")) | set(
        git("ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    )
    names.discard("")
    hits = 0
    for name in names:
        p = ROOT / name
        hits += forbidden_name(name)
        if p.is_symlink():
            hits += 1
        elif p.is_file():
            hits += content_hits(p.read_bytes(), tokens)
    index = git("ls-files", "-s", "-z")
    for entry in index.split(b"\0"):
        if entry:
            meta, name = entry.split(b"\t", 1)
            hits += forbidden_name(name.decode())
            hits += content_hits(
                git("cat-file", "blob", meta.split()[1].decode()), tokens
            )
    hits += content_hits(
        git("diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary"), tokens
    )
    commits = git("rev-list", "--all", optional=True).decode().splitlines()
    for commit in commits:
        for row in git("ls-tree", "-rz", commit).split(b"\0"):
            if row:
                meta, name = row.split(b"\t", 1)
                hits += forbidden_name(name.decode())
                if meta.split()[1] == b"blob":
                    hits += content_hits(
                        git("cat-file", "blob", meta.split()[2].decode()), tokens
                    )
    history = git(
        "log",
        "--all",
        "-p",
        "--no-ext-diff",
        "--no-textconv",
        "--format=fuller",
        optional=True,
    )
    hits += content_hits(history, tokens)
    required = (
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "README.md",
        "config.example.yaml",
        ".gitignore",
    )
    hits += sum(not (ROOT / name).is_file() for name in required)
    hits += int(
        "GNU AFFERO GENERAL PUBLIC LICENSE" not in (ROOT / "LICENSE").read_text()
    )
    result = {
        "audit": "PASS" if hits == 0 else "FAIL",
        "files": len(names),
        "commits": len(commits),
        "hits": hits,
        "scopes": [
            "workspace",
            "index",
            "staged diff",
            "all commit trees",
            "git log -p",
            "license files",
        ],
    }
    print(__import__("json").dumps(result))
    return int(hits != 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--private-env", type=Path, default=Path.home() / ".config/bambu/p2s.env"
    )
    try:
        raise SystemExit(audit(parser.parse_args().private_env))
    except Exception:
        print('{"audit":"FAIL","reason":"audit input unavailable; details suppressed"}')
        raise SystemExit(1)
