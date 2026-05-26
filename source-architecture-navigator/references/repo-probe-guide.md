---
name: repo-probe-guide
description: 只读仓库探针脚本的使用说明和边界。
version: 1.0.0
---

# Repo Probe Guide

<!-- @类型: 工具参考 -->
<!-- @目的: 说明如何用只读脚本快速获得仓库体征，而不污染目标仓库 -->

`scripts/repo_probe.py` 用于读取仓库文件清单并输出摘要。默认只写终端输出，不在目标仓库落盘。

## 适用场景

- 第一次进入陌生仓库，需要快速知道入口候选、manifest 文件和主要语言。
- 用户说"我越看越乱"，需要先建立项目识别卡。
- 需要给 3-5 个文件阅读路线，但还没有入口线索。

## 不适用场景

- 不用它替代真正的源码阅读。
- 不用它证明运行时行为。
- 不用它判定废弃代码；废弃判断还需要调用关系、配置入口和运行证据。

## 命令

```bash
python scripts/repo_probe.py <repo-path>
python scripts/repo_probe.py <repo-path> --json
python scripts/repo_probe.py <repo-path> --max-files 5000
```

可选写出文件：

```bash
python scripts/repo_probe.py <repo-path> --output <outside-or-user-approved-path>
```

## 输出解读

- Project card: 仓库根、文件总数、主语言、入口候选。
- Manifests: 依赖和构建配置入口。
- Entry candidates: 可能的启动、路由、页面或任务入口。
- Top directories: 主要目录形状。
- Symbol samples: 只作为阅读起点，不代表完整调用图。
- Import edge samples: 只作为静态线索，不代表运行时完整依赖。

## 版本历史

- **v1.0.0** (2026-05-27) - 初始版本，定义只读探针使用边界。
