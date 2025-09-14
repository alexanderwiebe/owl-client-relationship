"""Step 3: Create & link sub-issues (idempotent) for a parent story issue.

Pipeline example:
  python story-building/step1_fetch_project_issues.py | \
    ISSUE_NUMBER=1 python story-building/step2_decompose_single_issue.py | \
    ISSUE_NUMBER=1 python story-building/step3_create_subissues.py

Inputs:
  - STDIN: JSON array of task objects: [{"title": str, "description": str}, ...]
  - ISSUE_NUMBER env or first CLI arg: parent issue number

Behavior:
  - Determines project (user project v2) and ensures new sub-issues get added.
  - Sub-issue title pattern: "Story #<parent> – <Task Title>" (en dash)
  - Skips creation if an issue with identical title already exists (idempotent)
  - Appends new entries to parent issue body as checklist items under '### Sub-issues'
  - Will not duplicate checklist lines for already-linked children
  - Leaves existing sub-issues that are no longer in the tasks JSON (non-destructive)

Environment Variables:
  GITHUB_TOKEN / GITHUB_TOKEN_FG  (required)
  ISSUE_NUMBER                    (parent issue number if not CLI)
  USERNAME                        (override project owner, default config)
  PROJECT_NUMBER                  (override project number, default config)
  DRY_RUN=1                       (print intended actions only)
  RATE_DELAY=0.3                  (seconds between write calls; default 0.3)

Exit codes:
  0 success, 1 config / input error, 2 runtime API error
"""

from __future__ import annotations
import os
import sys
import json
import time
import re
from pathlib import Path
import requests
from typing import List, Dict, Any, Set
from dotenv import load_dotenv

# ------------- Config -------------
DEFAULT_USERNAME = "alexanderwiebe"
DEFAULT_PROJECT_NUMBER = 1
# keep consistent with earlier steps
ISSUE_TITLE_PREFIX_TEMPLATE = "Story #{parent} – {title}"

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN_FG")
if not TOKEN:
    sys.stderr.write("ERROR: GITHUB_TOKEN / GITHUB_TOKEN_FG not set.\n")
    sys.exit(1)

USERNAME = os.getenv("USERNAME", DEFAULT_USERNAME)
try:
    PROJECT_NUMBER = int(
        os.getenv("PROJECT_NUMBER", str(DEFAULT_PROJECT_NUMBER)))
except ValueError:
    sys.stderr.write("ERROR: PROJECT_NUMBER must be int.\n")
    sys.exit(1)

RATE_DELAY = float(os.getenv("RATE_DELAY", "0.3"))
DRY_RUN = os.getenv("DRY_RUN") == "1"

# adapt if repo differs from username naming
REPO_NAME = f"{USERNAME}/owl-client-relationship"
REST_HEADERS = {"Authorization": f"token {TOKEN}",
                "Accept": "application/vnd.github+json"}
GQL_ENDPOINT = "https://api.github.com/graphql"
GQL_HEADERS = {"Authorization": f"bearer {TOKEN}",
               "Accept": "application/vnd.github+json"}


def read_tasks_from_stdin() -> List[Dict[str, str]]:
    if sys.stdin.isatty():
        sys.stderr.write(
            "ERROR: No tasks JSON provided on stdin. Pipe from Step 2.\n")
        sys.exit(1)
    raw = sys.stdin.read().strip()
    if not raw:
        sys.stderr.write("ERROR: Empty stdin.\n")
        sys.exit(1)
    try:
        data = json.loads(raw)
    except Exception:
        sys.stderr.write("ERROR: Stdin not valid JSON array.\n")
        sys.exit(1)
    if not isinstance(data, list):
        sys.stderr.write("ERROR: Expected JSON array for tasks.\n")
        sys.exit(1)
    tasks: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        desc = (item.get("description") or "").strip()
        tasks.append({"title": title, "description": desc})
    if not tasks:
        sys.stderr.write("ERROR: No valid tasks with titles found.\n")
        sys.exit(1)
    return tasks


def resolve_parent_issue_number() -> int:
    # CLI arg overrides env
    if len(sys.argv) > 1:
        try:
            return int(sys.argv[1])
        except ValueError:
            sys.stderr.write(
                "ERROR: First argument must be an integer parent issue number.\n")
            sys.exit(1)
    issue_env = os.getenv("ISSUE_NUMBER")
    if issue_env:
        try:
            return int(issue_env)
        except ValueError:
            sys.stderr.write("ERROR: ISSUE_NUMBER env must be integer.\n")
            sys.exit(1)
    sys.stderr.write(
        "ERROR: Provide parent ISSUE_NUMBER (env or first arg).\n")
    sys.exit(1)


def gql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(GQL_ENDPOINT, json={
                      "query": query, "variables": variables}, headers=GQL_HEADERS)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f"Non-JSON GraphQL response {r.status_code}: {r.text[:200]}")
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


def get_project_id() -> str:
    q = """
    query($login:String!, $num:Int!){ user(login:$login){ projectV2(number:$num){ id } } }
    """
    d = gql(q, {"login": USERNAME, "num": PROJECT_NUMBER})
    proj = d["data"]["user"].get("projectV2")
    if not proj:
        raise RuntimeError(
            f"Project #{PROJECT_NUMBER} not found for user {USERNAME}")
    return proj["id"]


def list_repo_issues() -> List[Dict[str, Any]]:
    # Use REST pagination
    issues: List[Dict[str, Any]] = []
    page = 1
    while True:
        r = requests.get(f"https://api.github.com/repos/{REPO_NAME}/issues", params={
                         "state": "all", "per_page": 100, "page": page}, headers=REST_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(
                f"Issues fetch failed page {page}: {r.status_code} {r.text[:120]}")
        batch = r.json()
        if not batch:
            break
        for it in batch:
            if "pull_request" in it:  # skip PRs
                continue
            issues.append(it)
        page += 1
    return issues


def get_parent_issue(parent_number: int, issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    for it in issues:
        if it["number"] == parent_number:
            return it
    raise RuntimeError(f"Parent issue #{parent_number} not found in repo.")


def existing_subissue_titles(parent_number: int, issues: List[Dict[str, Any]]) -> Dict[str, int]:
    """Map existing subissue title -> issue number (pattern match)."""
    pattern = re.compile(rf"^Story #{parent_number} – ")
    mapping: Dict[str, int] = {}
    for it in issues:
        title = it.get("title", "")
        if pattern.match(title):
            mapping[title] = it["number"]
    return mapping


def create_issue(title: str, body: str) -> Dict[str, Any]:
    if DRY_RUN:
        return {"number": -1, "title": title, "body": body}
    r = requests.post(f"https://api.github.com/repos/{REPO_NAME}/issues",
                      headers=REST_HEADERS, json={"title": title, "body": body}, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Create issue failed: {r.status_code} {r.text[:200]}")
    return r.json()


def add_issue_to_project(project_id: str, node_id: str):
    if DRY_RUN:
        return
    m = """
    mutation($pid:ID!, $cid:ID!){ addProjectV2ItemById(input:{projectId:$pid, contentId:$cid}) { item { id } } }
    """
    gql(m, {"pid": project_id, "cid": node_id})


def fetch_issue_node_id(number: int) -> str:
    q = """
    query($owner:String!, $repo:String!, $number:Int!){ repository(owner:$owner,name:$repo){ issue(number:$number){ id } } }
    """
    owner, repo = REPO_NAME.split("/")
    d = gql(q, {"owner": owner, "repo": repo, "number": number})
    issue_obj = d["data"]["repository"].get("issue")
    if not issue_obj:
        raise RuntimeError(f"Cannot resolve node id for issue #{number}")
    return issue_obj["id"]


def append_checklist(parent_number: int, new_child_numbers: List[int]):
    if not new_child_numbers:
        return
    # Fetch parent (REST)
    r = requests.get(
        f"https://api.github.com/repos/{REPO_NAME}/issues/{parent_number}", headers=REST_HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(
            f"Parent fetch failed: {r.status_code} {r.text[:120]}")
    parent = r.json()
    body = parent.get("body") or ""
    existing_nums = extract_existing_child_numbers(body)
    add_nums = [n for n in new_child_numbers if n not in existing_nums]
    if not add_nums:
        return
    lines = body.rstrip().splitlines()
    if lines and lines[-1].strip() != "":
        lines.append("")
    if not any(l.strip().lower().startswith("### sub-issues") for l in lines):
        lines.append("### Sub-issues")
    # fetch child titles for each
    for n in add_nums:
        if DRY_RUN:
            title = "(DRY_RUN)"
        else:
            cr = requests.get(
                f"https://api.github.com/repos/{REPO_NAME}/issues/{n}", headers=REST_HEADERS, timeout=30)
            if cr.status_code != 200:
                title = "(title unavailable)"
            else:
                title = cr.json().get("title", "(no title)")
        lines.append(f"- [ ] #{n} — {title}")
    new_body = "\n".join(lines).rstrip() + "\n"
    if DRY_RUN:
        sys.stderr.write(
            f"DRY_RUN: would update parent #{parent_number} body with {len(add_nums)} checklist lines.\n")
        return
    er = requests.patch(f"https://api.github.com/repos/{REPO_NAME}/issues/{parent_number}",
                        headers=REST_HEADERS, json={"body": new_body}, timeout=30)
    if er.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to update parent checklist: {er.status_code} {er.text[:120]}")


def extract_existing_child_numbers(parent_body: str) -> Set[int]:
    pat = re.compile(r"^- \[.\] #(?P<num>\d+)", re.MULTILINE)
    nums: Set[int] = set()
    for m in pat.finditer(parent_body or ""):
        try:
            nums.add(int(m.group("num")))
        except ValueError:
            pass
    return nums


def main():
    parent_issue_number = resolve_parent_issue_number()
    tasks = read_tasks_from_stdin()
    sys.stderr.write(f"Parent issue: #{parent_issue_number}\n")
    sys.stderr.write(f"Incoming tasks: {len(tasks)}\n")

    try:
        all_issues = list_repo_issues()
    except Exception as e:
        sys.stderr.write(f"ERROR fetching repo issues: {e}\n")
        sys.exit(2)

    try:
        parent_issue = get_parent_issue(parent_issue_number, all_issues)
    except Exception as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(2)

    existing_map = existing_subissue_titles(parent_issue_number, all_issues)
    sys.stderr.write(f"Existing sub-issues detected: {len(existing_map)}\n")

    # Determine project id for adding new issues
    try:
        project_id = get_project_id()
    except Exception as e:
        sys.stderr.write(f"ERROR resolving project id: {e}\n")
        sys.exit(2)

    new_child_numbers: List[int] = []
    created = 0
    skipped = 0
    for task in tasks:
        raw_title = task["title"].strip()
        issue_title = ISSUE_TITLE_PREFIX_TEMPLATE.format(
            parent=parent_issue_number, title=raw_title)
        if issue_title in existing_map:
            skipped += 1
            continue
        body_lines = [
            f"Derived from parent Story #{parent_issue_number}: {parent_issue.get('title')}",
            f"PARENT-STORY: #{parent_issue_number}",
            "",
            task["description"] or "(no description provided)"
        ]
        body = "\n".join(body_lines).rstrip() + "\n"
        try:
            issue_json = create_issue(issue_title, body)
            number_created = issue_json["number"] if not DRY_RUN else -1
            # fetch node id (GraphQL) only if real
            if not DRY_RUN:
                node_id = fetch_issue_node_id(number_created)
                add_issue_to_project(project_id, node_id)
                new_child_numbers.append(number_created)
            created += 1
            sys.stderr.write(f"Created sub-issue: {issue_title}\n")
            if not DRY_RUN:
                time.sleep(RATE_DELAY)
        except Exception as e:
            sys.stderr.write(
                f"ERROR creating sub-issue '{issue_title}': {e}\n")
    sys.stderr.write(
        f"Summary: created {created}, skipped {skipped} (already exist).\n")

    # Update parent checklist
    try:
        append_checklist(parent_issue_number, new_child_numbers)
    except Exception as e:
        sys.stderr.write(f"WARNING: checklist update failed: {e}\n")

    sys.stderr.write("Done.\n")


if __name__ == "__main__":
    main()
