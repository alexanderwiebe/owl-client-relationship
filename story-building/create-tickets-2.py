"""Utility script: Add ALL existing issues in the repository to a User Project (Project v2).

Features:
 - Reads token from env var GITHUB_TOKEN (no hard-coded secrets)
 - Fetches project ID (user project) by number
 - Paginates through project items to avoid duplicate additions
 - Paginates through repo issues (open by default; can include closed)
 - Optional DRY_RUN mode
 - Idempotent: skips issues already in project

Usage:
  export GITHUB_TOKEN=ghp_xxx
  python create-tickets-2.py              # add open issues
  INCLUDE_CLOSED=1 python create-tickets-2.py   # include closed issues
  DRY_RUN=1 python create-tickets-2.py    # show actions only
"""

from __future__ import annotations
import os
from dotenv import load_dotenv  # load .env automatically
import sys
import time
from typing import Iterable, Set, Dict, Any
import requests
from github import Github

USERNAME = "alexanderwiebe"          # GitHub username owning the project
REPO_NAME = "alexanderwiebe/owl-client-relationship"
PROJECT_NUMBER = 1                    # User project number (as seen in URL)
GH_API = "https://api.github.com/graphql"

# Load .env (silent if file missing)
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

TOKEN = os.getenv("GITHUB_TOKEN_FG")
if not TOKEN:
    sys.stderr.write("ERROR: GITHUB_TOKEN_FG environment variable not set.\n")
    sys.exit(1)

INCLUDE_CLOSED = os.getenv("INCLUDE_CLOSED") == "1"
DRY_RUN = os.getenv("DRY_RUN") == "1"
PAGE_SIZE = 100

headers = {"Authorization": f"bearer {TOKEN}"}
gh = Github(TOKEN)
repo = gh.get_repo(REPO_NAME)


def gql(query: str, variables: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = {"query": query, "variables": variables or {}}
    r = requests.post(GH_API, json=payload, headers=headers)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response: {r.text}")
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


def get_project_id(user: str, number: int) -> str:
    q = """
  query($login:String!, $number:Int!) {
    user(login:$login) {
    projectV2(number:$number) { id }
    }
  }
  """
    data = gql(q, {"login": user, "number": number})
    try:
        return data["data"]["user"]["projectV2"]["id"]
    except Exception:
        raise RuntimeError(f"Cannot resolve project id in response: {data}")


def list_project_issue_node_ids(project_id: str) -> Set[str]:
    existing: Set[str] = set()
    cursor = None
    q = """
  query($pid:ID!, $cursor:String) {
    node(id:$pid) {
    ... on ProjectV2 {
      items(first: 100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        content { __typename ... on Issue { id number title } }
      }
      }
    }
    }
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


def add_issue_to_project(project_id: str, issue_node_id: str):
    mutation = """
  mutation($pid:ID!, $cid:ID!){
    addProjectV2ItemById(input:{projectId:$pid, contentId:$cid}) { item { id } }
  }
  """
    if DRY_RUN:
        print(f"DRY_RUN: would add issue node {issue_node_id} to project")
        return
    gql(mutation, {"pid": project_id, "cid": issue_node_id})


def iter_issue_node_ids(include_closed: bool) -> Iterable[Dict[str, Any]]:
    state = "all" if include_closed else "open"
    # PyGithub paginates automatically; we still stream through
    for issue in repo.get_issues(state=state):
        # skip pull requests (they appear in issues list)
        if issue.pull_request is not None:
            continue
        yield {
            "number": issue.number,
            "title": issue.title,
            "node_id": issue.raw_data["node_id"],
            "state": issue.state,
        }


def main():
    print(f"Repo: {REPO_NAME}")
    print(f"Project number: {PROJECT_NUMBER} (user: {USERNAME})")
    print(f"Include closed: {INCLUDE_CLOSED}  Dry-run: {DRY_RUN}")

    project_id = get_project_id(USERNAME, PROJECT_NUMBER)
    print(f"✅ Project ID: {project_id}")

    existing = list_project_issue_node_ids(project_id)
    print(f"Already in project: {len(existing)} issues")

    added = 0
    skipped = 0
    for info in iter_issue_node_ids(INCLUDE_CLOSED):
        node_id = info["node_id"]
        if node_id in existing:
            skipped += 1
            continue
        try:
            add_issue_to_project(project_id, node_id)
            added += 1
            print(f"➕ Added #{info['number']} - {info['title']}")
            # gentle rate pacing
            if not DRY_RUN:
                time.sleep(0.3)
        except Exception as e:
            print(f"✗ Failed adding #{info['number']}: {e}")
    print(f"Done. Added {added}, skipped {skipped} (already present).")


if __name__ == "__main__":
    main()
