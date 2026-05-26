#!/usr/bin/env python3
"""Read-only repository architecture probe.

The script prints a compact repository census. By default it writes nothing to
the inspected repository. Use --output only when a file artifact is wanted.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".cache",
    ".next",
    ".nuxt",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    "target",
    ".venv",
    "venv",
    "env",
    "outputs",
}

LANG_BY_SUFFIX = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".cs": "C#",
    ".php": "PHP",
    ".rb": "Ruby",
    ".swift": "Swift",
    ".c": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".h": "C/C++",
    ".hpp": "C/C++",
    ".sql": "SQL",
}

MANIFEST_NAMES = {
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "uv.lock",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "composer.json",
    "Gemfile",
    "Dockerfile",
    "docker-compose.yml",
    "tsconfig.json",
    "vite.config.ts",
    "vite.config.js",
    "next.config.js",
    "next.config.mjs",
}

ENTRY_NAME_PATTERNS = [
    re.compile(r"(^|[/\\])(main|app|server|index|cli|manage|worker|router|routes)\.(py|js|jsx|ts|tsx|go|rs)$", re.I),
    re.compile(r"(^|[/\\])(pages|routes|api|app)[/\\].+\.(js|jsx|ts|tsx|py)$", re.I),
    re.compile(r"(^|[/\\]).*config\.(js|ts|py|toml|yaml|yml|json)$", re.I),
]

IMPORT_RE = re.compile(r"""(?:import\s+.*?\s+from\s+|import\s+|require\()\s*['"]([^'"]+)['"]""")
SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)|"
    r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(?"
)


@dataclass
class SymbolSample:
    path: str
    kind: str
    name: str
    line: int


@dataclass
class ImportEdge:
    source: str
    target: str


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def safe_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def collect_files(root: Path, max_files: int) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if should_skip(rel):
            continue
        if path.is_file():
            files.append(path)
            if len(files) >= max_files:
                break
    return sorted(files)


def parse_python(path: Path, root: Path) -> tuple[list[SymbolSample], list[ImportEdge]]:
    rel = safe_rel(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return [], []

    symbols: list[SymbolSample] = []
    edges: list[ImportEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(SymbolSample(rel, "function", node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            symbols.append(SymbolSample(rel, "class", node.name, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                edges.append(ImportEdge(rel, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            edges.append(ImportEdge(rel, node.module))
    return symbols, edges


def parse_text_symbols(path: Path, root: Path) -> tuple[list[SymbolSample], list[ImportEdge]]:
    rel = safe_rel(path, root)
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return [], []

    symbols: list[SymbolSample] = []
    edges: list[ImportEdge] = []
    for line_no, line in enumerate(lines, start=1):
        match = SYMBOL_RE.search(line)
        if match:
            name = match.group(1) or match.group(2)
            kind = "class" if "class " in line else "function"
            symbols.append(SymbolSample(rel, kind, name, line_no))
        for target in IMPORT_RE.findall(line):
            edges.append(ImportEdge(rel, target))
    return symbols, edges


def build_report(root: Path, max_files: int) -> dict:
    files = collect_files(root, max_files=max_files)
    language_counts = Counter()
    dir_counts = Counter()
    manifests: list[str] = []
    entries: list[str] = []
    symbols: list[SymbolSample] = []
    edges: list[ImportEdge] = []

    for path in files:
        rel = safe_rel(path, root)
        language = LANG_BY_SUFFIX.get(path.suffix.lower())
        if language:
            language_counts[language] += 1
        top = rel.split("/", 1)[0]
        dir_counts[top] += 1
        if path.name in MANIFEST_NAMES:
            manifests.append(rel)
        if any(pattern.search(rel) for pattern in ENTRY_NAME_PATTERNS):
            entries.append(rel)

        if path.suffix.lower() == ".py":
            file_symbols, file_edges = parse_python(path, root)
        elif path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}:
            file_symbols, file_edges = parse_text_symbols(path, root)
        else:
            file_symbols, file_edges = [], []
        symbols.extend(file_symbols[:20])
        edges.extend(file_edges[:30])

    return {
        "root": str(root),
        "file_count_scanned": len(files),
        "language_counts": dict(language_counts.most_common()),
        "top_directories": dict(dir_counts.most_common(12)),
        "manifests": manifests[:40],
        "entry_candidates": entries[:50],
        "symbol_samples": [asdict(item) for item in symbols[:80]],
        "import_edge_samples": [asdict(item) for item in edges[:120]],
    }


def to_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# Project card")
    lines.append("")
    lines.append(f"- Root: `{report['root']}`")
    lines.append(f"- Files scanned: {report['file_count_scanned']}")
    langs = report["language_counts"]
    primary = next(iter(langs), "unknown")
    lines.append(f"- Primary language signal: {primary}")
    lines.append("")

    def section(title: str, items: list[str] | dict) -> None:
        lines.append(f"## {title}")
        if isinstance(items, dict):
            if not items:
                lines.append("- None found")
            for key, value in items.items():
                lines.append(f"- `{key}`: {value}")
        else:
            if not items:
                lines.append("- None found")
            for item in items:
                lines.append(f"- `{item}`")
        lines.append("")

    section("Language counts", report["language_counts"])
    section("Top directories", report["top_directories"])
    section("Manifests", report["manifests"])
    section("Entry candidates", report["entry_candidates"])

    lines.append("## Symbol samples")
    if not report["symbol_samples"]:
        lines.append("- None found")
    for item in report["symbol_samples"]:
        lines.append(f"- `{item['path']}:{item['line']}` {item['kind']} `{item['name']}`")
    lines.append("")

    lines.append("## Import edge samples")
    if not report["import_edge_samples"]:
        lines.append("- None found")
    by_source: dict[str, list[str]] = defaultdict(list)
    for item in report["import_edge_samples"]:
        if len(by_source[item["source"]]) < 5:
            by_source[item["source"]].append(item["target"])
    for source, targets in list(by_source.items())[:30]:
        joined = ", ".join(f"`{target}`" for target in targets)
        lines.append(f"- `{source}` -> {joined}")
    lines.append("")
    lines.append("> Read-only static census. Use it as a starting point, not as runtime proof.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", nargs="?", default=".", help="Repository path")
    parser.add_argument("--max-files", type=int, default=8000, help="Maximum files to scan")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--output", help="Optional output path")
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve()
    if not root.exists() or not root.is_dir():
        print(f"ERROR: repo path is not a directory: {root}", file=sys.stderr)
        return 1

    report = build_report(root, max_files=args.max_files)
    text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else to_markdown(report)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
