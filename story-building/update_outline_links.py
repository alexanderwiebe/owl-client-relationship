"""Update outline.md checklist items with links to GitHub issues.

Steps:
 1. Fetch all issues from the configured User Project V2 (issue number, title, url)
 2. Build a mapping Title -> Issue URL
 3. Parse `outline.md` and for each checklist bullet inside the mermaid block.

The current outline uses a two-line node label pattern inside the mermaid block:

        P1A["- [ ] Ontology Loading & Inspection  
                [[notebooks/ontology_load_and_query.ipynb]]"]

Notice the notebook link is on the next line and the first line ends with two spaces (Markdown line break).
This script now detects such two-line groups. If the task title matches an issue title and is not
already linked, it becomes:

        P1A["- [ ] [Ontology Loading & Inspection](https://github.com/.../issues/1)  
                [[notebooks/ontology_load_and_query.ipynb]]"]

Idempotency:
    - Lines already containing a markdown link right after the checkbox (pattern `- [ ] [`) are skipped.
    - Only exact (case sensitive) title matches are replaced.

Environment variables:
    GITHUB_TOKEN / GITHUB_TOKEN_FG  required
    USERNAME                        default: alexanderwiebe
    PROJECT_NUMBER                  default: 1
    OUTLINE_FILE                    default: outline.md
    DRY_RUN=1                       preview only

Limitations:
    - Exact title matching (no fuzzy search).
    - Assumes notebook link is on the immediate following line beginning with optional spaces then `[[`.
"""

from __future__ import annotations
import os
import sys
import re
from pathlib import Path
from typing import Dict, Any, List
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

USERNAME = os.getenv('USERNAME', 'alexanderwiebe')
PROJECT_NUMBER = int(os.getenv('PROJECT_NUMBER', '1'))
TOKEN = os.getenv('GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN_FG')
DRY_RUN = os.getenv('DRY_RUN') == '1'
OUTLINE_FILE = Path(os.getenv('OUTLINE_FILE', 'outline.md'))

if not TOKEN:
    sys.stderr.write('ERROR: Missing GITHUB_TOKEN / GITHUB_TOKEN_FG\n')
    sys.exit(1)

GQL_ENDPOINT = 'https://api.github.com/graphql'
HEADERS = {"Authorization": f"bearer {TOKEN}", "Accept": "application/vnd.github+json"}


def gql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    resp = requests.post(GQL_ENDPOINT, json={'query': query, 'variables': variables}, headers=HEADERS, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f'Non-JSON response: {resp.status_code} {resp.text[:200]}')
    if 'errors' in data:
        raise RuntimeError(f'GraphQL errors: {data["errors"]}')
    return data


def fetch_project_issues() -> List[Dict[str, Any]]:
    q = """
    query($login:String!, $num:Int!, $cursor:String){
      user(login:$login){
        projectV2(number:$num){
          items(first:100, after:$cursor){
            pageInfo{hasNextPage endCursor}
            nodes{ content{ __typename ... on Issue { id number title url state } } }
          }
        }
      }
    }
    """
    cursor = None
    issues: List[Dict[str, Any]] = []
    while True:
        data = gql(q, {'login': USERNAME, 'num': PROJECT_NUMBER, 'cursor': cursor})
        proj = data['data']['user'].get('projectV2')
        if not proj:
            raise RuntimeError(f'Project {PROJECT_NUMBER} not found for user {USERNAME}')
        items = proj['items']
        for node in items['nodes']:
            c = node.get('content')
            if c and c.get('__typename') == 'Issue':
                issues.append({
                    'number': c['number'],
                    'title': c['title'],
                    'url': c['url'],
                    'state': c.get('state')
                })
        if not items['pageInfo']['hasNextPage']:
            break
        cursor = items['pageInfo']['endCursor']
    return issues


def load_outline() -> List[str]:
    if not OUTLINE_FILE.exists():
        raise FileNotFoundError(f'Missing outline file: {OUTLINE_FILE}')
    return OUTLINE_FILE.read_text(encoding='utf-8').splitlines()


def update_lines(lines: List[str], issues_by_title: Dict[str, Dict[str, Any]]) -> List[str]:
    updated: List[str] = []
    changed = 0
    # Pattern for first line of a two-line task node label capturing checkbox mark (space or x)
    first_line_pat = re.compile(r'^(?P<prefix>\s*P\w+\["- \[)(?P<mark> |x)(?P<post>\] )(?!\[)(?P<rest>.+?)(?P<trail>\s{2})$')
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        # Quick filter: must contain '- [ ] '
        if '- [' in line and 'P' in line:
            m = first_line_pat.match(line.rstrip('\n'))
            if m and i + 1 < total:
                next_line = lines[i + 1]
                if '[[notebooks/' in next_line:
                    mark = m.group('mark')  # ' ' or 'x'
                    rest = m.group('rest')  # may already contain link
                    raw_title = rest.strip()
                    # If already link, extract title inside first []
                    if raw_title.startswith('[') and '](' in raw_title:
                        # title is between first '[' and first ']'
                        end = raw_title.find('](')
                        if end != -1:
                            plain_title = raw_title[1:end]
                        else:
                            plain_title = raw_title
                    else:
                        plain_title = raw_title
                    issue = issues_by_title.get(plain_title)
                    if issue:
                        issue_url = issue['url']
                        desired_mark = 'x' if (issue.get('state') == 'CLOSED') else mark
                        # Build linked title if not already linked
                        if not (raw_title.startswith('[') and '](' in raw_title):
                            linked_title = f'[{plain_title}]({issue_url})'
                        else:
                            linked_title = raw_title  # keep existing link
                        new_line = f"{m.group('prefix')}{desired_mark}{m.group('post')}{linked_title}{m.group('trail')}"
                        if new_line != line:
                            line = new_line
                            changed += 1
        updated.append(line)
        i += 1
    sys.stderr.write(f'Lines changed: {changed}\n')
    return updated


def main():
    try:
        issues = fetch_project_issues()
    except Exception as e:
        sys.stderr.write(f'ERROR fetching project issues: {e}\n')
        sys.exit(2)
    issues_by_title = {it['title']: it for it in issues}
    sys.stderr.write(f'Issues loaded: {len(issues_by_title)}\n')
    try:
        lines = load_outline()
    except Exception as e:
        sys.stderr.write(f'ERROR reading outline: {e}\n')
        sys.exit(2)
    new_lines = update_lines(lines, issues_by_title)
    if DRY_RUN:
        sys.stderr.write('DRY_RUN=1; not writing changes. Preview below (first 40 lines if long):\n')
        preview = '\n'.join(new_lines[:40])
        print(preview)
        return
    OUTLINE_FILE.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
    sys.stderr.write(f'Updated file written: {OUTLINE_FILE}\n')


if __name__ == '__main__':
    main()
