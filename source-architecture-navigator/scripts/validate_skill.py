#!/usr/bin/env python3
"""Validate this skill folder before packaging or publishing."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REQUIRED_FILES = [
    "SKILL.md",
    "references/source-reading-spec.md",
    "references/output-templates.md",
    "references/repo-probe-guide.md",
    "scripts/repo_probe.py",
    "scripts/render_navigation_html.py",
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

    output_templates = skill_dir / "references" / "output-templates.md"
    if output_templates.exists():
        text = read_text(output_templates)
        for marker in ["仓库/zip 首轮导航模板", "证据表", "L0 项目识别卡", "L1 核心对象小图", "L2 调用链小图", "L3 功能/数据/配置流", "HTML 阅读报告内容清单"]:
            if marker not in text:
                errors.append(f"output-templates.md missing marker: {marker}")

    repo_probe_guide = skill_dir / "references" / "repo-probe-guide.md"
    if repo_probe_guide.exists():
        text = read_text(repo_probe_guide)
        for marker in ["--keep-temp", "--extract-to", "--allow-output-in-repo", "render_navigation_html.py"]:
            if marker not in text:
                errors.append(f"repo-probe-guide.md missing marker: {marker}")

    spec = skill_dir / "references" / "source-reading-spec.md"
    if spec.exists():
        text = read_text(spec)
        for marker in ["证据表", "成员数量", "单文件大小", "只解压源码", "HTML 报告 spec"]:
            if marker not in text:
                errors.append(f"source-reading-spec.md missing marker: {marker}")

    repo_probe = skill_dir / "scripts" / "repo_probe.py"
    if repo_probe.exists():
        errors.extend(_verify_repo_probe_behavior(repo_probe))

    html_report = skill_dir / "scripts" / "render_navigation_html.py"
    if html_report.exists():
        errors.extend(_verify_html_report_behavior(html_report))

    if errors:
        for message in errors:
            print("ERROR: " + message)
        return 1

    print("OK: source-architecture-navigator skill checks passed")
    return 0


def _verify_repo_probe_behavior(repo_probe: Path) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="san_validate_") as tmp:
        root = Path(tmp)
        sample = root / "sample"
        pipeline = sample / "src" / "pkg" / "pipeline"
        scripts = sample / "scripts"
        pipeline.mkdir(parents=True)
        scripts.mkdir(parents=True)
        (sample / "README.md").write_text("Sample project\n", encoding="utf-8")
        (pipeline / "architecture_entry.py").write_text(
            "class StereoArchitecturePipeline:\n"
            "    def run_sequence(self):\n"
            "        return []\n",
            encoding="utf-8",
        )
        (scripts / "run_architecture_entry.py").write_text(
            "from pkg.pipeline.architecture_entry import StereoArchitecturePipeline\n"
            "def main():\n"
            "    return StereoArchitecturePipeline().run_sequence()\n",
            encoding="utf-8",
        )

        archive = root / "sample.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for path in sample.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(sample).as_posix())

        result = subprocess.run(
            [sys.executable, str(repo_probe), str(archive), "--max-files", "200"],
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            errors.append("repo_probe.py failed on sample zip: " + result.stderr.strip())
        else:
            stdout = result.stdout
            for marker in [
                "Extract root lifecycle",
                "src/pkg/pipeline/architecture_entry.py",
                "scripts/run_architecture_entry.py",
                "StereoArchitecturePipeline",
            ]:
                if marker not in stdout:
                    errors.append(f"repo_probe.py zip behavior missing marker: {marker}")

        output_inside = sample / "probe.md"
        result = subprocess.run(
            [sys.executable, str(repo_probe), str(sample), "--output", str(output_inside)],
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            errors.append("repo_probe.py should reject --output inside scanned repository by default")

    return errors


def _verify_html_report_behavior(html_report: Path) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="san_html_validate_") as tmp:
        root = Path(tmp)
        sample = root / "sample"
        pipeline = sample / "src" / "pkg" / "pipeline"
        scripts = sample / "scripts"
        pipeline.mkdir(parents=True)
        scripts.mkdir(parents=True)
        (sample / "README.md").write_text("# Sample project\n\nReads a left frame and builds a right-frame pipeline.\n", encoding="utf-8")
        (pipeline / "architecture_entry.py").write_text(
            "class StereoArchitecturePipeline:\n"
            "    def run_sequence(self):\n"
            "        return []\n",
            encoding="utf-8",
        )
        (scripts / "run_architecture_entry.py").write_text(
            "from pkg.pipeline.architecture_entry import StereoArchitecturePipeline\n"
            "def main():\n"
            "    return StereoArchitecturePipeline().run_sequence()\n",
            encoding="utf-8",
        )

        archive = root / "sample.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for path in sample.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(sample).as_posix())

        output = root / "report" / "source_navigation_report.html"
        result = subprocess.run(
            [sys.executable, str(html_report), str(archive), "--output", str(output), "--title", "Sample project"],
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            errors.append("render_navigation_html.py failed on sample zip: " + result.stderr.strip())
        elif not output.exists():
            errors.append("render_navigation_html.py did not create the HTML output")
        else:
            html_text = read_text(output)
            for marker in [
                "源码阅读导航报告",
                "L0 项目识别卡",
                "L1-L3 分层地图",
                "建议先看 3-5 个文件",
                "证据表",
                "StereoArchitecturePipeline",
            ]:
                if marker not in html_text:
                    errors.append(f"HTML report missing marker: {marker}")

        output_inside = sample / "source_navigation_report.html"
        result = subprocess.run(
            [sys.executable, str(html_report), str(sample), "--output", str(output_inside)],
            text=True,
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode == 0:
            errors.append("render_navigation_html.py should reject --output inside scanned repository by default")

    return errors


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
