#!/usr/bin/env python3
"""Validate this skill folder before packaging or publishing."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REQUIRED_FILES = [
    "SKILL.md",
    "references/source-reading-spec.md",
    "references/output-templates.md",
    "references/repo-probe-guide.md",
    "scripts/repo_probe.py",
    "assets/source-architecture-navigator.html",
]

REQUIRED_MARKERS = [
    "## @工作流: 源码架构导航",
    "## @工作流: 单对象追踪",
    "## @工作流: 仓库体检报告",
    "## @工作流: 施工边界桥接",
    "## 版本历史",
]

PLACEHOLDER_MARKER = "TO" + "DO"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_frontmatter(markdown: str) -> str | None:
    match = re.match(r"\A---\s*\n([\s\S]*?)\n---\s*\n", markdown)
    if not match:
        return None
    return match.group(1)


def verify(skill_dir: Path) -> int:
    errors: list[str] = []

    for rel_path in REQUIRED_FILES:
        path = skill_dir / rel_path
        if not path.exists():
            errors.append(f"Missing required file: {rel_path}")

    skill_md = skill_dir / "SKILL.md"
    content = ""
    if skill_md.exists():
        try:
            content = read_text(skill_md)
        except UnicodeDecodeError:
            errors.append("SKILL.md must be valid UTF-8")

    if content:
        frontmatter = extract_frontmatter(content)
        if not frontmatter:
            errors.append("SKILL.md missing YAML frontmatter")
        else:
            if "name: source-architecture-navigator" not in frontmatter:
                errors.append("Frontmatter name must be source-architecture-navigator")
            if "description:" not in frontmatter:
                errors.append("Frontmatter missing description")
            if "version: 1.0.0" not in frontmatter:
                errors.append("Frontmatter version must be 1.0.0")

        if PLACEHOLDER_MARKER in content:
            errors.append("SKILL.md still contains placeholder markers")

        for marker in REQUIRED_MARKERS:
            if marker not in content:
                errors.append(f"SKILL.md missing marker: {marker}")

    for rel_path in REQUIRED_FILES:
        path = skill_dir / rel_path
        if path.suffix.lower() not in {".md", ".py", ".html"} or not path.exists():
            continue
        try:
            text = read_text(path)
        except UnicodeDecodeError:
            errors.append(f"{rel_path} must be valid UTF-8")
            continue
        if PLACEHOLDER_MARKER in text:
            errors.append(f"{rel_path} still contains placeholder markers")

    if errors:
        for message in errors:
            print("ERROR: " + message)
        return 1

    print("OK: source-architecture-navigator skill checks passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill", required=True, help="Skill folder path")
    args = parser.parse_args()

    skill_dir = Path(args.skill).resolve()
    if not skill_dir.exists():
        print("ERROR: skill folder not found: " + str(skill_dir))
        return 1

    return verify(skill_dir)


if __name__ == "__main__":
    raise SystemExit(main())
