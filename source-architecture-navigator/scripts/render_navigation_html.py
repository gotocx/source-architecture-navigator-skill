#!/usr/bin/env python3
"""Render a static source-reading navigation report.

The report is intentionally written outside the scanned repository by default.
It uses repo_probe.py for the read-only census, then turns that census into a
human-readable HTML artifact with L0-L3 maps, evidence, and a reading route.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import repo_probe  # noqa: E402


REPORT_HEADING = "源码阅读导航报告"
UNKNOWN = "待源码阅读确认"


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def compact(value: str, limit: int = 88) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def code(value: object) -> str:
    return f"<code>{h(value)}</code>"


def strip_markdown(line: str) -> str:
    line = re.sub(r"^#+\s*", "", line.strip())
    line = re.sub(r"[*_`>#\[\]()]|\!\[[^\]]*\]", "", line)
    line = re.sub(r"https?://\S+", "", line)
    return compact(line, 150)


def read_project_hint(scan_root: Path, readmes: list[str]) -> str:
    for rel in readmes[:3]:
        path = scan_root / rel
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw in lines[:80]:
            clean = strip_markdown(raw)
            if len(clean) >= 12:
                return clean
    return UNKNOWN


def prepare_report(
    source: Path,
    output_path: Path,
    max_files: int,
    extract_to: str | None,
    keep_temp: bool,
    allow_output_in_repo: bool,
) -> tuple[dict, Path, tempfile.TemporaryDirectory[str] | None]:
    input_path = source.resolve()
    if not input_path.exists():
        raise ValueError(f"path does not exist: {input_path}")

    cleanup: tempfile.TemporaryDirectory[str] | None = None
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        if extract_to:
            scan_root = Path(extract_to).resolve()
            scan_root.mkdir(parents=True, exist_ok=True)
            lifecycle = "kept because --extract-to was provided"
        elif keep_temp:
            scan_root = Path(tempfile.mkdtemp(prefix="source_arch_nav_")).resolve()
            lifecycle = "kept because --keep-temp was provided"
        else:
            cleanup = tempfile.TemporaryDirectory(prefix="source_arch_nav_")
            scan_root = Path(cleanup.name).resolve()
            lifecycle = "auto-cleaned after this command exits; use --keep-temp or --extract-to for clickable file links"

        if repo_probe.is_relative_to(output_path, scan_root) and not allow_output_in_repo:
            if cleanup is not None:
                cleanup.cleanup()
            raise ValueError("--output is inside the scanned extract root; choose a path outside it")

        try:
            zip_stats = repo_probe.safe_extract_zip(input_path, scan_root)
        except (zipfile.BadZipFile, ValueError) as exc:
            if cleanup is not None:
                cleanup.cleanup()
            raise ValueError(f"failed to safely read zip: {exc}") from exc

        report = repo_probe.build_report(scan_root, max_files=max_files)
        report["source_archive"] = str(input_path)
        report["extract_root"] = str(scan_root)
        report["extract_root_lifecycle"] = lifecycle
        report["zip_stats"] = zip_stats
        return report, scan_root, cleanup

    if input_path.is_dir():
        if repo_probe.is_relative_to(output_path, input_path) and not allow_output_in_repo:
            raise ValueError("--output is inside the scanned repository; choose a path outside it")
        return repo_probe.build_report(input_path, max_files=max_files), input_path, cleanup

    raise ValueError(f"path is neither a directory nor a .zip archive: {input_path}")


def first_language(report: dict) -> str:
    langs = report.get("language_counts", {})
    return next(iter(langs), "unknown")


def choose_symbol(report: dict) -> dict | None:
    symbols = report.get("symbol_samples", [])
    return symbols[0] if symbols else None


def choose_edge(report: dict) -> dict | None:
    edges = report.get("import_edge_samples", [])
    return edges[0] if edges else None


def build_reading_route(report: dict) -> list[dict]:
    symbol_paths = [item["path"] for item in report.get("symbol_samples", [])]
    paths = unique(
        report.get("readme_files", [])[:1]
        + report.get("entry_candidates", [])[:2]
        + symbol_paths[:4]
        + report.get("manifests", [])[:2]
    )
    route: list[dict] = []
    for rel in paths[:5]:
        lower = rel.lower()
        if "readme" in lower:
            purpose = "确认项目目标、硬边界和推荐运行方式"
            skip = "先跳过细碎安装兼容项"
            level = "E3"
        elif lower.startswith("scripts/") or re.search(r"(^|/)(main|app|server|cli|index)\.", lower):
            purpose = "确认真实入口如何装配参数和启动主链路"
            skip = "先跳过实验脚本和一次性导出逻辑"
            level = "E3"
        elif "/pipeline/" in lower or "architecture_entry" in lower or "/routes" in lower or "/api" in lower:
            purpose = "看核心编排：输入、转换、输出和模块边界"
            skip = "先不深入底层工具函数"
            level = "E1"
        elif lower.endswith((".json", ".toml", ".yaml", ".yml", ".ini", ".cfg")):
            purpose = "确认依赖、配置入口和启动约束"
            skip = "先不展开每个依赖包"
            level = "E3"
        else:
            purpose = "找到核心对象定义，建立下一张 L1 小图"
            skip = "先跳过测试夹具和历史兼容分支"
            level = "E1"
        line = next((str(item["line"]) for item in report.get("symbol_samples", []) if item["path"] == rel), "1")
        route.append({"path": rel, "purpose": purpose, "skip": skip, "evidence": f"{level} `{rel}:{line}`"})
    return route


def build_evidence_rows(report: dict, route: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for rel in report.get("entry_candidates", [])[:4]:
        rows.append({"item": "入口候选", "level": "E3", "where": f"{rel}:1", "state": "静态命名/路径证据"})
    symbol = choose_symbol(report)
    if symbol:
        rows.append(
            {
                "item": f"核心对象 {symbol['name']}",
                "level": "E1",
                "where": f"{symbol['path']}:{symbol['line']}",
                "state": "符号定义证据",
            }
        )
    edge = choose_edge(report)
    if edge:
        rows.append(
            {
                "item": "导入关系样本",
                "level": "E2",
                "where": edge["source"],
                "state": f"依赖 {edge['target']}",
            }
        )
    for item in route[:3]:
        where = item["evidence"].split("`", 2)[1] if "`" in item["evidence"] else item["path"]
        rows.append({"item": "阅读路线项", "level": item["evidence"].split(" ", 1)[0], "where": where, "state": "建议先读"})
    return rows[:10]


def group_import_edges(report: dict) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in report.get("import_edge_samples", []):
        if len(grouped[item["source"]]) < 4:
            grouped[item["source"]].append(item["target"])
    return list(grouped.items())[:8]


def path_link(rel: str, scan_root: Path, linkable: bool) -> str:
    if not rel:
        return code(UNKNOWN)
    target = scan_root / rel.split(":", 1)[0]
    label = h(rel)
    if linkable and target.exists():
        return f'<a href="{h(target.resolve().as_uri())}">{label}</a>'
    return f"<code>{label}</code>"


def render_list(items: list[str], empty: str = "未发现") -> str:
    if not items:
        return f"<li>{h(empty)}</li>"
    return "\n".join(f"<li>{code(compact(item, 92))}</li>" for item in items)


def render_metric(label: str, value: object, note: str = "") -> str:
    return f"""
      <div class="metric">
        <span>{h(label)}</span>
        <strong>{h(value)}</strong>
        <small>{h(note)}</small>
      </div>
    """


def render_route(route: list[dict], scan_root: Path, linkable: bool) -> str:
    if not route:
        return '<p class="muted">探针没有找到足够的入口或符号样本。先用文件清单缩小范围。</p>'
    items = []
    for index, item in enumerate(route, start=1):
        items.append(
            f"""
            <article class="route-item">
              <div class="route-index">{index}</div>
              <div>
                <h3>{path_link(item['path'], scan_root, linkable)}</h3>
                <p><b>读什么:</b> {h(item['purpose'])}</p>
                <p><b>暂时跳过:</b> {h(item['skip'])}</p>
                <p class="evidence-chip">{h(item['evidence'])}</p>
              </div>
            </article>
            """
        )
    return "\n".join(items)


def render_symbol_table(report: dict, scan_root: Path, linkable: bool) -> str:
    rows = []
    for item in report.get("symbol_samples", [])[:12]:
        rows.append(
            f"""
            <tr>
              <td>{path_link(f"{item['path']}:{item['line']}", scan_root, linkable)}</td>
              <td>{h(item['kind'])}</td>
              <td>{code(item['name'])}</td>
            </tr>
            """
        )
    if not rows:
        return '<p class="muted">未发现可抽样的函数或类。</p>'
    return f"""
      <table>
        <thead><tr><th>位置</th><th>类型</th><th>名称</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    """


def render_edge_groups(report: dict) -> str:
    groups = group_import_edges(report)
    if not groups:
        return '<p class="muted">未发现导入边样本。</p>'
    blocks = []
    for source, targets in groups:
        blocks.append(
            f"""
            <div class="edge-block">
              <strong>{code(compact(source, 70))}</strong>
              <span>依赖</span>
              <p>{', '.join(code(compact(target, 42)) for target in targets)}</p>
            </div>
            """
        )
    return "\n".join(blocks)


def render_evidence(rows: list[dict]) -> str:
    if not rows:
        return '<p class="muted">暂无证据行。请先补充入口、符号或搜索结果。</p>'
    body = []
    for row in rows:
        body.append(
            f"""
            <tr>
              <td>{h(row['item'])}</td>
              <td><span class="level">{h(row['level'])}</span></td>
              <td>{code(row['where'])}</td>
              <td>{h(row['state'])}</td>
            </tr>
            """
        )
    return f"""
      <table>
        <thead><tr><th>对象/边/路线项</th><th>等级</th><th>证据位置</th><th>状态</th></tr></thead>
        <tbody>{''.join(body)}</tbody>
      </table>
    """


def render_html(report: dict, scan_root: Path, title: str, subtitle: str | None) -> str:
    route = build_reading_route(report)
    evidence_rows = build_evidence_rows(report, route)
    symbol = choose_symbol(report)
    edge = choose_edge(report)
    project_hint = read_project_hint(scan_root, report.get("readme_files", []))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lifecycle = report.get("extract_root_lifecycle", "")
    linkable = not lifecycle.startswith("auto-cleaned")
    source_label = report.get("source_archive") or report.get("root")
    zip_stats = report.get("zip_stats", {})
    primary = first_language(report)
    entries = report.get("entry_candidates", [])
    manifests = report.get("manifests", [])
    top_dirs = report.get("top_directories", {})

    core_object = (
        f"{symbol['kind']} {symbol['name']}"
        if symbol
        else "需要下一问指定函数、类、组件或接口"
    )
    core_location = f"{symbol['path']}:{symbol['line']}" if symbol else UNKNOWN
    edge_line = f"{edge['source']} -> {edge['target']}" if edge else "需要继续追踪调用者和被调用者"
    flow_entry = entries[0] if entries else "入口候选不足"
    flow_core = core_location
    flow_output = "输出/副作用需要进入源码确认"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)} · {REPORT_HEADING}</title>
  <style>
    :root {{
      --ink: #171717;
      --muted: #62676b;
      --paper: #f5f4ee;
      --panel: #ffffff;
      --line: #d9d4c7;
      --red: #c5362f;
      --blue: #1f5c86;
      --green: #26715d;
      --gold: #a87312;
      --soft-blue: #e6f0f5;
      --soft-green: #e4f2ec;
      --soft-red: #f6e7e4;
      --shadow: 0 18px 45px rgba(23, 23, 23, .10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(23, 23, 23, .035) 1px, transparent 1px),
        linear-gradient(0deg, rgba(23, 23, 23, .03) 1px, transparent 1px),
        var(--paper);
      background-size: 28px 28px;
      font: 15px/1.65 "Segoe UI", "Microsoft YaHei", sans-serif;
    }}
    a {{ color: var(--blue); text-decoration: none; border-bottom: 1px solid rgba(31, 92, 134, .35); }}
    a:hover {{ border-bottom-color: var(--blue); }}
    code {{
      padding: 2px 6px;
      border: 1px solid rgba(23, 23, 23, .12);
      border-radius: 5px;
      background: #f8f8f5;
      color: #222;
      font: 12px/1.4 "Cascadia Mono", Consolas, monospace;
      word-break: break-word;
    }}
    .page {{
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
      min-height: 100vh;
    }}
    aside {{
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 28px 22px;
      border-right: 1px solid var(--line);
      background: rgba(255, 255, 255, .72);
      backdrop-filter: blur(8px);
    }}
    .brand {{
      font-family: Georgia, "Times New Roman", serif;
      font-size: 22px;
      line-height: 1.1;
      margin: 0 0 18px;
    }}
    .side-note {{
      margin: 0 0 28px;
      color: var(--muted);
      font-size: 13px;
    }}
    nav a {{
      display: block;
      margin: 8px 0;
      padding: 8px 10px;
      border: 1px solid transparent;
      border-radius: 7px;
      color: var(--ink);
    }}
    nav a:hover {{ background: var(--soft-blue); border-color: #c9dce8; }}
    main {{ padding: 34px min(5vw, 72px) 72px; }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(260px, .65fr);
      gap: 28px;
      align-items: end;
      padding: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    .eyebrow {{
      display: inline-flex;
      gap: 8px;
      align-items: center;
      color: var(--red);
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
      font-size: 12px;
    }}
    h1, h2, h3 {{ margin: 0; line-height: 1.18; }}
    h1 {{
      margin-top: 12px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(36px, 6vw, 68px);
      letter-spacing: 0;
    }}
    .subtitle {{ margin: 18px 0 0; max-width: 820px; color: var(--muted); font-size: 17px; }}
    .stamp {{
      border-left: 5px solid var(--red);
      padding: 18px;
      background: var(--soft-red);
      border-radius: 8px;
    }}
    .stamp p {{ margin: 4px 0; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 18px 0 0;
    }}
    .metric {{
      padding: 15px;
      min-height: 112px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .72);
    }}
    .metric span, .metric small {{ display: block; color: var(--muted); }}
    .metric strong {{ display: block; margin: 8px 0 5px; font-size: 28px; line-height: 1; }}
    section {{
      margin-top: 28px;
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .92);
    }}
    section > header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }}
    section h2 {{ font-family: Georgia, "Times New Roman", serif; font-size: 29px; }}
    .tag {{
      align-self: start;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      color: var(--muted);
      background: #fbfbf8;
      font-size: 12px;
      white-space: nowrap;
    }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}
    .card {{
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .card h3 {{ margin-bottom: 10px; font-size: 16px; }}
    .card ul {{ margin: 0; padding-left: 18px; }}
    .map-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(170px, 1fr));
      gap: 12px;
    }}
    .lane {{
      position: relative;
      min-height: 210px;
      padding: 18px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #fff;
    }}
    .lane::before {{
      content: attr(data-layer);
      display: inline-block;
      margin-bottom: 14px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--ink);
      color: #fff;
      font-size: 12px;
      font-weight: 700;
    }}
    .lane:nth-child(2) {{ background: var(--soft-blue); border-color: var(--blue); }}
    .lane:nth-child(3) {{ background: var(--soft-green); border-color: var(--green); }}
    .lane:nth-child(4) {{ background: #fff5dc; border-color: var(--gold); }}
    .lane p {{ margin: 9px 0 0; color: var(--muted); }}
    .flow {{
      display: grid;
      grid-template-columns: 1fr auto 1fr auto 1fr;
      gap: 12px;
      align-items: center;
      margin-top: 16px;
    }}
    .flow-node {{
      min-height: 112px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .arrow {{ color: var(--red); font-size: 28px; font-family: Georgia, serif; }}
    .route-list {{ display: grid; gap: 12px; }}
    .route-item {{
      display: grid;
      grid-template-columns: 48px 1fr;
      gap: 14px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .route-index {{
      display: grid;
      place-items: center;
      width: 42px;
      height: 42px;
      border-radius: 50%;
      background: var(--ink);
      color: #fff;
      font-weight: 800;
    }}
    .route-item p {{ margin: 7px 0; }}
    .evidence-chip {{
      display: inline-block;
      padding: 4px 9px;
      border-radius: 999px;
      background: var(--soft-green);
      color: var(--green);
      font-size: 12px;
      font-weight: 700;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    th, td {{
      padding: 12px 13px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: #f0efe8; font-size: 13px; }}
    tr:last-child td {{ border-bottom: 0; }}
    .level {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--soft-blue);
      color: var(--blue);
      font-weight: 800;
      font-size: 12px;
    }}
    .edge-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .edge-block {{
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .edge-block span {{
      display: block;
      margin: 8px 0 4px;
      color: var(--red);
      font-weight: 800;
    }}
    .muted {{ color: var(--muted); }}
    .next-questions {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .question {{
      padding: 16px;
      border-radius: 8px;
      background: #171717;
      color: #fff;
    }}
    .question strong {{ color: #f4d35e; }}
    footer {{ margin-top: 24px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 980px) {{
      .page {{ display: block; }}
      aside {{ position: static; height: auto; }}
      main {{ padding: 20px; }}
      .hero, .metrics, .cards, .map-grid, .edge-grid, .next-questions {{ grid-template-columns: 1fr; }}
      .flow {{ grid-template-columns: 1fr; }}
      .arrow {{ display: none; }}
    }}
    @media print {{
      aside {{ display: none; }}
      .page {{ display: block; }}
      body {{ background: #fff; }}
      main {{ padding: 0; }}
      section, .hero {{ box-shadow: none; break-inside: avoid; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <aside>
      <p class="brand">Source<br>Architecture<br>Navigator</p>
      <p class="side-note">只读源码导航产物。先建立路线，再决定是否下钻或施工。</p>
      <nav>
        <a href="#l0">L0 项目识别</a>
        <a href="#maps">L1-L3 分层地图</a>
        <a href="#route">阅读路线</a>
        <a href="#evidence">证据表</a>
        <a href="#samples">符号与依赖样本</a>
        <a href="#next">下一问</a>
      </nav>
    </aside>
    <main>
      <div class="hero">
        <div>
          <span class="eyebrow">read-only navigation</span>
          <h1>{h(title)}</h1>
          <p class="subtitle">{h(subtitle or project_hint)}</p>
        </div>
        <div class="stamp">
          <p><b>生成时间</b> {h(now)}</p>
          <p><b>输入</b> {code(source_label)}</p>
          <p><b>扫描根</b> {code(report.get('root', scan_root))}</p>
          <p><b>生命周期</b> {h(lifecycle or '源目录保持不变')}</p>
        </div>
      </div>

      <div class="metrics">
        {render_metric("扫描文件", report.get("file_count_scanned", 0), "受 --max-files 限制")}
        {render_metric("主语言信号", primary, "基于扩展名统计")}
        {render_metric("入口候选", len(entries), "静态路径/命名启发")}
        {render_metric("zip 成员", zip_stats.get("members", "n/a"), "仅 zip 输入显示")}
      </div>

      <section id="l0">
        <header>
          <h2>L0 项目识别卡</h2>
          <span class="tag">先知道做什么，先不追所有细节</span>
        </header>
        <div class="cards">
          <div class="card">
            <h3>项目目标</h3>
            <p>{h(project_hint)}</p>
          </div>
          <div class="card">
            <h3>入口候选</h3>
            <ul>{render_list(entries[:6])}</ul>
          </div>
          <div class="card">
            <h3>配置/manifest</h3>
            <ul>{render_list(manifests[:6])}</ul>
          </div>
        </div>
        <div class="cards" style="margin-top:14px">
          <div class="card">
            <h3>主要目录</h3>
            <ul>{"".join(f"<li>{code(name)} <span class='muted'>({count})</span></li>" for name, count in list(top_dirs.items())[:6]) or "<li>未发现</li>"}</ul>
          </div>
          <div class="card">
            <h3>核心对象线索</h3>
            <p>{h(core_object)}</p>
            <p>{path_link(core_location, scan_root, linkable)}</p>
          </div>
          <div class="card">
            <h3>当前不确定项</h3>
            <p>探针只提供静态线索。运行时行为、废弃代码和真实数据流需要继续用 L1/L2/L3 证据补齐。</p>
          </div>
        </div>
      </section>

      <section id="maps">
        <header>
          <h2>L1-L3 分层地图</h2>
          <span class="tag">每层只回答一个阅读问题</span>
        </header>
        <div class="map-grid">
          <div class="lane" data-layer="L0">
            <h3>项目识别</h3>
            <p>输入源、语言信号、入口候选、manifest 和可先跳过的边缘路径。</p>
          </div>
          <div class="lane" data-layer="L1">
            <h3>核心对象</h3>
            <p>{h(core_object)}</p>
            <p>{path_link(core_location, scan_root, linkable)}</p>
          </div>
          <div class="lane" data-layer="L2">
            <h3>调用/依赖链</h3>
            <p>{h(edge_line)}</p>
          </div>
          <div class="lane" data-layer="L3">
            <h3>功能/数据流</h3>
            <p>用下一轮源码阅读确认输入、转换、输出和副作用，而不是一次画完整大网。</p>
          </div>
        </div>
        <div class="flow" aria-label="功能流草图">
          <div class="flow-node"><b>入口</b><p>{code(compact(flow_entry))}</p></div>
          <div class="arrow">→</div>
          <div class="flow-node"><b>核心编排</b><p>{path_link(flow_core, scan_root, linkable)}</p></div>
          <div class="arrow">→</div>
          <div class="flow-node"><b>输出/副作用</b><p>{h(flow_output)}</p></div>
        </div>
      </section>

      <section id="route">
        <header>
          <h2>建议先看 3-5 个文件</h2>
          <span class="tag">入口 -> 编排 -> 核心对象 -> 配置</span>
        </header>
        <div class="route-list">{render_route(route, scan_root, linkable)}</div>
      </section>

      <section id="evidence">
        <header>
          <h2>证据表</h2>
          <span class="tag">推断必须留待下一轮查证</span>
        </header>
        {render_evidence(evidence_rows)}
      </section>

      <section id="samples">
        <header>
          <h2>符号与依赖样本</h2>
          <span class="tag">静态样本，不等同完整调用图</span>
        </header>
        <h3 style="margin:0 0 12px">符号样本</h3>
        {render_symbol_table(report, scan_root, linkable)}
        <h3 style="margin:22px 0 12px">导入边样本</h3>
        <div class="edge-grid">{render_edge_groups(report)}</div>
      </section>

      <section id="next">
        <header>
          <h2>下一问</h2>
          <span class="tag">避免重新发散</span>
        </header>
        <div class="next-questions">
          <div class="question"><strong>下钻</strong><br>请只追踪 {h(core_object)}，给我 L1 地图和上下游。</div>
          <div class="question"><strong>横向</strong><br>请比较两个相邻模块是否在重复做同一件事，只给有证据的结论。</div>
          <div class="question"><strong>施工前</strong><br>基于这份报告，把目标转成施工边界卡，先不要改代码。</div>
        </div>
      </section>

      <footer>
        本报告由只读静态探针生成。它适合建立阅读路线，不替代运行、测试或人工源码复核。
      </footer>
    </main>
  </div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", default=".", help="Repository directory or source .zip")
    parser.add_argument("--output", required=True, help="HTML output path, preferably outside the scanned repository")
    parser.add_argument("--title", help="Report title; defaults to the source folder or archive name")
    parser.add_argument("--subtitle", help="Optional subtitle shown under the title")
    parser.add_argument("--max-files", type=int, default=8000, help="Maximum files to scan")
    parser.add_argument("--extract-to", help="Optional directory for zip extraction; keep it outside the target repo")
    parser.add_argument("--keep-temp", action="store_true", help="Keep an auto-created zip extraction directory")
    parser.add_argument(
        "--allow-output-in-repo",
        action="store_true",
        help="Allow --output inside the scanned directory; default rejects this to avoid repo pollution",
    )
    args = parser.parse_args(argv)

    source = Path(args.source)
    output_path = Path(args.output).resolve()
    cleanup: tempfile.TemporaryDirectory[str] | None = None
    try:
        report, scan_root, cleanup = prepare_report(
            source=source,
            output_path=output_path,
            max_files=args.max_files,
            extract_to=args.extract_to,
            keep_temp=args.keep_temp,
            allow_output_in_repo=args.allow_output_in_repo,
        )
        title = args.title or source.resolve().stem or REPORT_HEADING
        document = render_html(report, scan_root=scan_root, title=title, subtitle=args.subtitle)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
        print(f"HTML report written: {output_path}")
        return 0
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup is not None:
            cleanup.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
