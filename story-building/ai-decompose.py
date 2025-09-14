"""Automated story decomposition:

Iterates over existing "Story" issues already added to a User Project (Project V2)
and generates subtask issues (one per GPT-produced task) if they do not yet exist.

Detection rules / assumptions:
 - A story issue has a title beginning with STORY_PREFIX (default: "Story:")
 - Generated subtask issues are titled: "Story #<parentNumber> – <Task Title>" (en dash)
 - Idempotent: before creating a subtask we search for an existing issue with that exact title
 - Each new subtask body contains a marker line: PARENT-STORY: #<parentNumber>
 - All new issues are added to the same user Project V2 as the parent story

Environment:
  OPENAI_API_KEY   (required for GPT decomposition)
  GITHUB_TOKEN     (required for GitHub REST & GraphQL)
  STORY_REGEX      (optional override, Python regex applied to title)
  DRY_RUN=1        (preview actions without creating anything)

Configuration constants below can be edited locally.
"""

from __future__ import annotations
import os
import re
import json
import time
import requests
from typing import List, Dict, Any, Iterable, Set
from dotenv import load_dotenv
from github import Github, Auth

import openai

# ---- CONFIG ----
# GitHub username owning the user project
USERNAME = "alexanderwiebe"
REPO_NAME = "alexanderwiebe/owl-client-relationship"
PROJECT_NUMBER = 1                           # User project number (see URL)
STORY_PREFIX = "Story:"                      # Story title prefix
MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")
MAX_TOKENS = 600
TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
RATE_DELAY = 0.4                             # seconds between GitHub write calls

# ---- ENV ----
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN_FG")
DRY_RUN = os.getenv("DRY_RUN") == "1"
STORY_REGEX = os.getenv("STORY_REGEX")  # optional custom pattern
# treat every issue in project as story
ALL_AS_STORIES = os.getenv("ALL_AS_STORIES") == "1"
# treat every repo issue (not PR, not child) as story
ALL_REPO_ISSUES = os.getenv("ALL_REPO_ISSUES") == "1"
DEBUG = os.getenv("DEBUG") == "1"

if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY not set")
if not GITHUB_TOKEN:
    raise SystemExit("GITHUB_TOKEN (or GITHUB_TOKEN_FG) not set")

openai.api_key = OPENAI_API_KEY

GH_API_GRAPHQL = "https://api.github.com/graphql"
HEADERS_GQL = {"Authorization": f"bearer {GITHUB_TOKEN}"}
HEADERS_REST = {"Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json"}

auth = Auth.Token(GITHUB_TOKEN)
gh = Github(auth=auth)
repo = gh.get_repo(REPO_NAME)


# ---- GraphQL helpers (reuse style from create-tickets-2) ----
def gql(query: str, variables: Dict[str, Any] | None = None) -> Dict[str, Any]:
    r = requests.post(GH_API_GRAPHQL, json={
                      "query": query, "variables": variables or {}}, headers=HEADERS_GQL)
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


def get_project_id(user: str, number: int) -> str:
    q = """
    query($login:String!, $number:Int!) {
      user(login:$login) { projectV2(number:$number) { id } }
    }
    """
    data = gql(q, {"login": user, "number": number})
    try:
        return data["data"]["user"]["projectV2"]["id"]
    except Exception:
        raise RuntimeError(f"Could not resolve project id: {data}")


def list_project_story_issues(project_id: str) -> List[Dict[str, Any]]:
    """Return list of story issue dicts with keys: number, node_id, title, body.

    Selection logic (first match wins):
        1. If ALL_AS_STORIES=1 -> every issue item is returned
        2. Else if STORY_REGEX set -> titles matching regex
        3. Else titles starting with STORY_PREFIX
    """
    issues: List[Dict[str, Any]] = []
    cursor = None
    q = """
        query($pid:ID!, $cursor:String) {
            node(id:$pid) {
                ... on ProjectV2 {
                    items(first:100, after:$cursor) {
                        pageInfo { hasNextPage endCursor }
                        nodes {
                            content { __typename ... on Issue { id number title body } }
                        }
                    }
                }
            }
        }
        """
    pattern = re.compile(STORY_REGEX) if STORY_REGEX else None
    scanned_titles: List[str] = []
    while True:
        data = gql(q, {"pid": project_id, "cursor": cursor})
        items = data["data"]["node"]["items"]
        for node in items["nodes"]:
            content = node.get("content")
            if not content or content.get("__typename") != "Issue":
                continue
            title: str = content["title"]
            scanned_titles.append(title)
            if not ALL_AS_STORIES:
                if pattern:
                    if not pattern.search(title):
                        continue
                else:
                    if not title.startswith(STORY_PREFIX):
                        continue
            issues.append({
                "number": content["number"],
                "node_id": content["id"],
                "title": title,
                "body": content.get("body", "")
            })
        if not items["pageInfo"]["hasNextPage"]:
            break
        cursor = items["pageInfo"]["endCursor"]
    if DEBUG:
        print(f"DEBUG: scanned {len(scanned_titles)} project issue titles")
        if not issues:
            for t in scanned_titles:
                print(f"DEBUG title (no match): {t}")
    return issues


def list_project_issue_node_ids(project_id: str) -> Set[str]:
    """Return node_ids of issues already in the project (for quick membership checks)."""
    existing: Set[str] = set()
    cursor = None
    q = """
    query($pid:ID!, $cursor:String) {
      node(id:$pid) { ... on ProjectV2 { items(first:100, after:$cursor) { pageInfo { hasNextPage endCursor } nodes { content { __typename ... on Issue { id } } } } } }
    }
    """
    while True:
        data = gql(q, {"pid": project_id, "cursor": cursor})
        items = data["data"]["node"]["items"]
        for node in items["nodes"]:
            content = node.get("content")
            if content and content.get("__typename") == "Issue":
                existing.add(content["id"])
        if not items["pageInfo"]["hasNextPage"]:
            break
        cursor = items["pageInfo"]["endCursor"]
    return existing


def list_repo_issues_as_stories() -> List[Dict[str, Any]]:
    """Treat all repo issues (excluding PRs and already-generated child tasks) as stories.

    Skip issues that look like generated subtasks (title pattern 'Story #N – ...' or body containing 'PARENT-STORY:').
    """
    stories: List[Dict[str, Any]] = []
    child_title_re = re.compile(r"^Story #\d+ – ")
    for issue in repo.get_issues(state="all"):
        if issue.pull_request is not None:
            continue
        title = issue.title
        body = issue.body or ""
        if child_title_re.match(title) or "PARENT-STORY:" in body:
            continue  # skip already generated child tasks
        stories.append({
            "number": issue.number,
            "node_id": issue.raw_data["node_id"],
            "title": title,
            "body": body,
        })
    if DEBUG:
        print(f"DEBUG: repo issues considered stories: {len(stories)}")
    return stories


def existing_issue_titles() -> Set[str]:
    titles = set()
    for issue in repo.get_issues(state="all"):
        if issue.pull_request is None:
            titles.add(issue.title)
    return titles


def create_issue(title: str, body: str) -> Dict[str, Any]:
    if DRY_RUN:
        print(f"DRY_RUN create issue: {title}")
        return {"number": -1, "node_id": "DRY_RUN"}
    r = requests.post(f"https://api.github.com/repos/{REPO_NAME}/issues",
                      headers=HEADERS_REST, json={"title": title, "body": body})
    r.raise_for_status()
    data = r.json()
    return {"number": data["number"], "node_id": data["node_id"]}


def add_to_project(project_id: str, node_id: str):
    if DRY_RUN:
        print(f"DRY_RUN add to project: {node_id}")
        return
    mutation = """
    mutation($pid:ID!, $cid:ID!) { addProjectV2ItemById(input:{projectId:$pid, contentId:$cid}) { item { id } } }
    """
    gql(mutation, {"pid": project_id, "cid": node_id})


def decompose_story_with_gpt(story_text: str) -> List[Dict[str, str]]:
    prompt = (
        "Break down the following user story into 5-10 concise implementation tasks. "
        "Each task MUST be a JSON object with keys 'title' and 'description'. "
        "Return ONLY a JSON array, no prose. Keep titles <= 70 chars.\n\n" + story_text
    )
    resp = openai.ChatCompletion.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS
    )
    content = resp.choices[0].message.content.strip()
    # Extract first JSON array
    match = re.search(r"(\[.*\])", content, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
        if isinstance(data, list):
            # ensure structure
            cleaned = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get(
                    "name") or "Untitled Task"
                desc = item.get("description") or ""
                cleaned.append(
                    {"title": title.strip(), "description": desc.strip()})
            return cleaned
    except Exception:
        return []
    return []


def extract_existing_child_numbers(parent_body: str) -> Set[int]:
    """Parse existing task list style child issue references (- [ ] #123)."""
    nums: Set[int] = set()
    pattern = re.compile(r"^- \[.\] #(?P<num>\d+)", re.MULTILINE)
    for m in pattern.finditer(parent_body or ""):
        try:
            nums.add(int(m.group("num")))
        except ValueError:
            continue
    return nums


def append_tasklist_to_parent(parent_issue_number: int, child_numbers: List[int]):
    if not child_numbers:
        return
    issue = repo.get_issue(parent_issue_number)
    body = issue.body or ""
    existing_children = extract_existing_child_numbers(body)
    new_children = [n for n in child_numbers if n not in existing_children]
    if not new_children:
        return
    lines = body.rstrip().splitlines()
    if lines and lines[-1].strip() != "":
        lines.append("")  # blank separator
    lines.append("### Sub-issues") if not any(l.strip().lower().startswith("### sub-issues")
                                              for l in lines) else None
    for num in new_children:
        # Fetch child title for nicer list entry
        child_issue = repo.get_issue(num)
        lines.append(f"- [ ] #{num} — {child_issue.title}")
    new_body = "\n".join(lines).rstrip() + "\n"
    if DRY_RUN:
        print(
            f"DRY_RUN update parent #{parent_issue_number} with {len(new_children)} task list entries")
        return
    issue.edit(body=new_body)


def main():
    print(f"Repo: {REPO_NAME}  Project#: {PROJECT_NUMBER}  DRY_RUN={DRY_RUN}")
    project_id = get_project_id(USERNAME, PROJECT_NUMBER)
    if ALL_REPO_ISSUES:
        stories = list_repo_issues_as_stories()
    else:
        stories = list_project_story_issues(project_id)
    print(f"Found {len(stories)} story issues (mode: {'ALL_REPO_ISSUES' if ALL_REPO_ISSUES else 'PROJECT'}; ALL_AS_STORIES={ALL_AS_STORIES}, STORY_REGEX={'set' if STORY_REGEX else 'unset'})")

    project_issue_nodes = list_project_issue_node_ids(project_id)

    existing_titles = existing_issue_titles()

    for story in stories:
        story_num = story["number"]
        if f"PARENT-STORY: #{story_num}" in story["body"]:
            # Already processed marker (if user added marker manually to parent skip) – optional
            pass
        print(f"→ Processing Story #{story_num}: {story['title']}")
        # Ensure parent issue is on project if using ALL_REPO_ISSUES mode and absent
        if ALL_REPO_ISSUES and story["node_id"] not in project_issue_nodes:
            try:
                add_to_project(project_id, story["node_id"])
                project_issue_nodes.add(story["node_id"])
                print("  Added parent to project")
            except Exception as e:
                print(f"  Warning: failed to add parent to project: {e}")
        tasks = decompose_story_with_gpt(story["body"] or story["title"])
        if not tasks:
            print("  (no tasks produced)")
            continue
        created = 0
        skipped = 0
        created_child_numbers: List[int] = []
        for t in tasks:
            sub_title = f"Story #{story_num} – {t['title']}"
            if sub_title in existing_titles:
                skipped += 1
                continue
            body = (
                f"Derived from Story #{story_num}: {story['title']}\n\n"
                f"PARENT-STORY: #{story_num}\n\n"
                f"{t['description']}".strip()
            )
            issue = create_issue(sub_title, body)
            add_to_project(project_id, issue["node_id"])
            existing_titles.add(sub_title)
            created += 1
            if issue["number"] != -1:
                created_child_numbers.append(issue["number"])
            if not DRY_RUN:
                time.sleep(RATE_DELAY)
        print(f"  Tasks created: {created}, skipped(existing): {skipped}")
        # Link back via task list in parent
        try:
            append_tasklist_to_parent(story_num, created_child_numbers)
        except Exception as e:
            print(f"  Warning: failed to update parent task list: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
