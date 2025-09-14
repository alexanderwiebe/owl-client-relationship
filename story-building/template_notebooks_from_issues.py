"""Generate or update Jupyter notebook templates for each outlined parent issue.

Data sources:
  * outline.md (Mermaid nodes) to map issue number -> notebook path
  * GitHub issues API for parent issue body + sub-issue checklist

Notebook Template Structure (auto-managed portion):
  Cell 1 (markdown): Title header with link to parent issue
  Cell 2 (markdown): Parent issue description (trimmed; excludes auto sections)
  Cells 3..N: For each sub-issue:
      Markdown heading with link + status
      (Optional) code cell scaffold placeholder (enabled if INCLUDE_CODE_PLACEHOLDERS=1)

Idempotency & Merge Behavior:
    * If notebook does not exist: create new with template cells.
    * If exists and OVERWRITE=1: rebuild entire notebook (full regeneration).
    * If exists and OVERWRITE not set: MERGE MODE
                - Keep all existing cells
                - Update header (title, link, status) if present in first markdown cell
                - Append new sub-issue sections for any child issues not already represented
                - Never delete user-authored content or existing sub-issue sections

Environment Variables:
  GITHUB_TOKEN / GITHUB_TOKEN_FG   required
  USERNAME                         default alexanderwiebe
  OUTLINE_FILE                     default outline.md
  DRY_RUN=1                        no file writes
  ONLY_ISSUES=comma list           restrict to these parent issue numbers
  OVERWRITE=1                      allow replacing existing notebooks
  INCLUDE_CODE_PLACEHOLDERS=1      add empty code cell after each sub-issue section
  MAX_SUBISSUES                    limit number of sub-issue sections (for brevity/testing)

Limitations / Assumptions:
  * Parent issue body contains checklist lines of form '- [ ] #<num>' or '- [x] #<num>' optionally followed by an em dash and title.
  * Notebook path from outline markdown: second line of the node label contains '[[notebooks/...ipynb]]'.
    * Merge mode preserves arbitrary user cells, only appends missing sub-issue sections and refreshes header.
"""

from __future__ import annotations
import os
import re
import sys
import json
import uuid
from pathlib import Path
from typing import List, Dict, Optional, Set
import requests
from dataclasses import dataclass
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

USERNAME = os.getenv('USERNAME', 'alexanderwiebe')
TOKEN = os.getenv('GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN_FG')
OUTLINE_FILE = Path(os.getenv('OUTLINE_FILE', 'outline.md'))
DRY_RUN = os.getenv('DRY_RUN') == '1'
OVERWRITE = os.getenv('OVERWRITE') == '1'
INCLUDE_CODE_PLACEHOLDERS = os.getenv('INCLUDE_CODE_PLACEHOLDERS') == '1'
MAX_SUBISSUES = int(os.getenv('MAX_SUBISSUES', '0'))  # 0 = no limit
REFRESH_STATUS = os.getenv('REFRESH_STATUS') == '1'  # force status line refresh even if header differs

ONLY_ISSUES_ENV = os.getenv('ONLY_ISSUES')
ONLY_ISSUES: Set[int] = set()
if ONLY_ISSUES_ENV:
    for part in ONLY_ISSUES_ENV.split(','):
        part = part.strip()
        if part.isdigit():
            ONLY_ISSUES.add(int(part))

if not TOKEN:
    sys.stderr.write('ERROR: Missing GITHUB_TOKEN / GITHUB_TOKEN_FG\n')
    sys.exit(1)

REPO_NAME = f'{USERNAME}/owl-client-relationship'
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}

PARENT_ISSUE_RE = re.compile(r'^(?P<indent>\s*)(?P<node>P\w+)\["- \[[ x]\] \[[^\]]+\]\(https://github.com/[^/]+/[^/]+/issues/(?P<num>\d+)\)')
NOTEBOOK_LINE_RE = re.compile(r'^\s*\[\[(?P<path>notebooks/[^\]]+\.ipynb)\]\]"?\]')
CHECKLIST_CHILD = re.compile(r'^- \[(?: |x|X)\] #(\d+)', re.MULTILINE)
SUB_ISSUE_HEADING_RE = re.compile(r'^### Sub-issue #(\d+):')


@dataclass
class ParentMapping:
    issue_number: int
    notebook_path: str


def parse_outline(outline_text: str) -> List[ParentMapping]:
    lines = outline_text.splitlines()
    mappings: List[ParentMapping] = []
    for i, line in enumerate(lines):
        m = PARENT_ISSUE_RE.match(line)
        if not m:
            continue
        if i + 1 >= len(lines):
            continue
        m2 = NOTEBOOK_LINE_RE.match(lines[i + 1].strip())
        if not m2:
            continue
        issue_number = int(m.group('num'))
        nb_path = m2.group('path')
        mappings.append(ParentMapping(issue_number, nb_path))
    return mappings


def fetch_issue(number: int) -> Optional[Dict]:
    r = requests.get(f'https://api.github.com/repos/{REPO_NAME}/issues/{number}', headers=HEADERS, timeout=30)
    if r.status_code != 200:
        sys.stderr.write(f'WARN: fetch issue #{number} failed: {r.status_code}\n')
        return None
    return r.json()


def fetch_issues(numbers: List[int]) -> Dict[int, Dict]:
    result: Dict[int, Dict] = {}
    for n in numbers:
        issue = fetch_issue(n)
        if issue:
            result[n] = issue
    return result


def extract_child_numbers(parent_body: str) -> List[int]:
    nums = []
    for m in CHECKLIST_CHILD.finditer(parent_body or ''):
        try:
            nums.append(int(m.group(1)))
        except ValueError:
            pass
    return nums


def sanitize_description(body: str) -> str:
    # Remove auto sections like '### Notebook' and checklist lines referencing sub-issues
    lines = body.splitlines()
    cleaned: List[str] = []
    skip_notebook = False
    for l in lines:
        if l.strip().lower() == '### notebook':
            skip_notebook = True
            continue
        if skip_notebook:
            if l.startswith('### '):
                skip_notebook = False
            else:
                continue
        if CHECKLIST_CHILD.match(l.strip()):
            continue
        cleaned.append(l)
    text = '\n'.join(cleaned).strip()
    return text


def build_notebook_json(parent_issue: Dict, child_issues: List[Dict]) -> Dict:
    title = parent_issue.get('title', '(No Title)')
    number = parent_issue.get('number')
    url = parent_issue.get('html_url') or parent_issue.get('url')
    state = parent_issue.get('state')
    description = sanitize_description(parent_issue.get('body') or '')

    def new_id() -> str:
        return uuid.uuid4().hex[:8]

    def md_cell(text: str) -> Dict:
        return {"id": new_id(), "cell_type": "markdown", "metadata": {"language": "markdown"}, "source": text.splitlines()}

    def code_cell(text: str = '') -> Dict:
        return {"id": new_id(), "cell_type": "code", "execution_count": None, "metadata": {"language": "python"}, "outputs": [], "source": text.splitlines()}

    cells: List[Dict] = []
    header_lines = [f"# {title}", '', f"Parent Issue: [#{number}]({url})", f"Status: {state}"]
    cells.append(md_cell('\n'.join(header_lines)))
    if description:
        cells.append(md_cell(description))

    if child_issues:
        cells.append(md_cell('## Sub-Issues Overview'))
    for ch in child_issues:
        cnum = ch.get('number')
        curl = ch.get('html_url') or ch.get('url')
        ctitle = ch.get('title')
        cstate = ch.get('state')
        section_md = f"### Sub-issue #{cnum}: {ctitle}\n\nLink: [#{cnum}]({curl})\n\nStatus: {cstate}\n\nImplementation Notes:"  # placeholder
        cells.append(md_cell(section_md))
        if INCLUDE_CODE_PLACEHOLDERS:
            cells.append(code_cell('# TODO: code / experiments for sub-issue #' + str(cnum)))

    nb = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"}
        },
        "nbformat": 4,
        "nbformat_minor": 5
    }
    return nb


def load_existing_notebook(path: Path) -> Optional[Dict]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return None
        if 'cells' not in data or not isinstance(data['cells'], list):
            return None
        # Repair missing structural keys (previous versions wrote partial JSON)
        changed = False
        if 'metadata' not in data or not isinstance(data['metadata'], dict):
            data['metadata'] = {
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                "language_info": {"name": "python"}
            }
            changed = True
        else:
            md = data['metadata']
            if 'kernelspec' not in md:
                md['kernelspec'] = {"display_name": "Python 3", "language": "python", "name": "python3"}; changed = True
            if 'language_info' not in md:
                md['language_info'] = {"name": "python"}; changed = True
        if 'nbformat' not in data:
            data['nbformat'] = 4; changed = True
        if 'nbformat_minor' not in data:
            data['nbformat_minor'] = 5; changed = True
        if changed:
            sys.stderr.write(f'Repaired notebook structure: {path}\n')
        return data
    except Exception:
        return None


def extract_existing_subissue_numbers(nb: Dict) -> set:
    existing = set()
    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'markdown':
            continue
        lines = cell.get('source') or []
        for line in lines:
            if isinstance(line, str):
                m = SUB_ISSUE_HEADING_RE.match(line.strip())
                if m:
                    try:
                        existing.add(int(m.group(1)))
                    except ValueError:
                        pass
    return existing


def update_header_cell(nb: Dict, parent_issue: Dict):
    if not nb.get('cells'):
        return
    first = nb['cells'][0]
    if first.get('cell_type') != 'markdown':
        return
    title = parent_issue.get('title', '(No Title)')
    number = parent_issue.get('number')
    url = parent_issue.get('html_url') or parent_issue.get('url')
    state = parent_issue.get('state')
    desired_header = [f"# {title}", '', f"Parent Issue: [#{number}]({url})", f"Status: {state}"]
    # Normalize existing header to compare core lines ignoring trailing whitespace
    existing_lines = [l.rstrip('\n') for l in first.get('source', [])]
    # Replace if mismatch in first heading line or REFRESH_STATUS requested
    if not existing_lines or existing_lines[0] != desired_header[0] or REFRESH_STATUS:
        first['source'] = desired_header
    if 'id' not in first:
        first['id'] = uuid.uuid4().hex[:8]


def ensure_cell_ids(nb: Dict) -> int:
    changed = 0
    for cell in nb.get('cells', []):
        if 'id' not in cell:
            cell['id'] = uuid.uuid4().hex[:8]
            changed += 1
    return changed


def append_new_subissue_sections(nb: Dict, parent_issue: Dict, child_issues: List[Dict]):
    existing_nums = extract_existing_subissue_numbers(nb)
    to_add = [ci for ci in child_issues if ci.get('number') not in existing_nums]
    if not to_add:
        return 0
    def new_id() -> str:
        return uuid.uuid4().hex[:8]
    def md_cell(text: str) -> Dict:
        return {"id": new_id(), "cell_type": "markdown", "metadata": {"language": "markdown"}, "source": text.splitlines()}
    def code_cell(text: str = '') -> Dict:
        return {"id": new_id(), "cell_type": "code", "execution_count": None, "metadata": {"language": "python"}, "outputs": [], "source": text.splitlines()}
    # Ensure there is a '## Sub-Issues Overview' section; add if missing
    has_overview = any(
        cell.get('cell_type') == 'markdown' and any(isinstance(line,str) and line.strip() == '## Sub-Issues Overview' for line in cell.get('source', []))
        for cell in nb.get('cells', [])
    )
    if to_add and not has_overview:
        nb['cells'].append(md_cell('## Sub-Issues Overview'))
    for ch in to_add:
        cnum = ch.get('number')
        curl = ch.get('html_url') or ch.get('url')
        ctitle = ch.get('title')
        cstate = ch.get('state')
        section_md = f"### Sub-issue #{cnum}: {ctitle}\n\nLink: [#{cnum}]({curl})\n\nStatus: {cstate}\n\nImplementation Notes:"
        nb['cells'].append(md_cell(section_md))
        if INCLUDE_CODE_PLACEHOLDERS:
            nb['cells'].append(code_cell('# TODO: code / experiments for sub-issue #' + str(cnum)))
    return len(to_add)


def write_notebook(path: Path, nb: Dict):
    if DRY_RUN:
        sys.stderr.write(f'DRY_RUN: would write {path}\n')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(nb, indent=2) + '\n', encoding='utf-8')


def main():
    if not OUTLINE_FILE.exists():
        sys.stderr.write(f'ERROR: outline file not found: {OUTLINE_FILE}\n')
        sys.exit(2)
    outline_text = OUTLINE_FILE.read_text(encoding='utf-8')
    mappings = parse_outline(outline_text)
    sys.stderr.write(f'Found {len(mappings)} parent mappings in outline.\n')
    for mp in mappings:
        if ONLY_ISSUES and mp.issue_number not in ONLY_ISSUES:
            continue
        issue = fetch_issue(mp.issue_number)
        if not issue:
            continue
        child_nums = extract_child_numbers(issue.get('body') or '')
        if MAX_SUBISSUES:
            child_nums = child_nums[:MAX_SUBISSUES]
        child_issues = []
        for cn in child_nums:
            ch = fetch_issue(cn)
            if ch:
                child_issues.append(ch)
        nb_path = ROOT / mp.notebook_path
        if nb_path.exists():
            if OVERWRITE:
                nb_json = build_notebook_json(issue, child_issues)
                write_notebook(nb_path, nb_json)
                sys.stderr.write(f'REGENERATED notebook (OVERWRITE=1) for issue #{mp.issue_number}: {nb_path}\n')
            else:
                existing_nb = load_existing_notebook(nb_path)
                if not existing_nb:
                    sys.stderr.write(f'WARN invalid notebook JSON; skipping merge (use OVERWRITE=1 to rebuild): {nb_path}\n')
                    continue
                update_header_cell(existing_nb, issue)
                added_ids = ensure_cell_ids(existing_nb)
                added_sections = append_new_subissue_sections(existing_nb, issue, child_issues)
                if added_ids == 0 and added_sections == 0:
                    sys.stderr.write(f'NO-CHANGE notebook (already up-to-date) #{mp.issue_number}: {nb_path}\n')
                else:
                    write_notebook(nb_path, existing_nb)
                    sys.stderr.write(f'UPDATED notebook #{mp.issue_number}: +sections={added_sections} +ids={added_ids}: {nb_path}\n')
        else:
            nb_json = build_notebook_json(issue, child_issues)
            write_notebook(nb_path, nb_json)
            sys.stderr.write(f'CREATED notebook for issue #{mp.issue_number}: {nb_path}\n')
    sys.stderr.write('Done.\n')


if __name__ == '__main__':
    main()
