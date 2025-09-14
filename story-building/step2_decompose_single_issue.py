"""Step 2: Decompose a single Project issue into sub-issue task JSON using GPT.

This script:
 1. Either reads issues JSON from stdin (piped from step1) OR fetches from project if stdin empty
 2. Selects one issue (by ISSUE_NUMBER env / CLI arg) or the first if not provided
 3. Sends its title+body to the OpenAI model to get structured tasks
 4. Prints a JSON array of tasks to stdout (no GitHub writes yet)

Environment:
  GITHUB_TOKEN or GITHUB_TOKEN_FG   (read access to project & repo)
  OPENAI_API_KEY                    (model access)
  ISSUE_NUMBER (optional)           (select specific issue)
  MODEL (optional, default gpt-3.5-turbo)
  TEMPERATURE (optional, default 0.2)
  MAX_TASKS (optional int cap; default 12)
  PRETTY_JSON=1 (pretty print)

Usage:
  # automatic fetch (no pipe)
  python story-building/step2_decompose_single_issue.py
  ISSUE_NUMBER=42 python story-building/step2_decompose_single_issue.py

  # pipe from step1 to avoid a second API call:
  python story-building/step1_fetch_project_issues.py | \
      python story-building/step2_decompose_single_issue.py
  python story-building/step1_fetch_project_issues.py | \
      ISSUE_NUMBER=42 python story-building/step2_decompose_single_issue.py

You can also pass the issue number as the first CLI arg:
  python story-building/step2_decompose_single_issue.py 42

Output: JSON array such as:
  [ {"title": "Create X", "description": "..."}, ... ]

Proceed to Step 3 only after you verify the tasks look good.
"""

from __future__ import annotations
import os
import sys
import json
from pathlib import Path
import requests
import re
from typing import List, Dict, Any
from dotenv import load_dotenv
import openai  # fallback for legacy; primary path uses OpenAI client
try:
    from openai import OpenAI
    _USE_NEW_CLIENT = True
except ImportError:  # very old openai package
    OpenAI = None
    _USE_NEW_CLIENT = False

# ---------------- Config -----------------
USERNAME = "alexanderwiebe"   # user project owner
PROJECT_NUMBER = 1             # project number
GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
DEFAULT_MODEL = os.getenv("MODEL", "gpt-3.5-turbo")
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.2"))
MAX_TASKS = int(os.getenv("MAX_TASKS", "12"))

# --------------- Env / Tokens ------------
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN_FG")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not TOKEN:
    sys.stderr.write("ERROR: Missing GITHUB_TOKEN / GITHUB_TOKEN_FG\n")
    sys.exit(1)
if not OPENAI_KEY:
    sys.stderr.write("ERROR: Missing OPENAI_API_KEY\n")
    sys.exit(1)
if _USE_NEW_CLIENT and OpenAI is not None:
    client = OpenAI(api_key=OPENAI_KEY)
else:
    openai.api_key = OPENAI_KEY

HEADERS = {"Authorization": f"bearer {TOKEN}",
           "Accept": "application/vnd.github+json"}

# --------------- GraphQL Query -----------
QUERY = """
query($login:String!, $number:Int!, $after:String){
  user(login:$login){
    projectV2(number:$number){
      id
      items(first:100, after:$after){
        pageInfo { hasNextPage endCursor }
        nodes { content { __typename ... on Issue { id number title body url state } } }
      }
    }
  }
}
"""


def fetch_all_project_issues() -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    cursor = None
    while True:
        resp = requests.post(
            GRAPHQL_ENDPOINT,
            json={"query": QUERY, "variables": {"login": USERNAME,
                                                "number": PROJECT_NUMBER, "after": cursor}},
            headers=HEADERS,
            timeout=30,
        )
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Non-JSON response: {resp.status_code} {resp.text[:200]}")
        if 'errors' in data:
            raise RuntimeError(json.dumps(data['errors'], indent=2))
        user = data.get('data', {}).get('user')
        if not user:
            raise RuntimeError(
                "User null – check permissions (need project access)")
        proj = user.get('projectV2')
        if not proj:
            raise RuntimeError(f"Project {PROJECT_NUMBER} not found")
        items = proj['items']
        for node in items['nodes']:
            c = node.get('content')
            if not c or c.get('__typename') != 'Issue':
                continue
            issues.append({
                'number': c['number'],
                'title': c['title'],
                'body': c.get('body') or '',
                'url': c.get('url'),
                'state': c.get('state')
            })
        if not items['pageInfo']['hasNextPage']:
            break
        cursor = items['pageInfo']['endCursor']
    return issues


def choose_issue(issues: List[Dict[str, Any]], issue_number: int | None) -> Dict[str, Any]:
    if issue_number is not None:
        for i in issues:
            if i['number'] == issue_number:
                return i
        raise SystemExit(f"Issue #{issue_number} not found in project")
    if not issues:
        raise SystemExit("No issues in project")
    return issues[0]


def call_gpt_for_tasks(title: str, body: str) -> List[Dict[str, str]]:
    story_text = f"TITLE: {title}\n\nBODY:\n{body}".strip()
    prompt = (
        "You are an assistant splitting a GitHub issue (user story) into actionable development tasks. "
        "Return ONLY a JSON array, no commentary. Each element MUST have 'title' and 'description'. "
        f"Limit to at most {MAX_TASKS} tasks. Titles <= 70 chars. Avoid duplicates.\n\n" + story_text
    )
    if _USE_NEW_CLIENT and OpenAI is not None:
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=TEMPERATURE,
            max_tokens=700,
        )
        content = resp.choices[0].message.content.strip()
    else:  # legacy path for pinned <1.0 openai lib
        resp = openai.ChatCompletion.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=TEMPERATURE,
            max_tokens=700,
        )
        content = resp.choices[0].message.content.strip()
    match = re.search(r"(\[.*\])", content, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except Exception:
        return []
    tasks: List[Dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                t = (item.get('title') or item.get('name') or '').strip()
                d = (item.get('description') or '').strip()
                if t:
                    tasks.append({'title': t, 'description': d})
    return tasks[:MAX_TASKS]


def read_issues_from_stdin() -> List[Dict[str, Any]]:
    if sys.stdin.isatty():  # nothing piped
        return []
    raw = sys.stdin.read().strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        sys.stderr.write(
            "WARNING: stdin provided but not valid JSON; ignoring stdin.\n")
        return []
    if isinstance(data, list):
        # minimal structure check
        filtered = []
        for entry in data:
            if isinstance(entry, dict) and 'number' in entry and 'title' in entry:
                filtered.append({
                    'number': entry['number'],
                    'title': entry['title'],
                    'body': entry.get('body', ''),
                    'url': entry.get('url'),
                    'state': entry.get('state', 'UNKNOWN')
                })
        return filtered
    return []


def main():
    # Determine issue number from env or arg
    issue_number = None
    if len(sys.argv) > 1:
        try:
            issue_number = int(sys.argv[1])
        except ValueError:
            sys.stderr.write(
                "First argument must be an integer issue number\n")
            sys.exit(1)
    elif os.getenv('ISSUE_NUMBER'):
        try:
            issue_number = int(os.getenv('ISSUE_NUMBER'))
        except ValueError:
            sys.stderr.write("ISSUE_NUMBER env must be integer\n")
            sys.exit(1)

    issues = read_issues_from_stdin()
    if not issues:  # fallback to live fetch
        try:
            issues = fetch_all_project_issues()
        except Exception as e:
            sys.stderr.write(f"ERROR fetching issues: {e}\n")
            sys.exit(1)

    issue = choose_issue(issues, issue_number)
    sys.stderr.write(f"Selected issue #{issue['number']}: {issue['title']}\n")

    tasks = call_gpt_for_tasks(issue['title'], issue['body'])
    if os.getenv('PRETTY_JSON') == '1':
        print(json.dumps(tasks, indent=2))
    else:
        print(json.dumps(tasks, separators=(',', ':')))
    sys.stderr.write(f"Generated {len(tasks)} tasks.\n")


if __name__ == '__main__':
    main()
