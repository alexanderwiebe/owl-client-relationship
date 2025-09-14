"""Step 4: Full automation over all parent issues.

Combines Steps 1–3:
  * Enumerate all issues in the user Project V2 (excluding PRs)
  * Optionally filter / limit which parents are processed
  * For each parent issue:
       - Detect existing sub-issues (pattern: "Story #<parent> – <Task Title>")
       - Optionally skip if any sub-issues already exist (SKIP_IF_HAS_SUBISSUES=1)
       - Call GPT to decompose (idempotent: only create missing sub-issues)
       - Add new sub-issues to project
       - Append checklist entries in parent body for new sub-issues only

Environment Variables:
  GITHUB_TOKEN / GITHUB_TOKEN_FG   required
  OPENAI_API_KEY                   required
  USERNAME                         default: alexanderwiebe
  PROJECT_NUMBER                   default: 1 (user project number)
  MODEL                            default: gpt-3.5-turbo
  TEMPERATURE                      default: 0.2
  MAX_TASKS                        default: 12
  DRY_RUN=1                        no writes
  RATE_DELAY=0.3                   delay between writes
  ONLY_ISSUES=comma list           restrict to these issue numbers
  START_AT                         skip parents with number < START_AT
  MAX_PARENTS                      stop after processing N parents (for testing)
  SKIP_IF_HAS_SUBISSUES=1          if parent already has any sub-issue, skip decomposition
  STORY_PREFIX                     override prefix, default 'Story #'

Usage:
  DRY_RUN=1 python story-building/step4_full_run.py
  ONLY_ISSUES=1,5,12 python story-building/step4_full_run.py
  python story-building/step4_full_run.py | 

Idempotency:
  - Title pattern uniqueness guarantees skip on re-run
  - Parent body checklist not duplicated
  - Safe to re-run after model prompt changes; only new tasks produce new issues
"""

from __future__ import annotations
import os
import sys
import time
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Set
import requests
from dotenv import load_dotenv

try:
    from openai import OpenAI
    _USE_NEW_CLIENT = True
except ImportError:
    from importlib import import_module
    try:
        openai = import_module('openai')  # legacy
    except Exception:  # pragma: no cover
        openai = None
    _USE_NEW_CLIENT = False

# ---------- Config & Env ----------
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

USERNAME = os.getenv('USERNAME', 'alexanderwiebe')
PROJECT_NUMBER = int(os.getenv('PROJECT_NUMBER', '1'))
MODEL = os.getenv('MODEL', 'gpt-3.5-turbo')
TEMPERATURE = float(os.getenv('TEMPERATURE', '0.2'))
MAX_TASKS = int(os.getenv('MAX_TASKS', '12'))
DRY_RUN = os.getenv('DRY_RUN') == '1'
RATE_DELAY = float(os.getenv('RATE_DELAY', '0.3'))
STORY_PREFIX = os.getenv('STORY_PREFIX', 'Story #')  # used in sub-issue titles
SKIP_IF_HAS_SUBISSUES = os.getenv('SKIP_IF_HAS_SUBISSUES') == '1'
CACHE_TASKS = os.getenv('CACHE_TASKS') == '1'
REGENERATE_TASKS = os.getenv('REGENERATE_TASKS') == '1'
CACHE_DIR = Path(os.getenv('CACHE_DIR', '.story_decomp_cache'))

TOKEN = os.getenv('GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN_FG')
OPENAI_KEY = os.getenv('OPENAI_API_KEY')
if not TOKEN:
    sys.stderr.write('ERROR: Missing GITHUB_TOKEN / GITHUB_TOKEN_FG\n')
    sys.exit(1)
if not OPENAI_KEY:
    sys.stderr.write('ERROR: Missing OPENAI_API_KEY\n')
    sys.exit(1)

if _USE_NEW_CLIENT:
    client = OpenAI(api_key=OPENAI_KEY)
else:
    if openai is None:
        sys.stderr.write('ERROR: openai library not available.\n')
        sys.exit(1)
    openai.api_key = OPENAI_KEY

REPO_NAME = f'{USERNAME}/owl-client-relationship'
REST_HEADERS = {'Authorization': f'token {TOKEN}',
                'Accept': 'application/vnd.github+json'}
GQL_ENDPOINT = 'https://api.github.com/graphql'
GQL_HEADERS = {'Authorization': f'bearer {TOKEN}',
               'Accept': 'application/vnd.github+json'}

ONLY_ISSUES_ENV = os.getenv('ONLY_ISSUES')
ONLY_ISSUES: Set[int] = set()
if ONLY_ISSUES_ENV:
    for part in ONLY_ISSUES_ENV.split(','):
        part = part.strip()
        if part.isdigit():
            ONLY_ISSUES.add(int(part))

START_AT = int(os.getenv('START_AT', '0'))
MAX_PARENTS = int(os.getenv('MAX_PARENTS', '0'))  # 0 = no limit

# ---------- Helpers ----------


def gql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(GQL_ENDPOINT, json={
                      'query': query, 'variables': variables}, headers=GQL_HEADERS)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f'Non-JSON GraphQL response {r.status_code}: {r.text[:200]}')
    if 'errors' in data:
        raise RuntimeError(f'GraphQL errors: {data["errors"]}')
    return data


def get_project_id() -> str:
    q = 'query($login:String!,$n:Int!){ user(login:$login){ projectV2(number:$n){ id } } }'
    d = gql(q, {'login': USERNAME, 'n': PROJECT_NUMBER})
    proj = d['data']['user'].get('projectV2')
    if not proj:
        raise RuntimeError(
            f'Project {PROJECT_NUMBER} not found for user {USERNAME}')
    return proj['id']


def fetch_project_issue_items() -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    cursor = None
    q = """
    query($login:String!, $num:Int!, $cursor:String){
      user(login:$login){ projectV2(number:$num){ items(first:100, after:$cursor){ pageInfo{hasNextPage endCursor} nodes{ content{ __typename ... on Issue { id number title body url state } } } } } }
    }
    """
    while True:
        d = gql(q, {'login': USERNAME, 'num': PROJECT_NUMBER, 'cursor': cursor})
        items = d['data']['user']['projectV2']['items']
        for node in items['nodes']:
            c = node.get('content')
            if c and c.get('__typename') == 'Issue':
                issues.append({
                    'number': c['number'],
                    'title': c['title'],
                    'body': c.get('body') or '',
                    'url': c.get('url'),
                    'state': c.get('state'),
                    'id': c['id'],  # node id
                })
        if not items['pageInfo']['hasNextPage']:
            break
        cursor = items['pageInfo']['endCursor']
    return issues


def list_repo_issues() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        r = requests.get(f'https://api.github.com/repos/{REPO_NAME}/issues', params={
                         'state': 'all', 'per_page': 100, 'page': page}, headers=REST_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(
                f'Issues fetch failed page {page}: {r.status_code} {r.text[:120]}')
        batch = r.json()
        if not batch:
            break
        for it in batch:
            if 'pull_request' in it:
                continue
            out.append(it)
        page += 1
    return out


def existing_subissue_titles_for(parent_number: int, repo_issues: List[Dict[str, Any]]) -> Dict[str, int]:
    pat = re.compile(rf'^Story #{parent_number} – ')
    mapping: Dict[str, int] = {}
    for it in repo_issues:
        t = it.get('title', '')
        if pat.match(t):
            mapping[t] = it['number']
    return mapping


def parent_has_any_subissue(parent_number: int, repo_issues: List[Dict[str, Any]]) -> bool:
    pat = re.compile(rf'^Story #{parent_number} – ')
    return any(pat.match(it.get('title', '')) for it in repo_issues)


def fetch_issue_node_id(number: int) -> str:
    q = 'query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){ issue(number:$n){ id } } }'
    owner, repo = REPO_NAME.split('/')
    d = gql(q, {'o': owner, 'r': repo, 'n': number})
    issue_obj = d['data']['repository'].get('issue')
    if not issue_obj:
        raise RuntimeError(f'Cannot resolve node id for issue #{number}')
    return issue_obj['id']


def call_gpt(title: str, body: str) -> List[Dict[str, str]]:
    story_text = f'TITLE: {title}\n\nBODY:\n{body}'.strip()
    prompt = (
        'You are an assistant splitting a GitHub issue (user story) into actionable development tasks. '
        'Return ONLY a JSON array, no commentary. Each element MUST have "title" and "description". '
        f'Limit to at most {MAX_TASKS} tasks. Titles <= 70 chars. Avoid duplicates.\n\n' + story_text
    )
    if _USE_NEW_CLIENT:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=TEMPERATURE,
            max_tokens=700,
        )
        content = resp.choices[0].message.content.strip()
    else:
        resp = openai.ChatCompletion.create(
            model=MODEL,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=TEMPERATURE,
            max_tokens=700,
        )
        content = resp.choices[0].message.content.strip()
    m = re.search(r'(\[.*\])', content, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    tasks: List[Dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                t = (item.get('title') or item.get('name') or '').strip()
                dsc = (item.get('description') or '').strip()
                if t:
                    tasks.append({'title': t, 'description': dsc})
    return tasks[:MAX_TASKS]


def cache_path_for(parent_number: int) -> Path:
    return CACHE_DIR / f'{parent_number}.tasks.json'


def load_cached_tasks(parent_number: int) -> List[Dict[str, str]]:
    if not CACHE_TASKS:
        return []
    p = cache_path_for(parent_number)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, dict) and 'title' in item:
                    out.append(
                        {'title': item['title'], 'description': item.get('description', '')})
            return out
    except Exception:
        return []
    return []


def save_cached_tasks(parent_number: int, tasks: List[Dict[str, str]]):
    if not CACHE_TASKS:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path_for(parent_number).write_text(json.dumps(tasks, indent=2))
    except Exception as e:
        sys.stderr.write(
            f'  WARNING: failed to write cache for parent {parent_number}: {e}\n')


def create_issue(title: str, body: str) -> Dict[str, Any]:
    if DRY_RUN:
        return {'number': -1, 'title': title, 'body': body}
    r = requests.post(f'https://api.github.com/repos/{REPO_NAME}/issues',
                      headers=REST_HEADERS, json={'title': title, 'body': body}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f'Create issue failed: {r.status_code} {r.text[:140]}')
    return r.json()


def add_to_project(project_id: str, node_id: str):
    if DRY_RUN:
        return
    m = 'mutation($pid:ID!,$cid:ID!){ addProjectV2ItemById(input:{projectId:$pid,contentId:$cid}){ item { id } } }'
    gql(m, {'pid': project_id, 'cid': node_id})


def append_checklist(parent_number: int, new_child_numbers: List[int]):
    if not new_child_numbers:
        return
    r = requests.get(
        f'https://api.github.com/repos/{REPO_NAME}/issues/{parent_number}', headers=REST_HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(
            f'Parent fetch failed: {r.status_code} {r.text[:120]}')
    parent = r.json()
    body = parent.get('body') or ''
    existing_nums = extract_existing_child_numbers(body)
    add_nums = [n for n in new_child_numbers if n not in existing_nums]
    if not add_nums:
        return
    lines = body.rstrip().splitlines()
    if lines and lines[-1].strip() != '':
        lines.append('')
    if not any(l.strip().lower().startswith('### sub-issues') for l in lines):
        lines.append('### Sub-issues')
    for n in add_nums:
        if DRY_RUN:
            title = '(DRY_RUN)'
        else:
            cr = requests.get(
                f'https://api.github.com/repos/{REPO_NAME}/issues/{n}', headers=REST_HEADERS, timeout=30)
            title = cr.json().get(
                'title', '(no title)') if cr.status_code == 200 else '(title unavailable)'
        lines.append(f'- [ ] #{n} — {title}')
    new_body = '\n'.join(lines).rstrip() + '\n'
    if DRY_RUN:
        sys.stderr.write(
            f'DRY_RUN: would update parent #{parent_number} checklist with {len(add_nums)} entries.\n')
        return
    pr = requests.patch(f'https://api.github.com/repos/{REPO_NAME}/issues/{parent_number}',
                        headers=REST_HEADERS, json={'body': new_body}, timeout=30)
    if pr.status_code not in (200, 201):
        raise RuntimeError(
            f'Checklist update failed: {pr.status_code} {pr.text[:120]}')


def extract_existing_child_numbers(body: str) -> Set[int]:
    pat = re.compile(r'^- \[.\] #(\d+)', re.MULTILINE)
    nums: Set[int] = set()
    for m in pat.finditer(body or ''):
        try:
            nums.add(int(m.group(1)))
        except ValueError:
            pass
    return nums


def process_parent(project_id: str, parent: Dict[str, Any], repo_issues: List[Dict[str, Any]]) -> None:
    pnum = parent['number']
    if ONLY_ISSUES and pnum not in ONLY_ISSUES:
        return
    if pnum < START_AT:
        return
    sys.stderr.write(f'Parent #{pnum}: {parent["title"]}\n')
    if SKIP_IF_HAS_SUBISSUES and parent_has_any_subissue(pnum, repo_issues):
        sys.stderr.write('  Skipping (already has sub-issues)\n')
        return
    existing_map = existing_subissue_titles_for(pnum, repo_issues)
    tasks = []
    if CACHE_TASKS and not REGENERATE_TASKS:
        tasks = load_cached_tasks(pnum)
        if tasks:
            sys.stderr.write(f'  Loaded {len(tasks)} cached tasks.\n')
    if not tasks:
        tasks = call_gpt(parent['title'], parent['body'])
        if tasks:
            save_cached_tasks(pnum, tasks)
    if not tasks:
        sys.stderr.write('  No tasks generated.\n')
        return
    created_numbers: List[int] = []
    created = 0
    skipped = 0
    for task in tasks:
        sub_title = f'{STORY_PREFIX}{pnum} – {task["title"]}' if not sub_issue_title_has_prefix(
            task['title'], pnum) else task['title']
        if sub_title in existing_map:
            skipped += 1
            continue
        body_lines = [
            f'Derived from parent Story #{pnum}: {parent["title"]}',
            f'PARENT-STORY: #{pnum}',
            '',
            task['description'] or '(no description provided)'
        ]
        body = '\n'.join(body_lines).rstrip() + '\n'
        try:
            issue_json = create_issue(sub_title, body)
            if not DRY_RUN:
                node_id = fetch_issue_node_id(issue_json['number'])
                add_to_project(project_id, node_id)
                created_numbers.append(issue_json['number'])
            created += 1
            sys.stderr.write(f'  Created sub-issue: {sub_title}\n')
            if not DRY_RUN:
                time.sleep(RATE_DELAY)
        except Exception as e:
            sys.stderr.write(
                f'  ERROR creating sub-issue "{sub_title}": {e}\n')
    sys.stderr.write(
        f'  Summary: created={created} skipped(existing)={skipped}\n')
    try:
        append_checklist(pnum, created_numbers)
    except Exception as e:
        sys.stderr.write(f'  WARNING checklist update failed: {e}\n')


def sub_issue_title_has_prefix(title: str, parent_number: int) -> bool:
    return title.startswith(f'Story #{parent_number} – ')


def main():
    try:
        project_id = get_project_id()
    except Exception as e:
        sys.stderr.write(f'ERROR resolving project id: {e}\n')
        sys.exit(2)
    try:
        parents = fetch_project_issue_items()
    except Exception as e:
        sys.stderr.write(f'ERROR fetching project items: {e}\n')
        sys.exit(2)
    try:
        repo_issues = list_repo_issues()
    except Exception as e:
        sys.stderr.write(f'ERROR listing repo issues: {e}\n')
        sys.exit(2)

    sys.stderr.write(
        f'Parents discovered: {len(parents)} (DRY_RUN={DRY_RUN})\n')
    processed = 0
    for parent in parents:
        process_parent(project_id, parent, repo_issues)
        if MAX_PARENTS and (processed := processed + 1) >= MAX_PARENTS:
            sys.stderr.write('Reached MAX_PARENTS limit; stopping.\n')
            break
        # Refresh repo issues cache only if any creation happened (simple approach)
        if not DRY_RUN:
            repo_issues = list_repo_issues()
    sys.stderr.write('Done.\n')


if __name__ == '__main__':
    main()
