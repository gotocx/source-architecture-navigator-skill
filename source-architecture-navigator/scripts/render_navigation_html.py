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


REPORT_HEADING = "源码一次全解析报告"
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
    line = re.sub(r"[*`>#\[\]()]|\!\[[^\]]*\]", "", line)
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
    max_symbols: int,
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

        report = repo_probe.build_report(scan_root, max_files=max_files, max_symbols=max_symbols)
        report["source_archive"] = str(input_path)
        report["extract_root"] = str(scan_root)
        report["extract_root_lifecycle"] = lifecycle
        report["zip_stats"] = zip_stats
        return report, scan_root, cleanup

    if input_path.is_dir():
        if repo_probe.is_relative_to(output_path, input_path) and not allow_output_in_repo:
            raise ValueError("--output is inside the scanned repository; choose a path outside it")
        return repo_probe.build_report(input_path, max_files=max_files, max_symbols=max_symbols), input_path, cleanup

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


INTERNAL_MARKERS = {
    "pipeline",
    "routes",
    "api",
    "pages",
    "components",
    "state",
    "services",
    "models",
    "schemas",
    "db",
    "data",
    "train",
    "eval",
    "flow",
    "depth",
    "monitor",
    "utils",
    "config",
}


def module_key(path: str) -> str:
    parts = Path(path).parts
    if not parts:
        return "(root)"
    lowered = [part.lower() for part in parts]
    if lowered[0] == "scripts":
        return "scripts/"
    if lowered[0] in {"tests", "test"}:
        return "tests/"
    if lowered[0] == "src" and len(parts) >= 3:
        for part in parts[2:-1]:
            if part.lower() in INTERNAL_MARKERS:
                return f"{part}/"
        if len(parts) >= 4:
            return f"{parts[2]}/"
    if len(parts) >= 2:
        return f"{parts[0]}/"
    return "(root)"


def target_module(target: str) -> str:
    parts = [part for part in re.split(r"[./\\]", target) if part and part not in {"src"}]
    for part in parts:
        if part.lower() in INTERNAL_MARKERS:
            return f"{part}/"
    if len(parts) >= 2 and parts[0] not in {"typing", "pathlib", "os", "sys", "json", "re"}:
        return f"{parts[-2]}/"
    return "external"


def build_module_profiles(report: dict) -> list[dict]:
    profiles: dict[str, dict] = {}
    for rel in report.get("source_files", []):
        key = module_key(rel)
        profile = profiles.setdefault(
            key,
            {"module": key, "files": [], "symbols": [], "depends_on": set(), "depended_by": set(), "lines": 0},
        )
        profile["files"].append(rel)
        profile["lines"] += int(report.get("line_counts", {}).get(rel, 0))

    for symbol in report.get("symbol_samples", []):
        key = module_key(symbol["path"])
        profile = profiles.setdefault(
            key,
            {"module": key, "files": [], "symbols": [], "depends_on": set(), "depended_by": set(), "lines": 0},
        )
        profile["symbols"].append(symbol)

    for edge in report.get("import_edge_samples", []):
        source_key = module_key(edge["source"])
        target_key = target_module(edge["target"])
        if target_key != source_key:
            source = profiles.setdefault(
                source_key,
                {"module": source_key, "files": [], "symbols": [], "depends_on": set(), "depended_by": set(), "lines": 0},
            )
            source["depends_on"].add(target_key)
            if target_key != "external":
                target = profiles.setdefault(
                    target_key,
                    {"module": target_key, "files": [], "symbols": [], "depends_on": set(), "depended_by": set(), "lines": 0},
                )
                target["depended_by"].add(source_key)

    result = []
    for profile in profiles.values():
        profile["files"] = unique(profile["files"])
        profile["depends_on"] = sorted(profile["depends_on"])
        profile["depended_by"] = sorted(profile["depended_by"])
        result.append(profile)
    return sorted(result, key=lambda item: (-len(item["symbols"]), item["module"]))


def role_for_module(name: str, profile: dict | None = None) -> str:
    lower = name.lower()
    if lower == "scripts/":
        return "启动、烟测、导出、性能脚本"
    if "pipeline" in lower:
        return "主流程编排和数据结构汇合点"
    if "depth" in lower:
        return "深度估计、深度到视差转换"
    if "flow" in lower:
        return "几何搬运、光流、时序推导"
    if "train" in lower or "model" in lower:
        return "模型输入契约、训练数据和网络实现"
    if "eval" in lower:
        return "评估、可视化和指标输出"
    if "data" in lower:
        return "数据读取、预处理或缓存"
    if "monitor" in lower:
        return "运行观测和旁路探针"
    if lower == "tests/":
        return "行为验证和回归保护"
    if lower == "(root)":
        return "根目录级源码、启动说明或工程配置"
    if profile:
        file_count = len(profile.get("files", []))
        symbol_count = len(profile.get("symbols", []))
        depends = [item for item in profile.get("depends_on", []) if item != "external"]
        depended_by = profile.get("depended_by", [])
        if depends and depended_by:
            return f"{file_count} 个文件 / {symbol_count} 个对象，连接 {', '.join(depends[:2])} 与 {', '.join(depended_by[:2])}"
        if depends:
            return f"{file_count} 个文件 / {symbol_count} 个对象，向下使用 {', '.join(depends[:3])}"
        if depended_by:
            return f"{file_count} 个文件 / {symbol_count} 个对象，被 {', '.join(depended_by[:3])} 引用"
        if symbol_count:
            return f"{file_count} 个文件 / {symbol_count} 个对象，当前静态 import 边较少"
        if file_count:
            return f"{file_count} 个源码文件，主要用于局部实现或配置承载"
    return "根路径或小型模块承载层"


def coupling_label(profile: dict) -> tuple[str, str]:
    score = len(profile["depends_on"]) + len(profile["depended_by"])
    if "external" in profile["depends_on"]:
        score -= 1
    if score >= 6:
        return "高", "hot"
    if score >= 2:
        return "中", "warm"
    return "低", "cool"


def symbol_signature(item: dict) -> str:
    signature = item.get("signature") or item.get("name", "")
    return compact(signature, 120)


def signature_inputs(item: dict) -> str:
    signature = item.get("signature") or ""
    match = re.search(r"\((.*?)\)", signature)
    if not match:
        return "无显式参数"
    params = []
    for raw in match.group(1).split(","):
        param = raw.strip()
        if not param or param in {"self", "cls"}:
            continue
        params.append(compact(param.split("=", 1)[0].strip(), 36))
    return ", ".join(params[:5]) if params else "无显式业务参数"


def signature_output(item: dict) -> str:
    signature = item.get("signature") or ""
    match = re.search(r"\)\s*->\s*([^:]+)", signature)
    if match:
        return compact(match.group(1).strip(), 54)
    name = item.get("name", "").lower()
    kind = item.get("kind", "symbol")
    if kind == "class":
        return "实例对象 / 类型边界"
    if "__init__" in name:
        return "实例状态"
    if any(mark in name for mark in ["build", "make", "create"]):
        return "构造结果"
    if any(mark in name for mark in ["load", "read", "parse", "fetch"]):
        return "读取或解析结果"
    if any(mark in name for mark in ["save", "write", "export", "render"]):
        return "输出产物或落盘结果"
    return "返回值由函数体和调用方消费"


def symbol_effect_hint(item: dict) -> str:
    name = item.get("name", "").lower()
    path = item.get("path", "").lower()
    if any(mark in name for mark in ["save", "write", "export", "render"]):
        return "输出、渲染或落盘候选"
    if any(mark in name for mark in ["load", "read", "fetch", "parse"]):
        return "读取、解析或缓存候选"
    if any(mark in name for mark in ["run", "main", "execute", "handle", "process"]):
        return "流程编排或命令触发"
    if any(mark in path for mark in ["/eval", "\\eval", "/monitor", "\\monitor"]):
        return "指标、观测或报告输出"
    if any(mark in path for mark in ["/train", "\\train", "/model", "\\model"]):
        return "张量计算、模型状态或训练数据"
    if any(mark in path for mark in ["/flow", "\\flow", "/depth", "\\depth"]):
        return "几何/深度转换，注意张量方向"
    return "静态未见 I/O 关键词，优先按局部转换阅读"


def render_symbol_facts(item: dict, profile: dict) -> str:
    depends = [dep for dep in profile.get("depends_on", []) if dep != "external"]
    upstream = ", ".join(profile.get("depended_by", [])[:3]) or "未发现模块级上游 import"
    downstream = ", ".join(depends[:3]) or "无明显模块级下游 import"
    facts = [
        ("对象职责", object_role_hint(item, profile)),
        ("签名边界", symbol_signature(item)),
        ("输入", signature_inputs(item)),
        ("输出", signature_output(item)),
        ("调用证据", f"上游 {upstream} / 下游 {downstream}"),
        ("副作用", symbol_effect_hint(item)),
    ]
    return "".join(
        f"<span><b>{h(label)}</b>{h(value)}</span>"
        for label, value in facts
    )


def object_role_hint(item: dict, profile: dict) -> str:
    name = item.get("name", "")
    lower = name.lower()
    path = item.get("path", "").lower()
    module = profile.get("module", "")
    if item.get("kind") == "class":
        if any(mark in lower for mark in ["record", "sample", "input", "output", "state", "result", "config"]):
            return "数据结构/契约边界，先看字段含义和消费方"
        if "dataset" in lower:
            return "训练或评估样本集合，先看索引、读取和返回结构"
        if any(mark in lower for mark in ["pipeline", "runner", "engine"]):
            return "流程编排对象，先看构造依赖和公开方法"
        if any(mark in lower for mark in ["model", "net", "unet", "module"]):
            return "模型/网络结构，先看输入通道和 forward 输出"
        return "类边界，先看构造参数、公开方法和实例化位置"
    if lower in {"main", "run", "execute"} or lower.startswith("run_"):
        return "入口或任务执行函数，先看参数装配、主调用和输出收口"
    if "parse_arg" in lower:
        return "命令行参数解析，先看默认值、路径参数和开关"
    if "cuda" in lower or "memory" in lower or "mem" in lower:
        return "设备/显存观测函数，先看读取口径和调用时机"
    if "time" in lower or "stage" in lower or "timer" in lower:
        return "阶段计时/性能记录函数，先看包裹范围和输出字段"
    if any(mark in lower for mark in ["pair", "sequence", "frame"]):
        return "帧/序列处理节点，先看输入帧、状态传递和输出对象"
    if any(mark in lower for mark in ["depth", "disp", "disparity"]):
        return "深度/视差转换节点，先看尺度、方向和张量形状"
    if any(mark in lower for mark in ["flow", "warp", "splat", "dibr"]):
        return "几何搬运节点，先看坐标方向、遮挡和有效掩码"
    if any(mark in lower for mark in ["load", "read", "fetch", "parse"]):
        return "读取/解析节点，先看输入来源、格式和异常路径"
    if any(mark in lower for mark in ["save", "write", "export", "render"]):
        return "输出/落盘节点，先看产物格式、路径和调用方"
    if any(mark in lower for mark in ["metric", "psnr", "ssim", "iou", "eval"]):
        return "评估指标节点，先看度量输入、统计口径和输出字段"
    if any(mark in lower for mark in ["loss", "train", "forward", "patch"]):
        return "训练/推理节点，先看张量输入、梯度边界和返回值"
    if "scripts/" in module:
        script_name = Path(item.get("path", "")).stem
        return f"{script_name} 脚本内对象，先判断它是入口、计时、导出还是烟测辅助"
    if "test" in path:
        return "测试验证对象，先看它保护的行为和断言边界"
    return f"{module or '当前模块'} 中的局部处理节点，先从调用证据和返回值定位职责"


def render_full_parse_matrix(report: dict, profiles: list[dict], risks: list[dict]) -> str:
    checks = [
        ("项目识别", bool(report.get("readme_files") or report.get("manifests")), "README / manifest / 目录形状"),
        ("入口识别", bool(report.get("entry_candidates")), "脚本、路由、页面或任务入口"),
        ("全局分层", bool(profiles), "按目录和源码模块分层"),
        ("模块依赖", bool(report.get("import_edge_samples")), "静态 import / require 边"),
        ("符号索引", bool(report.get("symbol_samples")), f"{len(report.get('symbol_samples', []))} / {report.get('symbol_total', len(report.get('symbol_samples', [])))} 个函数、类、方法"),
        ("配置检查", bool(report.get("config_files") or report.get("manifests")), "manifest、配置文件、默认值线索"),
        ("测试入口", bool(report.get("test_files")), "tests/ 或 test_* 文件"),
        ("风险候选", bool(risks), "只列有静态证据的断点"),
    ]
    cells = []
    for label, ok, detail in checks:
        state = "已覆盖" if ok else "待补证据"
        tone = "ok" if ok else "todo"
        cells.append(
            f"""
            <div class="parse-cell {tone}">
              <strong>{h(label)}</strong>
              <span>{h(state)}</span>
              <p>{h(detail)}</p>
            </div>
            """
        )
    return "\n".join(cells)


def render_project_table(report: dict, project_hint: str, primary: str, profiles: list[dict]) -> str:
    entries = report.get("entry_candidates", [])
    manifests = report.get("manifests", [])
    rows = [
        ("项目目标线索", project_hint),
        ("主语言信号", primary),
        ("源码文件", f"{len(report.get('source_files', []))} 个"),
        ("入口候选", ", ".join(entries[:4]) or UNKNOWN),
        ("核心模块候选", ", ".join(profile["module"] for profile in profiles[:6]) or UNKNOWN),
        ("配置/manifest", ", ".join((manifests + report.get("config_files", []))[:5]) or "未发现"),
        ("测试入口", ", ".join(report.get("test_files", [])[:5]) or "未发现"),
        ("边界提醒", "这是静态一次全解析报告；本页承担完整阅读地图，运行时行为用测试、命令输出或日志补证。"),
    ]
    body = "".join(f"<tr><th>{h(key)}</th><td>{h(value)}</td></tr>" for key, value in rows)
    return f"<table class=\"fact-table\"><tbody>{body}</tbody></table>"


def render_layer_diagram(profiles: list[dict], report: dict) -> str:
    def pick(predicate, fallback: str) -> str:
        names = [profile["module"] for profile in profiles if predicate(profile["module"].lower())]
        return "  ".join(names[:6]) if names else fallback

    scripts = pick(lambda name: "scripts" in name, "scripts/ 或启动入口待确认")
    interface = pick(lambda name: any(mark in name for mark in ["pages", "routes", "api"]), "接口/页面层未在静态入口中显式出现")
    orchestration = pick(lambda name: "pipeline" in name or "service" in name, "编排层待从入口继续确认")
    domain = pick(lambda name: any(mark in name for mark in ["depth", "flow", "train", "model", "data"]), "核心领域模块待确认")
    support = pick(lambda name: any(mark in name for mark in ["eval", "monitor", "utils", "config"]), "评估/工具/配置层待确认")
    tests = "  ".join(report.get("test_files", [])[:3]) or "测试入口未发现"
    return f"""
<pre class="diagram">┌──────────────────────────────────────────────────────────────┐
│ 启动/脚本层                                                   │
│ {h(scripts):<60} │
├──────────────────────────────────────────────────────────────┤
│ 页面/接口/任务入口层                                          │
│ {h(interface):<60} │
├──────────────────────────────────────────────────────────────┤
│ 编排层                                                       │
│ {h(orchestration):<60} │
├──────────────────────────────────────────────────────────────┤
│ 核心领域模块                                                 │
│ {h(domain):<60} │
├──────────────────────────────────────────────────────────────┤
│ 支撑/配置/评估层                                              │
│ {h(support):<60} │
├──────────────────────────────────────────────────────────────┤
│ 测试/验证层                                                   │
│ {h(tests):<60} │
└──────────────────────────────────────────────────────────────┘</pre>
"""


def render_reading_ladder(report: dict) -> str:
    symbol_count = len(report.get("symbol_samples", []))
    symbol_total = report.get("symbol_total", symbol_count)
    return f"""
    <div class="reading-ladder">
      <div class="ladder-step">
        <b>L3</b>
        <strong>先看系统分层</strong>
        <span>回答“这个项目由哪些层组成，主链从哪里到哪里”。</span>
      </div>
      <div class="ladder-arrow">→</div>
      <div class="ladder-step">
        <b>L2</b>
        <strong>再看模块边界</strong>
        <span>回答“哪个模块依赖谁，哪里可能耦合或断裂”。</span>
      </div>
      <div class="ladder-arrow">→</div>
      <div class="ladder-step">
        <b>L1</b>
        <strong>最后挑函数/类</strong>
        <span>代表卡看局部职责，全量索引定位 {symbol_count} / {symbol_total} 个对象。</span>
      </div>
    </div>
    """


def render_module_table(profiles: list[dict]) -> str:
    if not profiles:
        return '<p class="muted">没有足够的模块线索。</p>'
    rows = []
    for profile in profiles[:14]:
        label, tone = coupling_label(profile)
        rows.append(
            f"""
            <tr>
              <td><b>{h(profile['module'])}</b></td>
              <td>{h(role_for_module(profile['module'], profile))}</td>
              <td>{h(len(profile['files']))}</td>
              <td>{h(len(profile['symbols']))}</td>
              <td>{h(', '.join(profile['depends_on'][:5]) or '无明显内部依赖')}</td>
              <td>{h(', '.join(profile['depended_by'][:5]) or '未发现')}</td>
              <td><span class="coupling {tone}">{h(label)}</span></td>
            </tr>
            """
        )
    return f"""
    <table>
      <thead><tr><th>模块</th><th>职责推断</th><th>文件</th><th>符号</th><th>依赖谁</th><th>被谁依赖</th><th>耦合</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_function_groups(profiles: list[dict], scan_root: Path, linkable: bool) -> str:
    groups = []
    for profile in profiles[:6]:
        symbols = profile["symbols"][:6]
        if not symbols:
            continue
        cards = []
        for item in symbols:
            badge = item.get("kind", "symbol")
            search_text = f"{profile['module']} {item.get('name', '')} {item.get('signature', '')} {item.get('path', '')}"
            cards.append(
                f"""
                <article class="func-card" data-symbol-text="{h(search_text).lower()}">
                  <div class="sig">{h(symbol_signature(item))}</div>
                  <div class="meta"><span>{h(badge)}</span>{path_link(f"{item['path']}:{item['line']}", scan_root, linkable)}</div>
                  <div class="func-facts">{render_symbol_facts(item, profile)}</div>
                </article>
                """
            )
        groups.append(
            f"""
            <details class="function-group" open>
              <summary>{h(profile['module'])} <small>{h(role_for_module(profile['module'], profile))} · {len(symbols)} 个代表符号</small></summary>
              {''.join(cards)}
            </details>
            """
        )
    return "\n".join(groups) or '<p class="muted">未抽取到函数、类或组件定义。</p>'


def render_symbol_inventory(report: dict, scan_root: Path, linkable: bool) -> str:
    symbols = report.get("symbol_samples", [])
    if not symbols:
        return '<p class="muted">未抽取到函数、类或组件定义。</p>'
    rows = []
    for index, item in enumerate(symbols, start=1):
        name = item.get("name", UNKNOWN)
        kind = item.get("kind", "symbol")
        path = item.get("path", "")
        line = item.get("line", 1)
        module = module_key(path)
        location = f"{path}:{line}"
        signature = symbol_signature(item)
        search_text = f"{index} {name} {kind} {module} {signature} {location}"
        rows.append(
            f"""
            <tr id="symbol-{index}" class="symbol-row"
                data-symbol-text="{h(search_text).lower()}"
                data-symbol-name="{h(name)}"
                data-symbol-kind="{h(kind)}"
                data-symbol-module="{h(module)}"
                data-symbol-location="{h(location)}">
              <td class="symbol-index">{index}</td>
              <td><code>{h(name)}</code><small>{h(compact(signature, 140))}</small></td>
              <td>{h(kind)}</td>
              <td>{h(module)}</td>
              <td>{path_link(location, scan_root, linkable)}</td>
            </tr>
            """
        )
    return f"""
    <div class="inventory-wrap">
      <table class="inventory-table">
        <thead><tr><th>#</th><th>符号</th><th>类型</th><th>层级/模块</th><th>证据位置</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """


def infer_symbol_note(item: dict, module: str = "") -> str:
    name = item.get("name", "")
    lower = name.lower()
    if any(mark in lower for mark in ["config", "settings", "options"]):
        return "负责承载配置或参数边界；阅读时先列默认值、覆盖来源和实际使用点。"
    if any(mark in lower for mark in ["input", "output", "state", "result", "summary", "schema"]):
        return "负责描述数据契约；阅读时先拆字段来源、转换位置、消费方和返回边界。"
    if any(mark in lower for mark in ["run", "main", "execute", "handle", "process"]):
        return "负责触发运行或编排流程；阅读时按入口、核心调用、输出收口梳理主链。"
    if any(mark in lower for mark in ["load", "read", "fetch", "parse"]):
        return "负责读取、解析或装配输入；阅读时确认来源、格式、异常路径和副作用。"
    if any(mark in lower for mark in ["save", "write", "export", "render"]):
        return "负责生成、渲染或落盘输出；阅读时确认产物格式、保存位置和调用方。"
    if item.get("kind") == "class":
        return "负责定义类级边界；阅读时先列构造参数、公开方法、实例化位置和状态变化。"
    kind = item.get("kind", "symbol")
    role = role_for_module(module) if module else "当前模块职责"
    return f"负责承接“{role}”中的{kind}边界；阅读时按签名梳理输入、处理、输出、调用点和副作用。"


def render_golden_path(route: list[dict], report: dict) -> str:
    steps = []
    labels = ["入口装配", "核心编排", "核心对象", "边界适配", "验证/配置"]
    for idx, item in enumerate(route[:5]):
        steps.append(
            f"""
            <div class="timeline-item">
              <h3>{idx + 1}. {h(labels[idx] if idx < len(labels) else '下一层')}</h3>
              <p>{code(item['path'])}</p>
              <small>{h(item['purpose'])}</small>
            </div>
            """
        )
    if not steps:
        steps.append('<div class="timeline-item"><h3>1. 待确认入口</h3><p>先补充可运行入口或文件清单。</p></div>')
    return "\n".join(steps)


def build_risks(report: dict, profiles: list[dict]) -> list[dict]:
    risks: list[dict] = []
    if not report.get("readme_files"):
        risks.append({"level": "P2", "kind": "目标线索缺失", "text": "未发现 README，项目目标需要从入口和 manifest 反推。", "fix": "先补项目目标、运行入口和硬边界。"})
    if not report.get("entry_candidates"):
        risks.append({"level": "P1", "kind": "入口不清", "text": "静态探针没有找到明确入口，阅读路线容易碎片化。", "fix": "先用文件清单或启动命令确定 main/script/route。"})
    if len(report.get("entry_candidates", [])) > 12:
        risks.append({"level": "P2", "kind": "入口过多", "text": f"发现 {len(report.get('entry_candidates', []))} 个入口候选，脚本/任务入口可能分散。", "fix": "按生产入口、测试入口、一次性脚本分组。"})
    if len(report.get("script_files", [])) > len(report.get("source_files", [])) * 0.45 and report.get("source_files"):
        risks.append({"level": "P2", "kind": "脚本占比高", "text": "scripts/ 文件占比较高，容易把一次性实验路径误读成主链路。", "fix": "先标记核心脚本和边缘脚本。"})
    if not report.get("test_files"):
        risks.append({"level": "P2", "kind": "测试入口缺失", "text": "未发现 tests/ 或 test_* 文件，行为判断缺少回归证据。", "fix": "阅读后若要施工，先补最小验收命令。"})
    for profile in profiles[:10]:
        label, _ = coupling_label(profile)
        if label == "高":
            risks.append(
                {
                    "level": "P1",
                    "kind": f"{profile['module']} 耦合偏高",
                    "text": f"该模块同时依赖 {len(profile['depends_on'])} 个模块，并被 {len(profile['depended_by'])} 个模块依赖。",
                    "fix": "在全量符号索引中定位该模块入口和契约对象，先判断它是编排层还是领域层。"
                }
            )
    return risks[:8]


def render_risks(risks: list[dict]) -> str:
    if not risks:
        return '<div class="note good"><b>未发现明显静态断点。</b><p>这不代表没有问题，只表示首轮探针没有足够证据列出风险。</p></div>'
    return "\n".join(
        f"""
        <div class="issue">
          <span>{h(item['level'])}</span>
          <h3>{h(item['kind'])}</h3>
          <p>{h(item['text'])}</p>
          <small>{h(item['fix'])}</small>
        </div>
        """
        for item in risks
    )


def render_dual_routes(route: list[dict], report: dict, scan_root: Path, linkable: bool) -> str:
    beginner = []
    for idx, item in enumerate(route[:4], start=1):
        beginner.append(
            f"<li><b>{path_link(item['path'], scan_root, linkable)}</b><span>{h(item['purpose'])}</span></li>"
        )
    seen_paths = {item["path"] for item in route[:4]}
    expert_symbols = [item for item in report.get("symbol_samples", []) if item.get("path") not in seen_paths][:4]
    expert = []
    for item in expert_symbols:
        location = f"{item['path']}:{item['line']}"
        expert.append(
            f"<li><b>{path_link(location, scan_root, linkable)}</b><span>{h(symbol_signature(item))}</span></li>"
        )
    return f"""
    <div class="route-panel">
      <h3>新手路线：建立全局认知</h3>
      <ol>{''.join(beginner) or '<li>先补入口文件或 README。</li>'}</ol>
    </div>
    <div class="route-panel expert">
      <h3>高手路线：直接看核心对象</h3>
      <ol>{''.join(expert) or '<li>先补充符号抽样。</li>'}</ol>
    </div>
    """


def render_contract_candidates(report: dict, scan_root: Path, linkable: bool) -> str:
    candidates = [
        item for item in report.get("symbol_samples", [])
        if re.search(r"(Input|Output|Config|State|Result|Summary|Schema|Payload|Request|Response)", item.get("name", ""))
    ][:8]
    rows = []
    for item in candidates:
        rows.append(
            f"""
            <tr>
              <td>{code(item['name'])}</td>
              <td>{h(item.get('kind', 'symbol'))}</td>
              <td>{path_link(f"{item['path']}:{item['line']}", scan_root, linkable)}</td>
            </tr>
            """
        )
    if not rows:
        return '<p class="muted">未识别到明显的 Input/Output/Config/State 等契约对象。</p>'
    return f"""
    <table>
      <thead><tr><th>对象</th><th>类型</th><th>位置</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """


def render_config_table(report: dict) -> str:
    items = unique(report.get("manifests", []) + report.get("config_files", []))[:16]
    if not items:
        return '<p class="muted">未发现 manifest 或配置文件。</p>'
    rows = []
    for rel in items:
        kind = "manifest" if Path(rel).name in repo_probe.MANIFEST_NAMES else "config"
        rows.append(f"<tr><td>{code(rel)}</td><td>{h(kind)}</td><td>检查默认值、环境变量覆盖和启动参数是否一致。</td></tr>")
    return f"""
    <table>
      <thead><tr><th>文件</th><th>类型</th><th>阅读目标</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
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
            <p>用本报告的符号索引和证据表确认输入、转换、输出和副作用，不把所有节点塞进一张大网。</p>
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
          <span class="tag">推断必须标注证据等级</span>
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


def render_full_html(report: dict, scan_root: Path, title: str, subtitle: str | None) -> str:
    route = build_reading_route(report)
    profiles = build_module_profiles(report)
    risks = build_risks(report, profiles)
    evidence_rows = build_evidence_rows(report, route)
    project_hint = read_project_hint(scan_root, report.get("readme_files", []))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lifecycle = report.get("extract_root_lifecycle", "")
    linkable = not lifecycle.startswith("auto-cleaned")
    source_label = report.get("source_archive") or report.get("root")
    zip_stats = report.get("zip_stats", {})
    primary = first_language(report)
    entries = report.get("entry_candidates", [])
    source_files = report.get("source_files", [])
    symbol_count = len(report.get("symbol_samples", []))
    edge_count = len(report.get("import_edge_samples", []))
    manifest_count = len(report.get("manifests", [])) + len(report.get("config_files", []))
    symbol_total = report.get("symbol_total", symbol_count)
    symbol_limit = report.get("symbol_limit", symbol_count)
    symbol_tag = f"展示 {symbol_count} / {symbol_total} 个对象"
    if report.get("symbol_truncated"):
        symbol_tag += f" · 上限 {symbol_limit}"

    first_symbol = choose_symbol(report)
    focus_target = first_symbol["name"] if first_symbol else "首个核心对象"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)} · {REPORT_HEADING}</title>
  <style>
    :root {{
      --paper: #f7f3ea;
      --ink: #171717;
      --muted: #68645e;
      --line: #d8d0c2;
      --panel: #fffaf0;
      --panel-2: #ebe4d6;
      --red: #c74231;
      --blue: #246b86;
      --green: #47725b;
      --amber: #b16d22;
      --black: #111;
      --soft-red: #fff2ec;
      --soft-green: #edf4ee;
      --soft-blue: #edf5f8;
      --soft-amber: #fff5df;
      --shadow: 0 18px 45px rgba(28, 24, 18, .12);
      --note-base: #ece4d5;
      --note-hi: #fffaf0;
      --note-lo: #c4b8a4;
      --note-shadow-dark: rgba(94, 73, 44, .18);
      --note-shadow-light: rgba(255, 250, 240, .76);
      --note-grad-a: #d4c7b2;
      --note-grad-b: #fff3dd;
      --note-glass-a: rgba(212, 199, 178, .26);
      --note-glass-b: rgba(255, 243, 221, .42);
      --note-glass-base: rgba(236, 228, 213, .34);
      --note-ring: rgba(36, 107, 134, .22);
      --note-tether: rgba(36, 107, 134, .46);
      --note-ink: #172125;
    }}
    body.theme-night {{
      --paper: #191a18;
      --ink: #eee8dc;
      --muted: #b5ad9f;
      --line: #393831;
      --panel: #22231f;
      --panel-2: #2c2c26;
      --red: #e06d5e;
      --blue: #7fb7c8;
      --green: #8eb89b;
      --amber: #d7a25e;
      --black: #f2eadc;
      --soft-red: #35221f;
      --soft-green: #223127;
      --soft-blue: #202f35;
      --soft-amber: #382d1e;
      --shadow: 0 0 0 rgba(0,0,0,0);
      --note-base: #272720;
      --note-hi: #39382f;
      --note-lo: #15160f;
      --note-shadow-dark: rgba(0, 0, 0, .46);
      --note-shadow-light: rgba(112, 94, 61, .18);
      --note-grad-a: #171810;
      --note-grad-b: #393427;
      --note-glass-a: rgba(23, 24, 16, .34);
      --note-glass-b: rgba(57, 52, 39, .48);
      --note-glass-base: rgba(39, 39, 32, .40);
      --note-ring: rgba(127, 183, 200, .24);
      --note-tether: rgba(127, 183, 200, .48);
      --note-ink: #f2eadc;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background:
        linear-gradient(90deg, rgba(23, 23, 23, .035) 1px, transparent 1px),
        linear-gradient(0deg, rgba(23, 23, 23, .035) 1px, transparent 1px),
        var(--paper);
      background-size: 28px 28px;
      color: var(--ink);
      font-family: "Aptos", "Microsoft YaHei", "Segoe UI", sans-serif;
      line-height: 1.62;
    }}
    body.theme-night {{
      background:
        linear-gradient(90deg, rgba(238, 232, 220, .045) 1px, transparent 1px),
        linear-gradient(0deg, rgba(238, 232, 220, .035) 1px, transparent 1px),
        var(--paper);
    }}
    a {{ color: var(--blue); text-decoration: none; border-bottom: 1px solid rgba(36, 90, 154, .35); }}
    a:hover {{ border-bottom-color: var(--blue); }}
    code {{
      display: inline-block;
      max-width: 100%;
      padding: 2px 7px;
      border: 1px solid rgba(17, 17, 17, .18);
      border-radius: 6px;
      background: #fffef8;
      color: var(--ink);
      font: 12px/1.45 "Cascadia Mono", Consolas, monospace;
      word-break: break-word;
    }}
    body.theme-night code {{ background: #161713; color: var(--ink); border-color: #3e3c35; }}
    body > header {{
      min-height: 88vh;
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(320px, 520px);
      gap: 44px;
      align-items: center;
      padding: 56px min(6vw, 76px) 42px;
      border-bottom: 2px solid var(--ink);
      background:
        repeating-linear-gradient(120deg, rgba(36, 107, 134, .055) 0 2px, transparent 2px 22px),
        linear-gradient(135deg, rgba(255,255,255,.72), rgba(235,228,214,.8));
    }}
    body.theme-night > header {{
      background:
        repeating-linear-gradient(120deg, rgba(127, 183, 200, .06) 0 2px, transparent 2px 22px),
        linear-gradient(135deg, rgba(34,35,31,.92), rgba(25,26,24,.96));
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 6px 10px;
      border: 1px solid var(--ink);
      background: var(--panel);
      font-size: 13px;
      text-transform: uppercase;
    }}
    .pin {{
      width: 9px;
      height: 9px;
      background: var(--red);
      border-radius: 50%;
      box-shadow: 0 0 0 4px rgba(199, 66, 49, .16);
    }}
    h1, h2, h3 {{ margin: 0; line-height: 1.12; letter-spacing: 0; }}
    h1 {{
      margin-top: 22px;
      max-width: 860px;
      font-family: Georgia, "Microsoft YaHei", serif;
      font-size: clamp(44px, 6vw, 92px);
      font-weight: 700;
    }}
    .lead {{
      max-width: 760px;
      margin: 22px 0 0;
      font-size: clamp(18px, 2vw, 24px);
      color: #35312b;
    }}
    body.theme-night .lead {{ color: #d8cfbf; }}
    .hero-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 30px;
    }}
    button, .button {{
      appearance: none;
      min-height: 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 10px 16px;
      border: 2px solid var(--ink);
      background: var(--ink);
      color: #fff;
      text-decoration: none;
      font-weight: 700;
      border-radius: 6px;
      box-shadow: 5px 5px 0 var(--red);
      cursor: pointer;
      transition: transform .18s ease, box-shadow .18s ease;
    }}
    button:hover, .button:hover {{
      transform: translate(-1px, -1px);
    }}
    button.secondary, .button.secondary {{
      background: var(--panel);
      color: var(--ink);
      box-shadow: 5px 5px 0 var(--blue);
    }}
    .report-nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 24px;
    }}
    .report-nav a {{
      display: inline-flex;
      min-height: 34px;
      align-items: center;
      padding: 6px 10px;
      border: 1px solid var(--ink);
      border-radius: 999px;
      background: rgba(255, 250, 240, .82);
      color: var(--ink);
      font-size: 13px;
      font-weight: 700;
    }}
    .report-nav a:hover {{ background: #fffef8; border-bottom-color: var(--ink); }}
    .map-board {{
      position: relative;
      min-height: 520px;
      border: 2px solid var(--ink);
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 24px;
      overflow: hidden;
    }}
    .map-board::before {{
      content: "";
      position: absolute;
      inset: 18px;
      border: 1px dashed rgba(17,17,17,.2);
      pointer-events: none;
    }}
    .source-card {{
      display: grid;
      align-content: center;
      gap: 12px;
    }}
    .source-card p {{ margin: 0; }}
    main {{ padding: 54px min(6vw, 76px) 80px; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 14px;
      max-width: 1180px;
      margin: 0 auto 58px;
    }}
    .metric {{
      min-height: 106px;
      padding: 18px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 4px 4px 0 rgba(17,17,17,.1);
    }}
    .metric span, .metric small {{ display: block; color: var(--muted); }}
    .metric strong {{ display: block; margin: 7px 0 4px; font-size: 32px; line-height: 1; }}
    section {{
      max-width: 1180px;
      margin: 0 auto 58px;
      padding: 24px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 4px 4px 0 rgba(17,17,17,.1);
      transition: opacity .45s ease, transform .45s ease, box-shadow .25s ease;
    }}
    section.visible {{
      opacity: 1;
      transform: translateY(0);
    }}
    section.focus-pulse {{
      animation: pulseFocus 1.15s ease;
    }}
    @keyframes pulseFocus {{
      0% {{ box-shadow: 0 0 0 0 rgba(189, 45, 53, .28); }}
      70% {{ box-shadow: 0 0 0 16px rgba(189, 45, 53, 0); }}
      100% {{ box-shadow: 4px 4px 0 rgba(17,17,17,.1); }}
    }}
    section > header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 16px;
      margin-bottom: 18px;
      border-bottom: 2px solid var(--ink);
    }}
    section h2 {{
      font-family: Georgia, "Microsoft YaHei", serif;
      font-size: clamp(30px, 4vw, 54px);
    }}
    .tag {{
      align-self: start;
      padding: 4px 10px;
      border: 1px solid currentColor;
      border-radius: 999px;
      color: var(--muted);
      background: var(--panel);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .parse-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .parse-cell {{
      min-height: 128px;
      padding: 18px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #fffef8;
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .parse-cell strong, .parse-cell span {{ display: block; }}
    .parse-cell span {{ margin: 8px 0; font-weight: 800; color: var(--green); }}
    .parse-cell.todo span {{ color: var(--amber); }}
    .parse-cell p {{ margin: 0; color: var(--muted); }}
    .two-col {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 16px;
    }}
    .reading-ladder {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto minmax(0, 1fr);
      gap: 12px;
      align-items: stretch;
      margin: 18px 0;
    }}
    .ladder-step {{
      padding: 16px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #fffef8;
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .ladder-step b {{
      display: inline-grid;
      place-items: center;
      width: 38px;
      height: 38px;
      margin-bottom: 10px;
      border: 2px solid var(--ink);
      border-radius: 50%;
      background: var(--blue);
      color: #fff;
    }}
    .ladder-step strong {{
      display: block;
      margin-bottom: 6px;
      font-size: 16px;
    }}
    .ladder-step span {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .ladder-arrow {{
      display: grid;
      place-items: center;
      color: var(--amber);
      font-size: 28px;
      font-weight: 900;
    }}
    .fact-table th {{
      width: 180px;
      color: var(--amber);
      background: #fffef8;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      border: 2px solid var(--ink);
      border-radius: 8px;
      overflow: hidden;
      background: var(--panel);
      font-size: 14px;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: var(--ink); color: #fff; }}
    tr:last-child td, tr:last-child th {{ border-bottom: 0; }}
    .diagram {{
      margin: 0;
      padding: 22px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #181713;
      color: #f5ead4;
      overflow-x: auto;
      font: 13px/1.55 "Cascadia Mono", Consolas, monospace;
    }}
    .function-group {{
      margin-top: 16px;
      padding: 16px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #fffef8;
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .function-group summary {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      cursor: pointer;
      color: var(--blue);
      font-weight: 800;
      font-size: 17px;
    }}
    .function-group summary::after {{
      content: "展开";
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--panel);
      color: var(--blue);
      font-size: 12px;
    }}
    .function-group[open] summary::after {{ content: "收起"; }}
    .function-group small {{ display: block; margin-top: 3px; color: var(--muted); font-weight: 400; }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      margin: 16px 0;
      padding: 12px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: var(--panel-2);
    }}
    .toolbar input {{
      min-width: min(420px, 100%);
      flex: 1;
      border: 2px solid var(--ink);
      border-radius: 6px;
      padding: 9px 11px;
      font: inherit;
      background: #fffef8;
    }}
    .toolbar .count {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .func-card {{
      padding: 13px 14px;
      margin: 10px 0;
      border: 2px solid var(--ink);
      border-left: 8px solid var(--blue);
      border-radius: 8px;
      background: #fffef8;
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
      transition: transform .18s ease, box-shadow .18s ease, background .18s ease;
    }}
    .func-card:hover {{
      transform: translate(-2px, -2px);
      box-shadow: 7px 7px 0 rgba(17,17,17,.14);
      background: #fff;
    }}
    .func-card.is-hidden {{ display: none; }}
    .function-group.no-match {{ display: none; }}
    .func-card .sig {{
      color: var(--blue);
      font: 13px/1.45 "Cascadia Mono", Consolas, monospace;
      word-break: break-word;
    }}
    .func-card p {{ margin: 6px 0; color: var(--ink); }}
    .func-card .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
    }}
    .func-card .meta span {{
      padding: 2px 7px;
      border-radius: 999px;
      border: 1px solid currentColor;
      border-radius: 999px;
      background: var(--panel);
      color: var(--blue);
      font-weight: 800;
    }}
    .func-facts {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }}
    .func-facts span {{
      min-height: 48px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 250, 240, .82);
      color: var(--ink);
      font-size: 12px;
      line-height: 1.35;
    }}
    .func-facts b {{
      display: block;
      margin-bottom: 3px;
      color: var(--amber);
      font-size: 11px;
    }}
    .inventory-wrap {{
      max-height: 640px;
      overflow: auto;
      scrollbar-gutter: stable both-edges;
      overscroll-behavior: contain;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background:
        linear-gradient(180deg, rgba(255,250,240,.96), rgba(247,243,234,.94)),
        var(--panel);
    }}
    .inventory-wrap::-webkit-scrollbar {{
      width: 16px;
      height: 16px;
    }}
    .inventory-wrap::-webkit-scrollbar-track {{
      border-left: 2px solid var(--ink);
      background:
        linear-gradient(145deg, rgba(196,184,164,.32), rgba(255,250,240,.74)),
        var(--panel-2);
    }}
    .inventory-wrap::-webkit-scrollbar-thumb {{
      border: 4px solid var(--panel-2);
      border-radius: 12px;
      background:
        linear-gradient(145deg, var(--blue), #174b60);
    }}
    .inventory-wrap::-webkit-scrollbar-corner {{
      background: var(--panel-2);
    }}
    .inventory-table {{
      border: 0;
      border-radius: 0;
      box-shadow: none;
    }}
    .inventory-table thead th {{
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    .inventory-table small {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font: 12px/1.4 "Cascadia Mono", Consolas, monospace;
      word-break: break-word;
    }}
    .inventory-table code {{
      color: var(--blue);
      font-weight: 800;
    }}
    .symbol-index {{
      width: 52px;
      color: var(--amber);
      font-weight: 900;
    }}
    .symbol-row.is-hidden {{ display: none; }}
    .timeline {{
      display: grid;
      gap: 12px;
      position: relative;
      padding-left: 14px;
    }}
    .timeline::before {{
      content: "";
      position: absolute;
      left: 4px;
      top: 8px;
      bottom: 8px;
      width: 2px;
      background: var(--ink);
    }}
    .timeline-item {{
      position: relative;
      padding: 14px 16px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #fffef8;
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .timeline-item::before {{
      content: "";
      position: absolute;
      left: -15px;
      top: 20px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--red);
    }}
    .timeline-item h3 {{ font-size: 16px; }}
    .timeline-item p {{ margin: 6px 0; }}
    .timeline-item small {{ color: var(--muted); }}
    .issue {{
      margin: 12px 0;
      padding: 14px 16px;
      border: 2px solid var(--ink);
      border-left: 8px solid var(--red);
      border-radius: 8px;
      background: var(--soft-red);
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .issue span {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: #fff;
      color: var(--red);
      font-weight: 800;
      font-size: 12px;
    }}
    .issue h3 {{ margin-top: 6px; font-size: 17px; }}
    .issue p {{ margin: 7px 0; }}
    .issue small {{ color: var(--green); font-weight: 700; }}
    .note {{
      padding: 14px 16px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: var(--soft-green);
    }}
    .route-panel {{
      padding: 18px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .route-panel.expert {{ background: #fffef8; }}
    .route-panel h3 {{ color: var(--green); margin-bottom: 10px; }}
    .route-panel.expert h3 {{ color: var(--blue); }}
    .route-panel ol {{ margin: 0; padding-left: 22px; }}
    .route-panel li {{ margin: 10px 0; }}
    .route-panel span {{ display: block; color: var(--muted); }}
    .coupling {{
      display: inline-block;
      min-width: 34px;
      text-align: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-weight: 800;
      font-size: 12px;
    }}
    .coupling.cool {{ background: var(--soft-green); color: var(--green); }}
    .coupling.warm {{ background: var(--soft-amber); color: var(--amber); }}
    .coupling.hot {{ background: var(--soft-red); color: var(--red); }}
    .level {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--panel);
      color: var(--blue);
      font-weight: 800;
      font-size: 12px;
    }}
    .next-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .question {{
      min-height: 128px;
      padding: 16px;
      border-radius: 8px;
      border: 2px solid var(--ink);
      background: #181713;
      color: #fff;
      box-shadow: 4px 4px 0 rgba(17,17,17,.14);
    }}
    .question strong {{ color: #f5ead4; }}
    .todo-item {{
      display: grid;
      grid-template-columns: 32px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      cursor: pointer;
      transition: opacity .18s ease, transform .18s ease, border-color .18s ease;
    }}
    .todo-check {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .todo-box {{
      position: relative;
      display: block;
      width: 30px;
      height: 30px;
      border: 2px solid #f5ead4;
      border-radius: 7px;
      background:
        linear-gradient(145deg, rgba(255,250,240,.18), rgba(17,17,17,.2)),
        #25221a;
      box-shadow:
        inset 4px 4px 8px rgba(0,0,0,.38),
        inset -4px -4px 8px rgba(255,250,240,.13);
    }}
    .todo-check:checked + .todo-box {{
      border-color: var(--green);
      background:
        linear-gradient(145deg, rgba(237,244,238,.78), rgba(95,139,90,.7)),
        var(--green);
      box-shadow:
        4px 4px 10px rgba(0,0,0,.28),
        -3px -3px 8px rgba(255,250,240,.14);
    }}
    .todo-check:checked + .todo-box::after {{
      content: "";
      position: absolute;
      left: 7px;
      top: 7px;
      width: 13px;
      height: 8px;
      border-left: 3px solid #181713;
      border-bottom: 3px solid #181713;
      transform: rotate(-45deg);
    }}
    .todo-item.is-done {{
      opacity: .72;
      border-color: var(--green);
    }}
    .todo-item.is-done .todo-text {{
      text-decoration: line-through;
      text-decoration-thickness: 2px;
      text-decoration-color: rgba(237,244,238,.7);
    }}
    .reader-notes {{
      background:
        linear-gradient(135deg, rgba(237, 244, 238, .72), rgba(255, 250, 240, .92)),
        var(--panel);
    }}
    .reader-notes-grid {{
      display: grid;
      grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr);
      gap: 16px;
      align-items: start;
    }}
    .note-tools, .notes-list, .note-export {{
      padding: 16px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      background: #fffef8;
      box-shadow: 4px 4px 0 rgba(17,17,17,.08);
    }}
    .note-tools {{
      display: grid;
      gap: 10px;
    }}
    .note-tools p, .note-card p {{
      margin: 0;
    }}
    .note-tools select, .note-tools textarea, .inline-note-pad input {{
      width: 100%;
      border: 2px solid var(--ink);
      border-radius: 6px;
      padding: 10px 11px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
    }}
    select {{
      appearance: none;
      min-height: 46px;
      padding-right: 42px !important;
      background:
        linear-gradient(45deg, transparent 50%, var(--ink) 50%) calc(100% - 22px) 20px / 7px 7px no-repeat,
        linear-gradient(135deg, var(--ink) 50%, transparent 50%) calc(100% - 15px) 20px / 7px 7px no-repeat,
        var(--panel) !important;
      cursor: pointer;
    }}
    select:focus, textarea:focus, input:focus {{
      outline: 0;
      border-color: var(--blue);
    }}
    * {{
      scrollbar-width: thin;
      scrollbar-color: var(--blue) rgba(235, 228, 214, .82);
    }}
    *::-webkit-scrollbar {{
      width: 14px;
      height: 14px;
    }}
    *::-webkit-scrollbar-track {{
      border: 2px solid var(--ink);
      border-radius: 999px;
      background: var(--panel-2);
    }}
    *::-webkit-scrollbar-thumb {{
      border: 3px solid var(--panel-2);
      border-radius: 999px;
      background: var(--blue);
    }}
    *::-webkit-scrollbar-thumb:hover {{
      background: var(--red);
    }}
    .notes-list {{
      display: grid;
      gap: 10px;
      min-height: 150px;
    }}
    .note-card {{
      padding: 12px;
      border: 2px solid var(--ink);
      border-left: 8px solid var(--green);
      border-radius: 8px;
      background: var(--panel);
    }}
    .note-card blockquote {{
      margin: 0 0 8px;
      padding-left: 10px;
      border-left: 3px solid var(--blue);
      color: var(--muted);
    }}
    .note-card footer {{
      display: none;
    }}
    .note-card button {{
      display: none;
    }}
    .note-export {{
      grid-column: 1 / -1;
    }}
    .note-export textarea {{
      width: 100%;
      min-height: 130px;
      border: 2px solid var(--ink);
      border-radius: 6px;
      padding: 12px;
      background: #181713;
      color: #f5ead4;
      font: 13px/1.55 "Cascadia Mono", Consolas, monospace;
    }}
    .inline-note-pad {{
      position: absolute;
      z-index: 30;
      width: min(300px, calc(100vw - 32px));
      padding: 0;
      border: 0;
      border-radius: 16px;
      background: transparent;
      touch-action: none;
    }}
    .inline-note-pad input {{
      display: block;
      min-height: 56px;
      border: 0;
      border-radius: 16px;
      padding: 0 18px;
      background-color: var(--note-glass-base);
      background-image: linear-gradient(145deg, var(--note-glass-b), var(--note-glass-a));
      box-shadow:
        inset 8px 8px 16px var(--note-shadow-dark),
        inset -8px -8px 16px var(--note-shadow-light),
        inset 0 0 0 1px var(--note-ring);
      color: var(--note-ink);
      -webkit-backdrop-filter: blur(4px) saturate(1.12);
      backdrop-filter: blur(4px) saturate(1.12);
      font: 600 16px/1.35 "Aptos", "Microsoft YaHei", "Segoe UI", sans-serif;
      outline: 0;
    }}
    .inline-note-pad footer {{
      display: none;
    }}
    .inline-note-view {{
      position: absolute;
      z-index: 29;
      width: min(300px, calc(100vw - 32px));
      min-height: 48px;
      padding: 13px 16px;
      border: 0;
      border-radius: 16px;
      background-color: var(--note-glass-base);
      background-image: linear-gradient(145deg, var(--note-glass-a), var(--note-glass-b));
      box-shadow:
        8px 8px 16px var(--note-shadow-dark),
        -8px -8px 16px var(--note-shadow-light),
        0 0 0 1px var(--note-ring);
      color: var(--note-ink);
      -webkit-backdrop-filter: blur(4px) saturate(1.12);
      backdrop-filter: blur(4px) saturate(1.12);
      cursor: grab;
      font: 600 14px/1.45 "Aptos", "Microsoft YaHei", "Segoe UI", sans-serif;
      overflow: hidden;
      text-overflow: ellipsis;
      touch-action: none;
      transition: box-shadow .16s ease, transform .16s ease, background-color .16s ease;
    }}
    .inline-note-pad.is-dragging,
    .inline-note-view.is-dragging {{
      cursor: grabbing;
      z-index: 34;
      transform: translateY(-1px);
    }}
    .inline-note-pad.is-linked input {{
      box-shadow:
        inset 8px 8px 16px var(--note-shadow-dark),
        inset -8px -8px 16px var(--note-shadow-light),
        inset 0 0 0 1px var(--note-tether);
    }}
    .inline-note-view.is-linked {{
      box-shadow:
        9px 9px 18px var(--note-shadow-dark),
        -7px -7px 16px var(--note-shadow-light),
        0 0 0 1px var(--note-tether);
    }}
    .inline-note-view:empty {{
      display: none;
    }}
    #noteLayer {{
      position: fixed;
      inset: 0;
      z-index: 9999;
      pointer-events: none;
      overflow: visible;
    }}
    #noteLayer .inline-note-pad,
    #noteLayer .inline-note-view {{
      pointer-events: auto;
    }}
    #noteTethers {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      overflow: visible;
    }}
    .note-tether-line {{
      stroke: var(--note-tether);
      stroke-width: 1.35;
      stroke-linecap: round;
      stroke-dasharray: 5 7;
      opacity: .82;
    }}
    .note-anchor-dot {{
      position: absolute;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 35% 30%, rgba(255,250,240,.95), transparent 0 32%, transparent 54%),
        var(--note-tether);
      box-shadow:
        0 0 0 6px rgba(36, 107, 134, .08),
        0 0 18px rgba(36, 107, 134, .18);
      transform: translate(-50%, -50%);
      pointer-events: none;
    }}
    .note-anchor-highlight {{
      position: absolute;
      border-radius: 5px;
      background: rgba(36, 107, 134, .13);
      box-shadow:
        inset 0 0 0 1px rgba(36, 107, 134, .18),
        0 0 14px rgba(255, 250, 240, .34);
      -webkit-backdrop-filter: blur(2px) saturate(1.06);
      backdrop-filter: blur(2px) saturate(1.06);
      pointer-events: none;
    }}
    mark.reader-highlight {{
      background: linear-gradient(transparent 50%, rgba(177, 109, 34, .35) 50%);
      color: inherit;
      padding: 0 2px;
    }}
    .muted {{ color: var(--muted); }}
    footer {{
      max-width: 1180px;
      margin: 0 auto;
      padding-top: 28px;
      border-top: 2px solid var(--ink);
      color: var(--muted);
      font-size: 14px;
    }}
    @media (max-width: 1100px) {{
      body > header, .metrics, .parse-grid, .two-col, .next-grid, .reader-notes-grid, .reading-ladder {{ grid-template-columns: 1fr; }}
      .ladder-arrow {{ display: none; }}
      body > header {{ min-height: unset; }}
      .note-export {{ grid-column: auto; }}
    }}
    @media (max-width: 620px) {{
      body > header {{ padding: 34px 18px; }}
      main {{ padding: 36px 18px 56px; }}
      .map-board {{ min-height: unset; }}
      .hero-actions {{ flex-direction: column; align-items: stretch; }}
      .func-facts {{ grid-template-columns: 1fr; }}
    }}
    @media print {{
      body {{ background: #fff; }}
      main {{ padding: 0; }}
      section, .metric, .map-board {{ box-shadow: none; break-inside: avoid; }}
    }}
  </style>
</head>
<body>
  <div id="noteLayer" data-no-note><svg id="noteTethers" data-no-note aria-hidden="true"></svg></div>
  <header>
    <div>
      <div class="eyebrow"><span class="pin"></span> Source Architecture Navigator</div>
      <h1>{h(title)}</h1>
      <p class="lead">{h(subtitle or project_hint)}</p>
      <div class="hero-actions">
            <button type="button" data-action="focus-golden">高亮主路径</button>
            <button type="button" class="secondary" data-action="expand-functions">展开全部函数</button>
            <button type="button" class="secondary" data-action="collapse-functions">收起函数地图</button>
            <button type="button" class="secondary" data-action="toggle-evidence">显示/隐藏证据</button>
            <button type="button" class="secondary" data-action="toggle-theme">夜间</button>
      </div>
      <nav class="report-nav">
        <a href="#project">项目识别</a>
        <a href="#coverage">解析覆盖</a>
        <a href="#layers">L3 全局分层</a>
        <a href="#modules">L2 模块关系</a>
        <a href="#functions">L1 函数地图</a>
        <a href="#inventory">全量符号索引</a>
        <a href="#golden">Golden Path</a>
        <a href="#contracts">配置与契约</a>
        <a href="#risks">问题诊断</a>
        <a href="#route">阅读路线</a>
        <a href="#evidence">证据表</a>
        <a href="#reader-notes">阅读笔记</a>
        <a href="#next">下一问</a>
      </nav>
    </div>
    <div class="map-board source-card" aria-label="源码分析报告元数据">
          <p><b>报告类型</b><br>{h(REPORT_HEADING)}</p>
          <p><b>生成时间</b><br>{h(now)}</p>
          <p><b>输入</b><br>{code(source_label)}</p>
          <p><b>扫描根</b><br>{code(report.get('root', scan_root))}</p>
          <p><b>zip 成员</b><br>{h(zip_stats.get('members', 'n/a'))}</p>
    </div>
  </header>

  <main>

      <div class="metrics">
        {render_metric("扫描文件", report.get("file_count_scanned", 0), "受 --max-files 限制")}
        {render_metric("源码文件", len(source_files), "按语言扩展名")}
        {render_metric("符号样本", symbol_count, "函数/类/方法")}
        {render_metric("依赖边", edge_count, "静态导入样本")}
        {render_metric("配置线索", manifest_count, "manifest + config")}
      </div>

      <section id="reader-notes" class="reader-notes" data-no-note>
        <header>
          <h2>阅读笔记</h2>
        </header>
        <div class="reader-notes-grid">
          <div class="note-tools">
            <select id="noteExportMode" aria-label="笔记导出格式">
              <option value="ordered">序号 + 笔记</option>
              <option value="source">原文 + 笔记</option>
            </select>
            <button type="button" class="secondary" data-action="copy-notes">复制导出内容</button>
            <button type="button" class="secondary" data-action="download-notes">下载笔记</button>
          </div>
          <div id="notesList" class="notes-list" aria-live="polite"></div>
          <div class="note-export">
            <textarea id="notesExport" readonly aria-label="笔记导出预览"></textarea>
          </div>
        </div>
      </section>

      <section id="project">
        <header>
          <h2>项目识别卡</h2>
          <span class="tag">先把系统身份定下来</span>
        </header>
        {render_project_table(report, project_hint, primary, profiles)}
      </section>

      <section id="coverage">
        <header>
          <h2>一次全解析覆盖矩阵</h2>
          <span class="tag">不是只给 3-5 个文件</span>
        </header>
        <div class="parse-grid">{render_full_parse_matrix(report, profiles, risks)}</div>
      </section>

      <section id="layers">
        <header>
          <h2>L3 全局分层架构</h2>
          <span class="tag">从入口层到验证层</span>
        </header>
        <p class="muted">阅读顺序是 L3 → L2 → L1：先用 L3 确认系统层次，再用 L2 看模块之间的依赖边界，最后在 L1 或全量索引里挑具体函数。</p>
        {render_reading_ladder(report)}
        {render_layer_diagram(profiles, report)}
      </section>

      <section id="modules">
        <header>
          <h2>L2 模块关系图</h2>
          <span class="tag">静态依赖 + 模块边界</span>
        </header>
        {render_module_table(profiles)}
      </section>

      <section id="functions">
        <header>
          <h2>L1 逐函数地图</h2>
          <span class="tag">代表对象用于快速定位</span>
        </header>
        <p class="muted">这里保留每个核心模块的代表对象，避免把同类职责重复解释成大段文字；完整函数、类、方法清单在下一节全量索引中交叉引用。</p>
        <div class="toolbar">
          <input id="symbolSearch" type="search" placeholder="筛选函数、类、模块或路径" aria-label="筛选函数、类、模块或路径">
          <button type="button" class="secondary" data-action="clear-search">清空筛选</button>
          <span class="count" id="symbolCount">显示全部符号</span>
        </div>
        {render_function_groups(profiles, scan_root, linkable)}
      </section>

      <section id="inventory">
        <header>
          <h2>全量符号索引</h2>
          <span class="tag">{h(symbol_tag)}</span>
        </header>
        <p class="muted">这是本报告的函数/类/方法目录：每一行都回到模块层级和证据位置。大型项目可用 <code>--max-symbols</code> 调整展示预算，避免固定上限截断关键模块。</p>
        <div class="toolbar">
          <button type="button" class="secondary" data-action="copy-symbol-inventory">复制当前索引</button>
        </div>
        {render_symbol_inventory(report, scan_root, linkable)}
      </section>

      <section id="golden">
        <header>
          <h2>Golden Path 核心路径识别</h2>
          <span class="tag">先读主链，再读边缘</span>
        </header>
        <div class="timeline">{render_golden_path(route, report)}</div>
      </section>

      <section id="contracts">
        <header>
          <h2>配置依赖与接口契约检查</h2>
          <span class="tag">默认值、字段、输入输出</span>
        </header>
        <div class="two-col">
          <div>
            <h3>配置入口</h3>
            {render_config_table(report)}
          </div>
          <div>
            <h3>契约对象候选</h3>
            {render_contract_candidates(report, scan_root, linkable)}
          </div>
        </div>
      </section>

      <section id="risks">
        <header>
          <h2>问题诊断：坏味道与架构断点</h2>
          <span class="tag">只列有静态证据的候选</span>
        </header>
        {render_risks(risks)}
      </section>

      <section id="route">
        <header>
          <h2>阅读路线建议</h2>
          <span class="tag">新手视角 + 高手视角</span>
        </header>
        <div class="two-col">{render_dual_routes(route, report, scan_root, linkable)}</div>
      </section>

      <section id="evidence">
        <header>
          <h2>证据表</h2>
          <span class="tag">每个判断都要能回到路径</span>
        </header>
        {render_evidence(evidence_rows)}
      </section>

      <section id="next">
        <header>
          <h2>下一步提问模板</h2>
          <span class="tag">让阅读继续沿结构推进</span>
        </header>
        <div class="next-grid todo-list" id="todoList" data-no-note>
          <label class="question todo-item" data-todo-id="drill">
            <input class="todo-check" type="checkbox" aria-label="完成继续下钻">
            <span class="todo-box" aria-hidden="true"></span>
            <span class="todo-text"><strong>继续下钻</strong><br>请追踪 {h(focus_target)}，给我输入、输出、副作用、上游和下游。</span>
          </label>
          <label class="question todo-item" data-todo-id="compare">
            <input class="todo-check" type="checkbox" aria-label="完成横向比较">
            <span class="todo-box" aria-hidden="true"></span>
            <span class="todo-text"><strong>横向比较</strong><br>请比较两个模块是否在做重复的事，只给有证据的结论和最小验证方式。</span>
          </label>
          <label class="question todo-item" data-todo-id="boundary">
            <input class="todo-check" type="checkbox" aria-label="完成施工前边界">
            <span class="todo-box" aria-hidden="true"></span>
            <span class="todo-text"><strong>施工前</strong><br>基于这份全解析报告，把目标转成施工边界卡，先不要改代码。</span>
          </label>
        </div>
      </section>

      <footer>
        本报告来自静态源码扫描和启发式归类。它的目标是一次性建立完整阅读地图；静态结论用路径证据标注，运行时行为用测试、命令输出或日志补证。
      </footer>
    </main>
  <script>
    const sections = document.querySelectorAll('section');
    const reveal = new IntersectionObserver((entries) => {{
      for (const entry of entries) {{
        if (entry.isIntersecting) {{
          entry.target.classList.add('visible');
          reveal.unobserve(entry.target);
        }}
      }}
    }}, {{ threshold: 0.08 }});
    sections.forEach((section) => reveal.observe(section));

    const cards = Array.from(document.querySelectorAll('.func-card'));
    const inventoryRows = Array.from(document.querySelectorAll('.symbol-row'));
    const groups = Array.from(document.querySelectorAll('.function-group'));
    const search = document.getElementById('symbolSearch');
    const count = document.getElementById('symbolCount');

    function applyFilter() {{
      const q = (search?.value || '').trim().toLowerCase();
      let visible = 0;
      for (const card of cards) {{
        const hit = !q || (card.dataset.symbolText || '').includes(q);
        card.classList.toggle('is-hidden', !hit);
      }}
      for (const row of inventoryRows) {{
        const hit = !q || (row.dataset.symbolText || '').includes(q);
        row.classList.toggle('is-hidden', !hit);
        if (hit) visible++;
      }}
      for (const group of groups) {{
        const hasHit = Array.from(group.querySelectorAll('.func-card')).some(card => !card.classList.contains('is-hidden'));
        group.classList.toggle('no-match', !hasHit);
        if (q && hasHit) group.open = true;
      }}
      if (count) count.textContent = q ? `索引命中 ${{visible}} / ${{inventoryRows.length}} 个符号` : `全量 ${{inventoryRows.length}} 个符号`;
      window.requestAnimationFrame(placeAllLayerItems);
    }}
    search?.addEventListener('input', applyFilter);
    applyFilter();

    function pulse(id) {{
      const target = document.getElementById(id);
      if (!target) return;
      target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      target.classList.remove('focus-pulse');
      window.setTimeout(() => target.classList.add('focus-pulse'), 240);
    }}

    const storeFallback = new Map();
    function storeGet(key, fallback = '') {{
      try {{
        const value = window.localStorage.getItem(key);
        return value === null ? fallback : value;
      }} catch (error) {{
        return storeFallback.has(key) ? storeFallback.get(key) : fallback;
      }}
    }}

    function storeSet(key, value) {{
      try {{
        window.localStorage.setItem(key, value);
      }} catch (error) {{
        storeFallback.set(key, value);
      }}
    }}

    const themeKey = 'source-nav-theme:' + location.pathname + ':' + document.title;
    function applyTheme(theme) {{
      document.body.classList.toggle('theme-night', theme === 'night');
      document.querySelectorAll('[data-action="toggle-theme"]').forEach(button => {{
        button.textContent = theme === 'night' ? '日间' : '夜间';
      }});
    }}
    applyTheme(storeGet(themeKey, 'day'));

    const todoKey = 'source-nav-todos:' + location.pathname + ':' + document.title;
    const todoChecks = Array.from(document.querySelectorAll('.todo-check'));
    function loadTodos() {{
      let saved = {{}};
      try {{
        saved = JSON.parse(storeGet(todoKey, '{{}}') || '{{}}');
      }} catch (error) {{
        saved = {{}};
      }}
      for (const input of todoChecks) {{
        const item = input.closest('.todo-item');
        const id = item?.dataset.todoId || '';
        input.checked = Boolean(saved[id]);
        item?.classList.toggle('is-done', input.checked);
      }}
    }}
    function saveTodos() {{
      const saved = {{}};
      for (const input of todoChecks) {{
        const item = input.closest('.todo-item');
        const id = item?.dataset.todoId || '';
        if (id) saved[id] = input.checked;
        item?.classList.toggle('is-done', input.checked);
      }}
      storeSet(todoKey, JSON.stringify(saved));
    }}
    todoChecks.forEach(input => input.addEventListener('change', saveTodos));
    loadTodos();

    const notesKey = 'source-nav-notes:' + location.pathname + ':' + document.title;
    const notesList = document.getElementById('notesList');
    const noteExportMode = document.getElementById('noteExportMode');
    const notesExport = document.getElementById('notesExport');
    const noteLayer = document.getElementById('noteLayer');
    const noteTethers = document.getElementById('noteTethers');
    let notes = [];
    let activeNoteHost = null;
    let activeAnchorNoteId = '';
    let dragState = null;
    let saveTimer = 0;
    let suppressDocumentClickUntil = 0;
    let suppressNextDocumentClick = false;
    let suppressDocumentClickTimer = 0;

    function loadNotes() {{
      try {{
        notes = JSON.parse(storeGet(notesKey, '[]') || '[]');
        if (!Array.isArray(notes)) notes = [];
      }} catch (error) {{
        notes = [];
      }}
    }}

    function saveNotes() {{
      storeSet(notesKey, JSON.stringify(notes));
    }}

    function scheduleSave() {{
      window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(() => {{
        saveNotes();
        renderNotes();
      }}, 180);
    }}

    function suppressDocumentClick(ms = 320) {{
      window.clearTimeout(suppressDocumentClickTimer);
      suppressNextDocumentClick = true;
      suppressDocumentClickUntil = Date.now() + ms;
      suppressDocumentClickTimer = window.setTimeout(() => {{
        suppressNextDocumentClick = false;
      }}, ms);
    }}

    function elementFromNode(node) {{
      if (!node) return document.body;
      return node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    }}

    function eventClosest(event, selector) {{
      const element = elementFromNode(event.target);
      return element && element.closest ? element.closest(selector) : null;
    }}

    function sectionLabel(node) {{
      const element = elementFromNode(node);
      const section = element?.closest('section');
      if (!section) return '首屏概览';
      const title = section.querySelector('h2')?.textContent?.trim() || section.id || '未命名区块';
      const symbol = element.closest('.func-card')?.querySelector('.sig')?.textContent?.trim();
      const rowPath = element.closest('tr')?.querySelector('code')?.textContent?.trim();
      const detail = symbol || rowPath || '';
      return detail ? title + ' / ' + detail : title;
    }}

    function orderForNode(node) {{
      const element = elementFromNode(node);
      const anchors = Array.from(document.querySelectorAll('body > header, section, .func-card, tr, .timeline-item, .issue, .route-panel, .question'));
      const index = anchors.findIndex(anchor => anchor === element || anchor.contains(element));
      return index >= 0 ? index : Date.now();
    }}

    const noteHostSelector = '.func-card, .issue, .route-panel, .question, .timeline-item, .parse-cell, td, li, article, section, body > header';
    const allNoteHosts = [document.body, ...Array.from(document.querySelectorAll(noteHostSelector))
      .filter(host => !host.closest('[data-no-note]'))];
    const hostIds = new WeakMap();
    const hostsById = new Map();

    function stableHostId(host) {{
      if (!host || host === document.body) return 'host-body';
      const parts = [];
      let node = host;
      while (node && node !== document.body && node.nodeType === Node.ELEMENT_NODE) {{
        const parent = node.parentElement;
        if (!parent) break;
        const siblings = Array.from(parent.children).filter(child => child.tagName === node.tagName);
        const marker = Array.from(node.classList || [])
          .filter(name => ['func-card', 'issue', 'route-panel', 'question', 'timeline-item', 'parse-cell', 'metric', 'map-board'].includes(name))
          .slice(0, 2)
          .join('.');
        parts.push(`${{node.tagName.toLowerCase()}}${{marker ? '.' + marker : ''}}:${{Math.max(0, siblings.indexOf(node))}}`);
        node = parent;
      }}
      const raw = parts.reverse().join('>');
      let hash = 2166136261;
      for (let index = 0; index < raw.length; index += 1) {{
        hash ^= raw.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
      }}
      return 'host-' + (hash >>> 0).toString(36);
    }}

    allNoteHosts.forEach(host => {{
      const id = stableHostId(host);
      hostIds.set(host, id);
      hostsById.set(id, host);
    }});

    function noteHost(node) {{
      const element = elementFromNode(node);
      let host = element?.closest(noteHostSelector) || element?.closest('section, article, main, body') || document.body;
      if (host === document.documentElement) host = document.body;
      ensureHostId(host);
      return host;
    }}

    function ensureHostId(host) {{
      if (!host) return '';
      let id = hostIds.get(host);
      if (!id) {{
        id = stableHostId(host);
        hostIds.set(host, id);
        hostsById.set(id, host);
      }}
      return id;
    }}

    function hostById(hostId) {{
      return hostId ? hostsById.get(hostId) || null : null;
    }}

    function cleanText(value, limit = 900) {{
      return (value || '').replace(/\\s+/g, ' ').trim().slice(0, limit);
    }}

    function cssEscape(value) {{
      if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
      return String(value).replace(/[^a-zA-Z0-9_-]/g, match => '\\\\' + match);
    }}

    function noteForHost(host, quote = '') {{
      const hostId = ensureHostId(host);
      if (quote) return notes.find(note => note.hostId === hostId && note.quote === quote);
      return notes.find(note => note.hostId === hostId && !note.quote);
    }}

    function layerEditorForHost(host) {{
      if (!noteLayer || !host) return null;
      const hostId = ensureHostId(host);
      return noteLayer.querySelector(`.inline-note-pad[data-host-id="${{cssEscape(hostId)}}"]`);
    }}

    function clamp(value, min, max) {{
      return Math.min(max, Math.max(min, value));
    }}

    function notePointFromContext(host, context) {{
      const rect = host.getBoundingClientRect();
      let x = Number.isFinite(context.clientX) ? context.clientX : null;
      let y = Number.isFinite(context.clientY) ? context.clientY : null;
      if ((x === null || y === null) && context.range) {{
        const rangeRect = context.range.getBoundingClientRect();
        if (rangeRect.width || rangeRect.height) {{
          x = rangeRect.left + Math.min(24, Math.max(0, rangeRect.width / 2));
          y = rangeRect.top + rangeRect.height / 2;
        }}
      }}
      if (x === null || y === null || !rect.width || !rect.height) return null;
      return {{
        relX: clamp((x - rect.left) / rect.width, 0, 1),
        relY: clamp((y - rect.top) / rect.height, 0, 1)
      }};
    }}

    function noteLayerRect(host, note = null) {{
      const rect = host.getBoundingClientRect();
      const maxWidth = Math.max(180, Math.min(300, window.innerWidth - 32));
      const width = Math.min(maxWidth, Math.max(180, rect.width - 28));
      const hasPoint = note && Number.isFinite(note.relX) && Number.isFinite(note.relY);
      const anchorX = rect.left + (hasPoint ? rect.width * note.relX : 14);
      const anchorY = rect.top + (hasPoint ? rect.height * note.relY : 12);
      const offsetX = Number(note?.dx) || 0;
      const offsetY = Number(note?.dy) || 0;
      const left = clamp(anchorX - 18 + offsetX, 16, window.innerWidth - width - 16);
      const top = clamp(anchorY - 28 + offsetY, 12, window.innerHeight - 72);
      return {{ left, top, width }};
    }}

    function layerNoteForElement(element) {{
      return notes.find(note => note.id === element.dataset.noteId) || null;
    }}

    function placeLayerItem(element, host, note = null) {{
      if (!element || !host) return;
      const hostRect = host.getBoundingClientRect();
      const isVisible = hostRect.bottom >= 0 && hostRect.top <= window.innerHeight && hostRect.right >= 0 && hostRect.left <= window.innerWidth;
      element.hidden = !isVisible;
      if (!isVisible) return;
      const rect = noteLayerRect(host, note || layerNoteForElement(element));
      element.style.left = rect.left + 'px';
      element.style.top = rect.top + 'px';
      element.style.width = rect.width + 'px';
    }}

    function placeAllLayerItems() {{
      if (!noteLayer) return;
      noteLayer.querySelectorAll('.inline-note-pad, .inline-note-view').forEach(item => {{
        const host = hostById(item.dataset.hostId || '');
        if (host) placeLayerItem(item, host);
      }});
      if (activeAnchorNoteId) showAnchorEffects(activeAnchorNoteId);
    }}

    function noteById(noteId) {{
      return notes.find(note => note.id === noteId) || null;
    }}

    function anchorPointForNote(host, note) {{
      const rect = host.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      const relX = Number.isFinite(note?.relX) ? note.relX : 0;
      const relY = Number.isFinite(note?.relY) ? note.relY : 0;
      const x = rect.left + rect.width * relX;
      const y = rect.top + rect.height * relY;
      if (x < -24 || x > window.innerWidth + 24 || y < -24 || y > window.innerHeight + 24) return null;
      return {{ x, y }};
    }}

    function clearAnchorVisuals() {{
      noteLayer?.querySelectorAll('.note-anchor-dot, .note-anchor-highlight').forEach(item => item.remove());
      noteLayer?.querySelectorAll('.inline-note-pad, .inline-note-view').forEach(item => item.classList.remove('is-linked'));
      if (noteTethers) noteTethers.replaceChildren();
    }}

    function hideAnchorEffects(noteId = '') {{
      if (noteId && activeAnchorNoteId && activeAnchorNoteId !== noteId) return;
      activeAnchorNoteId = '';
      clearAnchorVisuals();
    }}

    function quoteRectsForNote(note, host) {{
      const quote = note?.quote || '';
      if (!quote || !host) return [];
      const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT, {{
        acceptNode(node) {{
          const parent = node.parentElement;
          if (!parent || parent.closest('[data-no-note], script, style')) return NodeFilter.FILTER_REJECT;
          return node.nodeValue.includes(quote) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        }}
      }});
      const textNode = walker.nextNode();
      if (!textNode) return [];
      const start = textNode.nodeValue.indexOf(quote);
      if (start < 0) return [];
      const range = document.createRange();
      range.setStart(textNode, start);
      range.setEnd(textNode, start + quote.length);
      return Array.from(range.getClientRects()).filter(rect => rect.width > 4 && rect.height > 4).slice(0, 6);
    }}

    function showAnchorEffects(noteId, element = null) {{
      const note = noteById(noteId);
      const host = hostById(note?.hostId || '');
      const target = element || noteLayer?.querySelector(`[data-note-id="${{cssEscape(noteId)}}"]`);
      if (!note || !host || !target || target.hidden) {{
        hideAnchorEffects(noteId);
        return;
      }}
      activeAnchorNoteId = noteId;
      clearAnchorVisuals();
      target.classList.add('is-linked');
      const anchor = anchorPointForNote(host, note);
      if (!anchor) return;

      for (const rect of quoteRectsForNote(note, host)) {{
        const highlight = document.createElement('div');
        highlight.className = 'note-anchor-highlight';
        highlight.dataset.noNote = 'true';
        highlight.style.left = Math.max(0, rect.left - 2) + 'px';
        highlight.style.top = Math.max(0, rect.top - 1) + 'px';
        highlight.style.width = rect.width + 4 + 'px';
        highlight.style.height = rect.height + 2 + 'px';
        noteLayer.append(highlight);
      }}

      const dot = document.createElement('div');
      dot.className = 'note-anchor-dot';
      dot.dataset.noNote = 'true';
      dot.style.left = anchor.x + 'px';
      dot.style.top = anchor.y + 'px';
      noteLayer.append(dot);

      if (noteTethers) {{
        const rect = target.getBoundingClientRect();
        const startX = clamp(anchor.x, rect.left + 8, rect.right - 8);
        const startY = clamp(anchor.y, rect.top + 8, rect.bottom - 8);
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.classList.add('note-tether-line');
        line.setAttribute('x1', String(startX));
        line.setAttribute('y1', String(startY));
        line.setAttribute('x2', String(anchor.x));
        line.setAttribute('y2', String(anchor.y));
        noteTethers.append(line);
      }}
    }}

    function removeInlineEditors() {{
      noteLayer?.querySelectorAll('.inline-note-pad').forEach(editor => editor.remove());
      activeNoteHost = null;
      hideAnchorEffects();
    }}

    function highlightRange(range, id) {{
      return;
    }}

    function highlightFirstText(quote, id) {{
      return;
    }}

    function sortedNotes() {{
      return [...notes].sort((a, b) => (a.order - b.order) || String(a.createdAt).localeCompare(String(b.createdAt)));
    }}

    function buildExport() {{
      const mode = noteExportMode?.value || 'ordered';
      const sorted = sortedNotes().filter(note => (note.text || '').trim());
      if (!sorted.length) return '';
      return sorted.map((note, index) => {{
        const body = (note.text || '').trim();
        if (mode === 'source') {{
          const source = note.quote || note.label || '页面锚点';
          return `原文：${{source}}\\n笔记：${{body}}`;
        }}
        return `${{index + 1}}. ${{body}}`;
      }}).join('\\n\\n');
    }}

    function writeClipboard(text, fallbackElement = null) {{
      if (!text) return;
      if (navigator.clipboard?.writeText) {{
        navigator.clipboard.writeText(text);
        return;
      }}
      const target = fallbackElement || document.createElement('textarea');
      if (!fallbackElement) {{
        target.value = text;
        target.style.position = 'fixed';
        target.style.left = '-9999px';
        document.body.append(target);
      }}
      target.focus();
      target.select();
      document.execCommand('copy');
      if (!fallbackElement) target.remove();
    }}

    function buildSymbolInventoryText() {{
      const rows = inventoryRows.filter(row => !row.classList.contains('is-hidden'));
      return rows.map((row, index) => {{
        const name = row.dataset.symbolName || '';
        const kind = row.dataset.symbolKind || '';
        const module = row.dataset.symbolModule || '';
        const location = row.dataset.symbolLocation || '';
        return `${{index + 1}}. ${{name}} | ${{kind}} | ${{module}} | ${{location}}`;
      }}).join('\\n');
    }}

    function copySymbolInventory() {{
      writeClipboard(buildSymbolInventoryText());
    }}

    function attachNoteInteraction(element, note, host, openOnTap = false) {{
      element.addEventListener('mouseenter', () => showAnchorEffects(note.id, element));
      element.addEventListener('mouseleave', () => {{
        if (!dragState && openOnTap) hideAnchorEffects(note.id);
      }});
      element.addEventListener('click', (event) => {{
        if (element.dataset.wasDragged === 'true') {{
          event.preventDefault();
          event.stopPropagation();
          element.dataset.wasDragged = 'false';
        }}
      }});
      element.addEventListener('dblclick', (event) => {{
        if (!openOnTap) return;
        event.preventDefault();
        event.stopPropagation();
        openInlineNote({{ node: host, quote: note.quote || '', range: null }});
      }});
      let lastPointerDragAt = 0;
      function beginDrag(event, pointerId = null) {{
        if (typeof event.button === 'number' && event.button !== 0) return;
        dragState = {{
          element,
          note,
          host,
          openOnTap,
          startX: event.clientX,
          startY: event.clientY,
          startDx: Number(note.dx) || 0,
          startDy: Number(note.dy) || 0,
          moved: false
        }};
        if (pointerId !== null) element.setPointerCapture?.(pointerId);
        showAnchorEffects(note.id, element);
      }}
      function moveDrag(event) {{
        if (!dragState || dragState.element !== element) return;
        const deltaX = event.clientX - dragState.startX;
        const deltaY = event.clientY - dragState.startY;
        if (!dragState.moved && Math.hypot(deltaX, deltaY) < 5) return;
        dragState.moved = true;
        event.preventDefault();
        note.dx = dragState.startDx + deltaX;
        note.dy = dragState.startDy + deltaY;
        note.updatedAt = new Date().toISOString();
        element.classList.add('is-dragging');
        placeLayerItem(element, host, note);
        showAnchorEffects(note.id, element);
      }}
      function endDrag(event, pointerId = null) {{
        if (!dragState || dragState.element !== element) return;
        const moved = dragState.moved;
        if (pointerId !== null) element.releasePointerCapture?.(pointerId);
        element.classList.remove('is-dragging');
        dragState = null;
        suppressDocumentClick();
        if (moved) {{
          event.preventDefault();
          event.stopPropagation();
          element.dataset.wasDragged = 'true';
          saveNotes();
          if (notesExport) notesExport.value = buildExport();
          showAnchorEffects(note.id, element);
          return;
        }}
      }}
      function cancelDrag() {{
        element.classList.remove('is-dragging');
        dragState = null;
        hideAnchorEffects(note.id);
      }}
      element.addEventListener('pointerdown', (event) => {{
        lastPointerDragAt = Date.now();
        beginDrag(event, event.pointerId);
      }});
      element.addEventListener('pointermove', moveDrag);
      element.addEventListener('pointerup', (event) => endDrag(event, event.pointerId));
      element.addEventListener('pointercancel', cancelDrag);
      element.addEventListener('mousedown', (event) => {{
        if (Date.now() - lastPointerDragAt < 180) return;
        beginDrag(event, null);
      }});
      element.addEventListener('mousemove', moveDrag);
      element.addEventListener('mouseup', (event) => endDrag(event, null));
      element.addEventListener('mouseleave', () => {{
        if (dragState && dragState.element === element && !dragState.moved) cancelDrag();
      }});
    }}

    function renderNotes() {{
      if (!notesList) return;
      const sorted = sortedNotes().filter(note => cleanText(note.text));
      notesList.replaceChildren();
      noteLayer?.querySelectorAll('.inline-note-view').forEach(view => view.remove());
      sorted.forEach((note, index) => {{
        const card = document.createElement('article');
        card.className = 'note-card';
        if (note.quote) {{
          const quote = document.createElement('blockquote');
          quote.textContent = note.quote;
          card.append(quote);
          highlightFirstText(note.quote, note.id);
        }}
        const body = document.createElement('p');
        body.textContent = note.text;
        card.append(body);
        notesList.append(card);
        const host = hostById(note.hostId || '');
        if (noteLayer && host && host !== activeNoteHost && !layerEditorForHost(host)) {{
          const view = document.createElement('div');
          view.className = 'inline-note-view';
          view.dataset.noNote = 'true';
          view.dataset.noteId = note.id;
          view.dataset.hostId = note.hostId || '';
          view.textContent = note.text;
          attachNoteInteraction(view, note, host, true);
          noteLayer.append(view);
          placeLayerItem(view, host, note);
        }}
      }});
      if (notesExport) notesExport.value = buildExport();
      placeAllLayerItems();
    }}

    function finishInlineNote(host) {{
      const editor = layerEditorForHost(host);
      if (!editor) {{
        activeNoteHost = null;
        hideAnchorEffects();
        renderNotes();
        return;
      }}
      const input = editor.querySelector('input');
      const noteId = editor.dataset.noteId;
      const text = cleanText(input?.value, 1600);
      const note = notes.find(item => item.id === noteId);
      if (note) note.text = text;
      if (!text && noteId) notes = notes.filter(note => note.id !== noteId);
      saveNotes();
      removeInlineEditors();
      renderNotes();
    }}

    function openInlineNote(context) {{
      const host = noteHost(context.node);
      if (activeNoteHost && activeNoteHost !== host) finishInlineNote(activeNoteHost);
      removeInlineEditors();
      activeNoteHost = host;
      const hostId = ensureHostId(host);
      noteLayer?.querySelectorAll(`.inline-note-view[data-host-id="${{cssEscape(hostId)}}"]`).forEach(view => view.remove());

      let note = noteForHost(host, context.quote || '');
      const point = notePointFromContext(host, context);
      const isNewNote = !note;
      if (!note) {{
        note = {{
          id: 'note-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
          text: '',
          quote: context.quote || '',
          order: orderForNode(context.node),
          hostId,
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString()
        }};
        notes.push(note);
      }}
      const missingAnchor = !Number.isFinite(note.relX) || !Number.isFinite(note.relY);
      if (point && (isNewNote || missingAnchor)) {{
        note.relX = point.relX;
        note.relY = point.relY;
      }}
      if (context.range && note.quote) highlightRange(context.range, note.id);

      const editor = document.createElement('div');
      editor.className = 'inline-note-pad';
      editor.dataset.noNote = 'true';
      editor.dataset.noteId = note.id;
      editor.dataset.hostId = hostId;
      editor.innerHTML = `
        <input aria-label="阅读笔记">
      `;
      const input = editor.querySelector('input');
      input.value = note.text || '';
      input.addEventListener('input', () => {{
        note.text = input.value;
        note.updatedAt = new Date().toISOString();
        scheduleSave();
      }});
      noteLayer?.append(editor);
      placeLayerItem(editor, host, note);
      attachNoteInteraction(editor, note, host, false);
      showAnchorEffects(note.id, editor);
      input.focus();
    }}

    function captureSelectionNote() {{
      const selection = window.getSelection();
      const quote = cleanText(selection?.toString(), 500);
      if (!quote || !selection || selection.rangeCount === 0) {{
        return false;
      }}
      const range = selection.getRangeAt(0).cloneRange();
      const node = range.commonAncestorContainer;
      openInlineNote({{
        quote,
        range,
        node
      }});
      return true;
    }}

    function copyNotes() {{
      const text = buildExport();
      if (!text) return;
      writeClipboard(text, notesExport);
    }}

    function downloadNotes() {{
      const text = buildExport();
      if (!text) return;
      const blob = new Blob([text], {{ type: 'text/plain;charset=utf-8' }});
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'source-reading-notes.txt';
      link.click();
      URL.revokeObjectURL(link.href);
    }}

    loadNotes();
    renderNotes();
    noteExportMode?.addEventListener('change', renderNotes);
    window.addEventListener('resize', placeAllLayerItems);
    window.addEventListener('scroll', placeAllLayerItems, {{ passive: true }});
    document.addEventListener('toggle', () => window.requestAnimationFrame(placeAllLayerItems), true);

    const noteIgnoreSelector = '[data-no-note], .report-nav, .hero-actions, button, input, textarea, select, summary';
    let selectionNoteOpenedAt = 0;

    function elementForNode(node) {{
      if (!node) return null;
      return node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    }}

    function openNoteFromSelection(sourceEvent = null) {{
      const selection = window.getSelection();
      const quote = cleanText(selection?.toString(), 500);
      if (!quote || quote.length < 2 || !selection || selection.rangeCount === 0 || selection.isCollapsed) return false;
      const range = selection.getRangeAt(0).cloneRange();
      const element = elementForNode(range.commonAncestorContainer);
      if (!element || element.closest(noteIgnoreSelector) || element.closest('#noteLayer')) return false;
      const rects = Array.from(range.getClientRects()).filter(rect => rect.width > 2 && rect.height > 2);
      if (!rects.length) return false;
      const editor = activeNoteHost ? layerEditorForHost(activeNoteHost) : null;
      if (editor && sourceEvent?.target && editor.contains(sourceEvent.target)) return false;
      if (activeNoteHost) finishInlineNote(activeNoteHost);
      suppressDocumentClick(520);
      selectionNoteOpenedAt = Date.now();
      openInlineNote({{
        quote,
        range,
        node: range.commonAncestorContainer
      }});
      return true;
    }}

    function runAction(action) {{
      if (action === 'focus-golden') pulse('golden');
      if (action === 'expand-functions') groups.forEach(group => group.open = true);
      if (action === 'collapse-functions') groups.forEach(group => group.open = false);
      if (action === 'expand-functions' || action === 'collapse-functions') {{
        window.requestAnimationFrame(placeAllLayerItems);
      }}
      if (action === 'clear-search' && search) {{
        search.value = '';
        applyFilter();
        search.focus();
      }}
      if (action === 'toggle-evidence') {{
        const table = document.querySelector('#evidence table');
        if (table) table.hidden = !table.hidden;
        pulse('evidence');
      }}
      if (action === 'toggle-theme') {{
        const next = document.body.classList.contains('theme-night') ? 'day' : 'night';
        storeSet(themeKey, next);
        applyTheme(next);
      }}
      if (action === 'copy-symbol-inventory') copySymbolInventory();
      if (action === 'copy-notes') copyNotes();
      if (action === 'download-notes') downloadNotes();
    }}

    function openNoteFromPointer(event) {{
      event.preventDefault();
      event.stopPropagation();
      const selection = window.getSelection();
      const quote = cleanText(selection?.toString(), 500);
      const range = quote && selection && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
      const node = range?.commonAncestorContainer || event.target;
      openInlineNote({{
        quote,
        range,
        node,
        clientX: event.clientX,
        clientY: event.clientY
      }});
    }}

    function handleDocumentClick(event) {{
      if (suppressNextDocumentClick || Date.now() < suppressDocumentClickUntil) {{
        suppressNextDocumentClick = false;
        window.clearTimeout(suppressDocumentClickTimer);
        event.preventDefault();
        event.stopPropagation();
        return;
      }}
      const button = eventClosest(event, '[data-action]');
      const editor = activeNoteHost ? layerEditorForHost(activeNoteHost) : null;
      if (editor && editor.contains(event.target)) return;
      if (activeNoteHost) {{
        finishInlineNote(activeNoteHost);
      }}
      if (button) {{
        runAction(button.dataset.action);
        return;
      }}
      if (eventClosest(event, noteIgnoreSelector) || event.defaultPrevented) return;
    }}

    function handleDocumentDoubleClick(event) {{
      if (eventClosest(event, '[data-action]') || eventClosest(event, noteIgnoreSelector) || event.defaultPrevented) return;
      if (Date.now() - selectionNoteOpenedAt < 520) return;
      const editor = activeNoteHost ? layerEditorForHost(activeNoteHost) : null;
      if (editor && editor.contains(event.target)) return;
      if (activeNoteHost) finishInlineNote(activeNoteHost);
      openNoteFromPointer(event);
    }}

    function handleDocumentMouseUp(event) {{
      if (eventClosest(event, '[data-action]') || eventClosest(event, noteIgnoreSelector) || event.defaultPrevented) return;
      window.setTimeout(() => openNoteFromSelection(event), 0);
    }}

    document.addEventListener('mouseup', handleDocumentMouseUp, true);
    document.addEventListener('click', handleDocumentClick, true);
    document.addEventListener('dblclick', handleDocumentDoubleClick, true);
  </script>
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
    parser.add_argument("--max-symbols", type=int, default=1200, help="Maximum function/class/method symbols to show in the inventory; use 0 for all")
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
            max_symbols=args.max_symbols,
            extract_to=args.extract_to,
            keep_temp=args.keep_temp,
            allow_output_in_repo=args.allow_output_in_repo,
        )
        title = args.title or source.resolve().stem or REPORT_HEADING
        document = render_full_html(report, scan_root=scan_root, title=title, subtitle=args.subtitle)
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
