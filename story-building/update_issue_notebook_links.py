"""Update GitHub issue bodies with links to their associated Jupyter notebooks from outline.md.

Outline node structure (two-line label inside mermaid):
  P1A["- [ ] [Ontology Loading & Inspection](https://github.com/<owner>/<repo>/issues/1) (0/6 • 0%)  \\
        [[notebooks/ontology_load_and_query.ipynb]]"]

Script actions:
 1. Parse `outline.md` mermaid diagram; collect (node_id, issue_number, notebook_path, issue_url).
 2. For each issue, fetch current body.
 3. If the notebook path already appears in the body (substring match), skip.
 4. Else append or update a Notebook section:
        ### Notebook\n
        - [ontology_load_and_query.ipynb](notebooks/ontology_load_and_query.ipynb)
    (If the section header already exists, add link under it if missing.)

Environment variables:
  GITHUB_TOKEN / GITHUB_TOKEN_FG   required
  USERNAME                         default: alexanderwiebe
  OUTLINE_FILE                    default: outline.md
  NOTEBOOK_SECTION_HEADER         default: ### Notebook
  DRY_RUN=1                       preview only, no writes
  ONLY_ISSUES=comma separated     restrict processing to these issue numbers

Idempotency:
  - Re-runs will not duplicate links (substring and exact line checks)
  - Section reused if present

Usage:
  DRY_RUN=1 python story-building/update_issue_notebook_links.py
  python story-building/update_issue_notebook_links.py
"""

from __future__ import annotations
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Dict, Set
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

USERNAME = os.getenv('USERNAME', 'alexanderwiebe')
TOKEN = os.getenv('GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN_FG')
OUTLINE_FILE = Path(os.getenv('OUTLINE_FILE', 'outline.md'))
NOTEBOOK_SECTION_HEADER = os.getenv('NOTEBOOK_SECTION_HEADER', '### Notebook')
DRY_RUN = os.getenv('DRY_RUN') == '1'
VERBOSE = os.getenv('VERBOSE') == '1'

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
REST_HEADERS = {
    'Authorization': f'token {TOKEN}',
    'Accept': 'application/vnd.github+json'
}


@dataclass
class NodeLink:
    node_id: str
    issue_number: int
    issue_url: str
    notebook_path: str


ISSUE_LINE_RE = re.compile(r'^(?P<indent>\s*)(?P<node>P\w+)\["- \[[ x]\] \[[^\]]+\]\(https://github.com/[^/]+/[^/]+/issues/(?P<num>\d+)\).*')
NOTEBOOK_LINE_RE = re.compile(r'^\s*\[\[([^\]]+\.ipynb)\]\]"\]')


def parse_outline(outline_text: str) -> List[NodeLink]:
    lines = outline_text.splitlines()
    results: List[NodeLink] = []
    for i, line in enumerate(lines):
        m = ISSUE_LINE_RE.match(line)
        if not m:
            continue
        if i + 1 >= len(lines):
            continue
        next_line = lines[i + 1]
        m2 = NOTEBOOK_LINE_RE.match(next_line.strip())
        if not m2:
            continue
        issue_number = int(m.group('num'))
        node_id = m.group('node')
        # Extract issue URL from the full matched part by re-search
        url_match = re.search(r'(https://github.com/[^)]+/issues/\d+)', line)
        if not url_match:
            continue
        issue_url = url_match.group(1)
        notebook_path = m2.group(1)
        results.append(NodeLink(node_id=node_id, issue_number=issue_number, issue_url=issue_url, notebook_path=notebook_path))
    return results


def fetch_issue(number: int) -> Optional[Dict]:
    r = requests.get(f'https://api.github.com/repos/{REPO_NAME}/issues/{number}', headers=REST_HEADERS, timeout=30)
    if r.status_code != 200:
        sys.stderr.write(f'WARN: fetch issue #{number} failed: {r.status_code}\n')
        return None
    return r.json()


def patch_issue(number: int, body: str) -> bool:
    if DRY_RUN:
        return True
    r = requests.patch(f'https://api.github.com/repos/{REPO_NAME}/issues/{number}', headers=REST_HEADERS, json={'body': body}, timeout=30)
    if r.status_code not in (200, 201):
        sys.stderr.write(f'ERROR: patch issue #{number} failed: {r.status_code} {r.text[:120]}\n')
        return False
    return True


def ensure_notebook_section(body: str, notebook_path: str) -> str:
    # Even if path appears, ensure it's within a proper section; if not, we'll still add section.
    path_present = notebook_path in body
    lines = body.splitlines()
    header_indices = [i for i, l in enumerate(lines) if l.strip() == NOTEBOOK_SECTION_HEADER]
    link_line = f'- [{os.path.basename(notebook_path)}]({notebook_path})'
    if header_indices:
        idx = header_indices[0]
        # Scan existing links under header
        insert_at = idx + 1
        while insert_at < len(lines) and lines[insert_at].strip().startswith('- ['):
            if lines[insert_at].strip() == link_line:
                # Already properly linked
                return body
            insert_at += 1
        # If path present elsewhere but not linked under header, still add link
        lines.insert(insert_at, link_line)
        return '\n'.join(lines).rstrip() + '\n'
    # Append new section
    if lines and lines[-1].strip() != '':
        lines.append('')
    lines.append(NOTEBOOK_SECTION_HEADER)
    lines.append(link_line)
    return '\n'.join(lines).rstrip() + '\n'


def main():
    if not OUTLINE_FILE.exists():
        sys.stderr.write(f'ERROR: outline file not found: {OUTLINE_FILE}\n')
        sys.exit(2)
    outline_text = OUTLINE_FILE.read_text(encoding='utf-8')
    nodes = parse_outline(outline_text)
    sys.stderr.write(f'Parsed nodes with issues: {len(nodes)}\n')
    updated = 0
    skipped = 0
    for node in nodes:
        if ONLY_ISSUES and node.issue_number not in ONLY_ISSUES:
            continue
        issue = fetch_issue(node.issue_number)
        if not issue:
            continue
        body = issue.get('body') or ''
        new_body = ensure_notebook_section(body, node.notebook_path)
        if new_body == body:
            skipped += 1
            if VERBOSE:
                sys.stderr.write(f'SKIP #{node.issue_number}: {node.notebook_path} already linked or present.\n')
            continue
        if patch_issue(node.issue_number, new_body):
            updated += 1
            sys.stderr.write(f'Updated issue #{node.issue_number} with notebook {node.notebook_path}\n')
        elif VERBOSE:
            sys.stderr.write(f'FAIL #{node.issue_number}: patch failed.\n')
    sys.stderr.write(f'Done. updated={updated} skipped(no change)={skipped}\n')
    if DRY_RUN:
        sys.stderr.write('DRY_RUN mode: no changes persisted.\n')


if __name__ == '__main__':
    main()
