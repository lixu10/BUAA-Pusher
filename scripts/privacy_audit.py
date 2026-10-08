"""Audit staged files and Git history without printing any matched secret values.

Standard-library only. Optional local inputs are read-only and never copied.
This is a release guardrail, not a substitute for manual review.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
from pathlib import Path, PurePosixPath


PATTERNS = {
    "github credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "cloud credential": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "personal email": re.compile(r"[\w.+-]+@(?:qq|163|126|gmail|outlook|hotmail)\.com\b", re.I),
    "local home path": re.compile(r"(?:[A-Z]:[\\/]Users[\\/][A-Za-z0-9._-]+|(?<![\w/])/(?:Users|home)/[A-Za-z0-9._-]+)", re.I),
}
BLOCKED_DIRS = {"data", ".venv", "venv", ".research", ".openai", ".codex", ".agents", "__pycache__"}
EXAMPLE_ENVS = {".env.example", ".env.compose.example"}


def risks(path: str, content: bytes, private_values: set[str] | None = None) -> list[str]:
    name = PurePosixPath(path)
    found = []
    if any(part in BLOCKED_DIRS for part in name.parts):
        found.append("private/runtime directory")
    if (name.name.startswith(".env") and name.name not in EXAMPLE_ENVS) or name.name == ".master_key":
        found.append("local configuration/key")
    if re.search(r"\.(?:db(?:-.*)?|sqlite\w*|log|pem|key|p12|pfx|pyc|pyo)$", name.name, re.I):
        found.append("runtime/credential file")
    text = content.decode("utf-8", errors="replace")
    found.extend(label for label, pattern in PATTERNS.items() if pattern.search(text))
    if any(value in text for value in private_values or set()):
        found.append("known local private value")
    if name.name in EXAMPLE_ENVS:
        for line in text.splitlines():
            if line.strip().startswith("PUAA_MASTER_KEY=") and line.partition("=")[2].strip():
                found.append("nonempty example master key")
    return found


def known_private_values(database: Path | None, env_file: Path | None, key_file: Path | None) -> set[str]:
    values: set[str] = set()
    if database:
        # SQLite read-only mode: do not create, migrate or modify the user's database.
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for table, columns in {
                "puaa_users": ("email", "display_name", "password_hash"),
                "school_connections": ("school_id", "display_name", "password_ciphertext"),
                "notification_channels": ("config_ciphertext",),
                "puaa_sessions": ("token_hash",),
            }.items():
                available = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                selected = [column for column in columns if column in available]
                if selected:
                    for row in db.execute(f'SELECT {",".join(selected)} FROM "{table}"'):
                        values.update(str(value) for value in row if value is not None and len(str(value)) >= 4)
    if env_file:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and not key.startswith("#") and re.search(r"KEY|TOKEN|PASSWORD|SECRET", key, re.I):
                value = value.strip().strip("\"'")
                if len(value) >= 4:
                    values.add(value)
    if key_file:
        value = key_file.read_text(encoding="ascii").strip()
        if value:
            values.add(value)
    return values


def git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, check=True)
    return result.stdout


def audit(repository: Path, private_values: set[str]) -> int:
    findings: set[tuple[str, str]] = set()
    entries = git(repository, "ls-files", "--stage", "-z").split(b"\0")
    blobs: dict[tuple[str, str], None] = {}
    for entry in entries:
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            filename = path.decode("utf-8")
            if mode not in {"100644", "100755"} or stage != "0":
                findings.add((filename, "nonregular/unmerged staged file"))
            else:
                blobs[(oid, filename)] = None
    refs = git(repository, "rev-list", "--all").decode().splitlines()
    # Review every reachable tree, including files deleted in later commits.
    for commit in refs:
        for entry in git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                mode, kind, oid = metadata.decode().split()
                filename = path.decode("utf-8")
                if kind == "blob":
                    blobs[(oid, filename)] = None
                else:
                    findings.add((filename, "external Git object"))
        metadata = git(repository, "show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit)
        for label in risks("commit-metadata", metadata, private_values):
            findings.add((commit[:12], label))
    for oid, filename in blobs:
        for label in risks(filename, git(repository, "cat-file", "blob", oid), private_values):
            findings.add((filename, label))
    for filename, label in sorted(findings):
        print(f"BLOCKED: {filename}: {label}")
    print(f"Audited {len(blobs)} file versions and {len(refs)} commits; findings: {len(findings)}")
    if not blobs:
        print("BLOCKED: no staged or committed files to audit")
        return 1
    return int(bool(findings))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--private-db", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    values = known_private_values(args.private_db, args.env_file, args.key_file)
    return audit(args.repository.resolve(), values)


if __name__ == "__main__":
    raise SystemExit(main())
