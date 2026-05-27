# Source Architecture Navigator Skill

This repository contains the `source-architecture-navigator` skill.

It helps read source-code architecture without polluting the target repository:

- classify messy code questions before answering
- build one-function maps, call-chain maps, data-flow maps, and layered architecture maps
- produce guided reading routes of 3-5 files
- identify evidence-based architecture risks
- require a change boundary card before implementation work

## Install

```bash
npx skills add https://github.com/gotocx/source-architecture-navigator-skill --skill source-architecture-navigator
```

## Local validation

```bash
cd source-architecture-navigator
python scripts/validate_skill.py --skill .
```

The HTML overview is at `source-architecture-navigator/assets/source-architecture-navigator.html`.

Generate a one-pass source analysis HTML report:

```bash
python source-architecture-navigator/scripts/render_navigation_html.py <repo-or-source.zip> --output <outside-analysis-dir>/source_navigation_report.html
```
