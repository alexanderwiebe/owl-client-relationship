import os
import sys
import requests
import json
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (parent of this script's directory)
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN_FG")
if not TOKEN:
    sys.stderr.write(
        "ERROR: GITHUB_TOKEN or GITHUB_TOKEN_FG not set in environment.\n")
    sys.exit(1)
USER = "alexanderwiebe"
PROJECT_NUMBER = 1
API = "https://api.github.com/graphql"
headers = {"Authorization": f"bearer {TOKEN}"}

query = """
query($login:String!, $number:Int!, $after:String){
  user(login:$login){
    projectV2(number:$number){
      id
      title
      items(first:100, after:$after){
        pageInfo { hasNextPage endCursor }
        nodes{
          id
          content{
            __typename
            ... on Issue {
              id
              number
              title
              state
              url
              closedAt
            }
            ... on PullRequest {
              id
              number
              title
              state
              url
              mergedAt
            }
          }
          fieldValues(first:20){
            nodes{
              __typename
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2Field { name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title
                field { ... on ProjectV2IterationField { name } }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number
                field { ... on ProjectV2Field { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""


def run(after=None):
    payload = {"query": query,
               "variables": {"login": USER,
                             "number": PROJECT_NUMBER,
                             "after": after}}
    r = requests.post(API, json=payload, headers=headers)
    raw_text = r.text
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f"Non-JSON response (status {r.status_code}):\n{raw_text[:500]}")
    if "errors" in data:
        # Attach partial data if present
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    if "data" not in data:
        raise RuntimeError(f"Missing 'data' key in response: {raw_text[:500]}")
    user_block = data["data"].get("user")
    if not user_block:
        raise RuntimeError(
            "'user' is null – check USER value or token scopes (needs project:read)")
    project_block = user_block.get("projectV2")
    if not project_block:
        raise RuntimeError(
            f"Project number {PROJECT_NUMBER} not found for user {USER}.")
    return project_block["items"]


all_issues = []
cursor = None
while True:
    try:
        resp = run(cursor)
    except Exception as e:
        sys.stderr.write(f"Fetch failed: {e}\n")
        sys.exit(1)
        for node in resp["nodes"]:
            c = node["content"]
            if not c or c["__typename"] != "Issue":
                continue
            # Flatten field values
            fields = {}
            for fv in node["fieldValues"]["nodes"]:
                t = fv["__typename"]
                if t == "ProjectV2ItemFieldTextValue":
                    fields[fv["field"]["name"]] = fv["text"]
                elif t == "ProjectV2ItemFieldSingleSelectValue":
                    fields[fv["field"]["name"]] = fv["name"]
                elif t == "ProjectV2ItemFieldIterationValue":
                    fields[fv["field"]["name"]] = fv["title"]
                elif t == "ProjectV2ItemFieldNumberValue":
                    fields[fv["field"]["name"]] = fv["number"]
            all_issues.append({
                "number": c["number"],
                "title": c["title"],
                "state": c["state"],
                "url": c["url"],
                "fields": fields
            })
        if not resp["pageInfo"]["hasNextPage"]:
            break
        cursor = resp["pageInfo"]["endCursor"]

print(f"Collected {len(all_issues)} issues")
