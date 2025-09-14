import re
from github import Github

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

TOKEN = os.getenv("GITHUB_TOKEN_FG")
if not TOKEN:
    sys.stderr.write("ERROR: GITHUB_TOKEN environment variable not set.\n")
    sys.exit(1)


# --- CONFIG ---
REPO_NAME = "alexanderwiebe/owl-client-relationship"
PROJECT_NUMBER = 1  # the project number inside the repo (check in URL)

# --- CONNECT ---
g = Github(TOKEN)
repo = g.get_repo(REPO_NAME)

# read outline.md
with open("outline.md", "r", encoding="utf-8") as f:
    content = f.read()

# regex to capture phases + tasks
phase_pattern = re.compile(r'subgraph\s+(P\d+)\["(.*?)"\](.*?)end', re.S)
task_pattern = re.compile(r'P\d+[A-Z]\["- \[ \] (.*?)\s+\[\[(.*?)\]\]\"]')

phases = {}
for match in phase_pattern.finditer(content):
    phase_id, phase_name, phase_body = match.groups()
    tasks = task_pattern.findall(phase_body)
    phases[phase_name] = [{"title": t[0], "link": t[1]} for t in tasks]

# ensure milestones for each Phase
milestones = {m.title: m for m in repo.get_milestones(state="all")}
for phase_name in phases.keys():
    if phase_name not in milestones:
        milestones[phase_name] = repo.create_milestone(phase_name)

# create issues
for phase_name, tasks in phases.items():
    for task in tasks:
        title = task["title"]
        link = task["link"]
        body = f"Notebook: [{link}]({link})\n\nPhase: {phase_name}"
        issue = repo.create_issue(
            title=title,
            body=body,
            milestone=milestones[phase_name]
        )
        print(f"✅ Created issue: {issue.html_url}")
