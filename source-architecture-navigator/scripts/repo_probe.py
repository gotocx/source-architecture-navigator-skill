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
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path


ZIP_MAX_MEMBERS = 20000
ZIP_MAX_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
ZIP_MAX_MEMBER_BYTES = 50 * 1024 * 1024

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

TEXT_SUFFIXES = set(LANG_BY_SUFFIX) | {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".html",
    ".css",
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
    re.compile(r"(^|[/\\])scripts[/\\](run|smoke|profile|inspect|export|download|score|cut|raw)_.*\.py$", re.I),
    re.compile(r"(^|[/\\])src[/\\].*[/\\](pipeline|routes?|api)[/\\].*\.(py|js|jsx|ts|tsx)$", re.I),
    re.compile(r"(^|[/\\]).*(architecture_entry|entry|pipeline)\.(py|js|jsx|ts|tsx)$", re.I),
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
    signature: str = ""


@dataclass
class ImportEdge:
    source: str
    target: str


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


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


def entry_score(rel: str) -> tuple[int, str]:
    lower = rel.lower()
    score = 0
    if lower.startswith("src/"):
        score += 40
    if "/pipeline/" in lower or "\\pipeline\\" in lower:
        score += 50
    if "architecture_entry" in lower:
        score += 80
    if lower.endswith("/__init__.py"):
        score += 10
    if lower.startswith("scripts/run_"):
        score += 45
    if lower.startswith("scripts/smoke_"):
        score += 20
    if lower.startswith("tests/"):
        score -= 30
    return (-score, rel)


def symbol_score(item: SymbolSample) -> tuple[int, str, int]:
    lower = item.path.lower()
    score = 0
    if lower.startswith("src/"):
        score += 50
    if "/pipeline/" in lower or "\\pipeline\\" in lower:
        score += 60
    if "architecture_entry" in lower:
        score += 80
    if item.kind == "class":
        score += 15
    if lower.startswith("scripts/run_"):
        score += 20
    if lower.startswith("scripts/"):
        score -= 10
    if lower.startswith("tests/"):
        score -= 30
    return (-score, item.path, item.line)


def edge_score(item: ImportEdge) -> tuple[int, str, str]:
    lower = item.source.lower()
    score = 0
    if lower.startswith("src/"):
        score += 50
    if "/pipeline/" in lower or "\\pipeline\\" in lower:
        score += 60
    if "architecture_entry" in lower:
        score += 80
    if lower.startswith("scripts/run_"):
        score += 25
    if lower.startswith("scripts/"):
        score -= 10
    if lower.startswith("tests/"):
        score -= 30
    return (-score, item.source, item.target)


def format_annotation(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def format_args(args: ast.arguments) -> str:
    parts: list[str] = []
    all_args = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(all_args) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(all_args, defaults):
        text = arg.arg
        annotation = format_annotation(arg.annotation)
        if annotation:
            text += f": {annotation}"
        if default is not None:
            text += "=..."
        parts.append(text)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        text = arg.arg
        annotation = format_annotation(arg.annotation)
        if annotation:
            text += f": {annotation}"
        if default is not None:
            text += "=..."
        parts.append(text)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts)


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    text = f"{prefix}{node.name}({format_args(node.args)})"
    returns = format_annotation(node.returns)
    if returns:
        text += f" -> {returns}"
    return text


def class_signature(node: ast.ClassDef) -> str:
    bases = [format_annotation(base) for base in node.bases]
    bases = [base for base in bases if base]
    suffix = f"({', '.join(bases)})" if bases else ""
    return f"class {node.name}{suffix}"


def parse_python(path: Path, root: Path) -> tuple[list[SymbolSample], list[ImportEdge]]:
    rel = safe_rel(path, root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return [], []

    symbols: list[SymbolSample] = []
    edges: list[ImportEdge] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(SymbolSample(rel, "function", node.name, node.lineno, function_signature(node)))
        elif isinstance(node, ast.ClassDef):
            symbols.append(SymbolSample(rel, "class", node.name, node.lineno, class_signature(node)))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        SymbolSample(
                            rel,
                            "method",
                            f"{node.name}.{child.name}",
                            child.lineno,
                            function_signature(child),
                        )
                    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
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
            symbols.append(SymbolSample(rel, kind, name, line_no, name))
        for target in IMPORT_RE.findall(line):
            edges.append(ImportEdge(rel, target))
    return symbols, edges


def build_report(root: Path, max_files: int) -> dict:
    files = collect_files(root, max_files=max_files)
    language_counts = Counter()
    dir_counts = Counter()
    manifests: list[str] = []
    readmes: list[str] = []
    source_files: list[str] = []
    config_files: list[str] = []
    test_files: list[str] = []
    script_files: list[str] = []
    line_counts: dict[str, int] = {}
    entries: list[str] = []
    symbols: list[SymbolSample] = []
    edges: list[ImportEdge] = []

    for path in files:
        rel = safe_rel(path, root)
        language = LANG_BY_SUFFIX.get(path.suffix.lower())
        if language:
            language_counts[language] += 1
            source_files.append(rel)
        top = rel.split("/", 1)[0] if "/" in rel else "(root files)"
        dir_counts[top] += 1
        if path.name in MANIFEST_NAMES:
            manifests.append(rel)
        if path.name.lower().startswith("readme") and path.suffix.lower() in {".md", ".txt", ".rst"}:
            readmes.append(rel)
        if path.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".env.example"} or "config" in path.name.lower():
            config_files.append(rel)
        if rel.lower().startswith("tests/") or "/tests/" in rel.lower() or path.name.lower().startswith("test_") or path.name.lower().endswith("_test.py"):
            test_files.append(rel)
        if rel.lower().startswith("scripts/"):
            script_files.append(rel)
        if any(pattern.search(rel) for pattern in ENTRY_NAME_PATTERNS):
            entries.append(rel)

        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                line_counts[rel] = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
            except OSError:
                pass

        if path.suffix.lower() == ".py":
            file_symbols, file_edges = parse_python(path, root)
        elif path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}:
            file_symbols, file_edges = parse_text_symbols(path, root)
        else:
            file_symbols, file_edges = [], []
        symbols.extend(file_symbols)
        edges.extend(file_edges)

    entries = sorted(set(entries), key=entry_score)
    symbols = sorted(symbols, key=symbol_score)
    edges = sorted(edges, key=edge_score)

    return {
        "root": str(root),
        "file_count_scanned": len(files),
        "language_counts": dict(language_counts.most_common()),
        "top_directories": dict(dir_counts.most_common(12)),
        "manifests": manifests[:40],
        "readme_files": readmes[:20],
        "source_files": source_files[:400],
        "config_files": config_files[:120],
        "test_files": test_files[:120],
        "script_files": script_files[:120],
        "line_counts": dict(sorted(line_counts.items(), key=lambda item: (-item[1], item[0]))[:200]),
        "entry_candidates": entries[:50],
        "symbol_samples": [asdict(item) for item in symbols[:260]],
        "import_edge_samples": [asdict(item) for item in edges[:360]],
    }


def should_extract_member(member_name: str) -> bool:
    path = Path(member_name)
    if should_skip(path):
        return False
    return path.name in MANIFEST_NAMES or path.suffix.lower() in TEXT_SUFFIXES


def safe_extract_zip(zip_path: Path, destination: Path) -> dict:
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > ZIP_MAX_MEMBERS:
            raise ValueError(f"Zip has too many members: {len(members)} > {ZIP_MAX_MEMBERS}")

        total_uncompressed = 0
        extracted = 0
        skipped = 0
        for member in members:
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"Unsafe zip member path: {member.filename}")
            if member.file_size > ZIP_MAX_MEMBER_BYTES:
                raise ValueError(f"Zip member too large: {member.filename}")
            total_uncompressed += member.file_size
            if total_uncompressed > ZIP_MAX_TOTAL_UNCOMPRESSED:
                raise ValueError("Zip uncompressed size exceeds safety limit")

            if member.is_dir() or not should_extract_member(member.filename):
                skipped += 1
                continue
            target = (destination / member.filename).resolve()
            if not str(target).startswith(str(destination.resolve())):
                raise ValueError(f"Unsafe zip member target: {member.filename}")
            archive.extract(member, destination)
            extracted += 1
        return {
            "members": len(members),
            "extracted_members": extracted,
            "skipped_members": skipped,
            "uncompressed_bytes": total_uncompressed,
        }


def to_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# Project card")
    lines.append("")
    if report.get("source_archive"):
        lines.append(f"- Source archive: `{report['source_archive']}`")
        lines.append(f"- Extract root: `{report['extract_root']}`")
        lines.append(f"- Extract root lifecycle: {report['extract_root_lifecycle']}")
        stats = report.get("zip_stats", {})
        if stats:
            lines.append(
                "- Zip scan: "
                f"{stats.get('extracted_members', 0)} extracted, "
                f"{stats.get('skipped_members', 0)} skipped, "
                f"{stats.get('members', 0)} total members"
            )
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
    section("Readme files", report.get("readme_files", []))
    section("Config files", report.get("config_files", []))
    section("Test files", report.get("test_files", []))
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
    parser.add_argument("--extract-to", help="Optional directory for zip extraction; must be outside the target repo")
    parser.add_argument("--keep-temp", action="store_true", help="Keep an auto-created zip extraction directory")
    parser.add_argument(
        "--allow-output-in-repo",
        action="store_true",
        help="Allow --output inside the scanned directory; default rejects this to avoid repo pollution",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.repo).resolve()
    if not input_path.exists():
        print(f"ERROR: path does not exist: {input_path}", file=sys.stderr)
        return 1

    output_path = Path(args.output).resolve() if args.output else None

    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        if args.extract_to:
            scan_root = Path(args.extract_to).resolve()
            scan_root.mkdir(parents=True, exist_ok=True)
            cleanup = None
            lifecycle = "kept because --extract-to was provided"
        elif args.keep_temp:
            scan_root = Path(tempfile.mkdtemp(prefix="source_arch_nav_")).resolve()
            cleanup = None
            lifecycle = "kept because --keep-temp was provided"
        else:
            cleanup = tempfile.TemporaryDirectory(prefix="source_arch_nav_")
            scan_root = Path(cleanup.name).resolve()
            lifecycle = "auto-cleaned after this command exits; use --keep-temp or --extract-to to inspect files"

        try:
            try:
                zip_stats = safe_extract_zip(input_path, scan_root)
            except (zipfile.BadZipFile, ValueError) as exc:
                print(f"ERROR: failed to safely read zip: {exc}", file=sys.stderr)
                return 1
            if output_path and is_relative_to(output_path, scan_root) and not args.allow_output_in_repo:
                print("ERROR: --output is inside the scanned extract root; use a path outside it", file=sys.stderr)
                return 1
            report = build_report(scan_root, max_files=args.max_files)
            report["source_archive"] = str(input_path)
            report["extract_root"] = str(scan_root)
            report["extract_root_lifecycle"] = lifecycle
            report["zip_stats"] = zip_stats
            text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else to_markdown(report)
        finally:
            if cleanup is not None:
                cleanup.cleanup()
    elif input_path.is_dir():
        if output_path and is_relative_to(output_path, input_path) and not args.allow_output_in_repo:
            print("ERROR: --output is inside the scanned repository; use a path outside it", file=sys.stderr)
            return 1
        report = build_report(input_path, max_files=args.max_files)
        text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else to_markdown(report)
    else:
        print(f"ERROR: path is neither a directory nor a .zip archive: {input_path}", file=sys.stderr)
        return 1

    if args.output:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
