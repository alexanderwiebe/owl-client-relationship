"""Annotate outline.md tasks with sub-issue completion progress.

For each top-level story task line inside the mermaid block (two-line node label):
  P1A["- [ ] [Title](.../issues/<n>)  
      [[notebooks/....]]"]

We identify the parent issue number (<n>), gather sub-issues from the parent issue body
markdown checklist lines (pattern: '- [ ] #<child>' or '- [x] #<child>'), count closed vs total,
and append a progress annotation: (closed/total • P%). E.g.:

  P1A["- [ ] [Title](.../issues/1) (3/7 • 43%)  

Idempotent: existing progress annotations '(d+/d+ • d+%)' are replaced.

Environment variables:
  GITHUB_TOKEN / GITHUB_TOKEN_FG  required
  USERNAME                        default alexanderwiebe
  PROJECT_NUMBER                  currently unused (we use repo issues REST)
  OUTLINE_FILE                    default outline.md
  DRY_RUN=1                       preview only
  INCLUDE_ZERO=1                  include (0/0 • 0%) when no sub-issues found

Notes:
  - Exact issue link extraction expects '/issues/<number>' in the link.
  - Sub-issue detection relies on parent body checklists created earlier by automation.
"""

from __future__ import annotations
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

USERNAME = os.getenv('USERNAME', 'alexanderwiebe')
TOKEN = os.getenv('GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN_FG')
OUTLINE_FILE = Path(os.getenv('OUTLINE_FILE', 'outline.md'))
DRY_RUN = os.getenv('DRY_RUN') == '1'
INCLUDE_ZERO = os.getenv('INCLUDE_ZERO') == '1'
REPO_NAME = f"{USERNAME}/owl-client-relationship"

if not TOKEN:
    sys.stderr.write('ERROR: Missing GITHUB_TOKEN / GITHUB_TOKEN_FG\n')
    sys.exit(1)

REST_HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}


def list_repo_issues() -> Dict[int, dict]:
    issues: Dict[int, dict] = {}
    page = 1
    while True:
        r = requests.get(f'https://api.github.com/repos/{REPO_NAME}/issues', params={"state": "all", "per_page": 100, "page": page}, headers=REST_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f'Issue list failed page {page}: {r.status_code} {r.text[:160]}')
        batch = r.json()
        if not batch:
            break
        for it in batch:
            if 'pull_request' in it:
                continue
            issues[it['number']] = it
        page += 1
    return issues


PARENT_LINK_LINE = re.compile(r'^(?P<indent>\s*P\w+\["- \[)(?P<mark> |x)(?P<mid>\] )(?P<link>\[[^\]]+\]\(https://github.com/[^\)]+/issues/(?P<num>\d+)\))(?P<rest>.*)$')
PROGRESS_ANNOTATION = re.compile(r'\s*\(\d+/\d+\s+•\s+\d+%\)')
CHECKLIST_CHILD = re.compile(r'^- \[(?: |x|X)\] #(\d+)', re.MULTILINE)


def extract_child_numbers(parent_body: str) -> List[int]:
    nums = []
    for m in CHECKLIST_CHILD.finditer(parent_body or ''):
        try:
            nums.append(int(m.group(1)))
        except ValueError:
            pass
    return nums


def compute_progress(parent_issue: dict, issue_map: Dict[int, dict]) -> Tuple[int, int, int]:
    body = parent_issue.get('body') or ''
    child_nums = extract_child_numbers(body)
    total = len(child_nums)
    closed = 0
    for n in child_nums:
        child = issue_map.get(n)
        if child and child.get('state') == 'closed':
            closed += 1
    percent = int(round((closed / total) * 100)) if total else 0
    return closed, total, percent


def annotate_lines(lines: List[str], issue_map: Dict[int, dict]) -> List[str]:
    updated: List[str] = []
    changes = 0
    for line in lines:
        m = PARENT_LINK_LINE.match(line.rstrip('\n'))
        if m:
            num = int(m.group('num'))
            parent = issue_map.get(num)
            if parent:
                closed, total, percent = compute_progress(parent, issue_map)
                if total > 0 or INCLUDE_ZERO:
                    # Remove existing annotation before adding new
                    rest_clean = PROGRESS_ANNOTATION.sub('', m.group('rest'))
                    # Preserve two trailing spaces if present (markdown line break inside node label)
                    trailing_two = '  ' if rest_clean.endswith('  ') else ''
                    rest_core = rest_clean[:-2] if trailing_two else rest_clean
                    annotation = f' ({closed}/{total} • {percent}%)'
                    new_line = f"{m.group('indent')}{m.group('mark')}{m.group('mid')}{m.group('link')}{rest_core}{annotation}{trailing_two}"
                    if new_line != line:
                        line = new_line
                        changes += 1
        updated.append(line)
    sys.stderr.write(f'Progress annotations updated: {changes}\n')
    return updated


def main():
    try:
        issue_map = list_repo_issues()
    except Exception as e:
        sys.stderr.write(f'ERROR fetching issues: {e}\n')
        sys.exit(2)
    if not OUTLINE_FILE.exists():
        sys.stderr.write(f'Missing outline file: {OUTLINE_FILE}\n')
        sys.exit(2)
    lines = OUTLINE_FILE.read_text(encoding='utf-8').splitlines()
    new_lines = annotate_lines(lines, issue_map)
    if DRY_RUN:
        print('\n'.join(new_lines[:60]))
        return
    OUTLINE_FILE.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
    sys.stderr.write(f'Outline updated with progress annotations.\n')


if __name__ == '__main__':
    main()
