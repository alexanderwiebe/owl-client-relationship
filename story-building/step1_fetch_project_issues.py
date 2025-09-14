"""Step 1: Fetch all Issue items from a User Project (Project v2) and output JSON.

Usage:
  python story-building/step1_fetch_project_issues.py              # prints JSON array to stdout
  python story-building/step1_fetch_project_issues.py > issues.json  # save to file

Environment:
  GITHUB_TOKEN or GITHUB_TOKEN_FG  (must have project read access)

This script ONLY does retrieval (no GPT, no writes) so you can verify data before proceeding.
"""

from __future__ import annotations
import os
import sys
import json
from pathlib import Path
import requests
from dotenv import load_dotenv

# ---- CONFIG (adjust if needed) ----
USERNAME = "alexanderwiebe"      # GitHub user owner of the user project
PROJECT_NUMBER = 1               # Project number in URL
PAGE_SIZE = 100
GRAPHQL_ENDPOINT = "https://api.github.com/graphql"

# ---- ENV ----
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN_FG")
if not TOKEN:
    sys.stderr.write(
        "ERROR: Set GITHUB_TOKEN or GITHUB_TOKEN_FG in .env or environment.\n")
    sys.exit(1)

HEADERS = {"Authorization": f"bearer {TOKEN}",
           "Accept": "application/vnd.github+json"}

# ---- GraphQL Query ----
QUERY = """
query($login:String!, $number:Int!, $after:String){
  user(login:$login){
    projectV2(number:$number){
      id
      title
      items(first:100, after:$after){
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            __typename
            ... on Issue { id number title state url body closedAt createdAt updatedAt }
            ... on PullRequest { id number title state url }
          }
          fieldValues(first:20){
            nodes {
              __typename
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2Field { name } } }
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
              ... on ProjectV2ItemFieldIterationValue { title field { ... on ProjectV2IterationField { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2Field { name } } }
            }
          }
        }
      }
    }
  }
}
"""


def gql_fetch(after: str | None):
    resp = requests.post(
        GRAPHQL_ENDPOINT,
        json={"query": QUERY, "variables": {"login": USERNAME,
                                            "number": PROJECT_NUMBER, "after": after}},
        headers=HEADERS,
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            f"Non-JSON response: status {resp.status_code} body={resp.text[:400]}")
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    root = data.get("data", {})
    user = root.get("user")
    if not user:
        raise RuntimeError(
            "'user' is null: check USERNAME or token scopes (needs classic PAT with repo+project or FG token with Project access)")
    project = user.get("projectV2")
    if not project:
        raise RuntimeError(
            f"Project #{PROJECT_NUMBER} not found for user {USERNAME}")
    return project["items"]


def flatten_field_values(field_nodes):
    result = {}
    for fv in field_nodes:
        t = fv["__typename"]
        if t == "ProjectV2ItemFieldTextValue":
            result[fv["field"]["name"]] = fv.get("text")
        elif t == "ProjectV2ItemFieldSingleSelectValue":
            result[fv["field"]["name"]] = fv.get("name")
        elif t == "ProjectV2ItemFieldIterationValue":
            result[fv["field"]["name"]] = fv.get("title")
        elif t == "ProjectV2ItemFieldNumberValue":
            result[fv["field"]["name"]] = fv.get("number")
    return result


def collect_issues() -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    cursor = None
    page = 1
    while True:
        items = gql_fetch(cursor)
        page_info = items["pageInfo"]
        for node in items["nodes"]:
            content = node.get("content")
            if not content or content.get("__typename") != "Issue":
                continue  # skip PRs or empty
            fields = flatten_field_values(
                node.get("fieldValues", {}).get("nodes", []))
            issues.append({
                "number": content["number"],
                "title": content["title"],
                "state": content["state"],
                "url": content["url"],
                "body": content.get("body") or "",
                "fields": fields,
                "createdAt": content.get("createdAt"),
                "updatedAt": content.get("updatedAt"),
            })
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        page += 1
    return issues


def main():
    try:
        issues = collect_issues()
    except Exception as e:
        sys.stderr.write(f"ERROR fetching issues: {e}\n")
        sys.exit(1)
    # Output compact JSON (pretty if env PRETTY_JSON=1)
    if os.getenv("PRETTY_JSON") == "1":
        print(json.dumps(issues, indent=2))
    else:
        print(json.dumps(issues, separators=(",", ":")))
    sys.stderr.write(
        f"Fetched {len(issues)} issues from Project #{PROJECT_NUMBER}.\n")


if __name__ == "__main__":
    main()
