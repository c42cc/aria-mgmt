"""Canonical 20-task corpus for the anchor pressure-test suite.

Each task is a dict with:
  - id: unique identifier
  - category: gmail | calendar | filesystem | github | cross
  - prompt: the !ask text to send via webhook
  - anchor_tools: which anchor-backed tools we expect to fire
"""

TASKS = [
    # --- Gmail (5) ---
    {
        "id": "gmail-count-today",
        "category": "gmail",
        "prompt": "How many emails did I receive today at c@c42.io? Give me the exact count.",
        "anchor_tools": ["search_emails"],
    },
    {
        "id": "gmail-summary-today",
        "category": "gmail",
        "prompt": "Summarize the emails I received today at c@c42.io.",
        "anchor_tools": ["search_emails"],
    },
    {
        "id": "gmail-search-anthropic",
        "category": "gmail",
        "prompt": "How many emails from anthropic.com are in my inbox from the last 3 days?",
        "anchor_tools": ["search_emails"],
    },
    {
        "id": "gmail-read-latest",
        "category": "gmail",
        "prompt": "Read the most recent email in my c@c42.io inbox and tell me the subject, sender, and date.",
        "anchor_tools": ["search_emails"],
    },
    {
        "id": "gmail-unread-count",
        "category": "gmail",
        "prompt": "Exactly how many unread emails are in my c@c42.io Gmail inbox right now?",
        "anchor_tools": ["search_emails"],
    },

    # --- Calendar (5) ---
    {
        "id": "cal-list-calendars",
        "category": "calendar",
        "prompt": "List all my Google calendars.",
        "anchor_tools": ["list-calendars"],
    },
    {
        "id": "cal-today",
        "category": "calendar",
        "prompt": "What events are on my Google Calendar today? List every event with its time.",
        "anchor_tools": ["list-events"],
    },
    {
        "id": "cal-tomorrow",
        "category": "calendar",
        "prompt": "What is on my Google Calendar tomorrow? List every event with its time.",
        "anchor_tools": ["list-events"],
    },
    {
        "id": "cal-this-week",
        "category": "calendar",
        "prompt": "How many calendar events do I have this week (Mon-Sun)?",
        "anchor_tools": ["list-events"],
    },
    {
        "id": "cal-next-meeting",
        "category": "calendar",
        "prompt": "When is my next calendar event? Give me the name and start time.",
        "anchor_tools": ["list-events"],
    },

    # --- Filesystem (4) ---
    {
        "id": "fs-list-src",
        "category": "filesystem",
        "prompt": "How many Python files are in /Users/corbin/PycharmProjects/agi_env_v1/ucs2/src? List them.",
        "anchor_tools": ["search_files"],
    },
    {
        "id": "fs-search-prompts",
        "category": "filesystem",
        "prompt": "List all .md files in /Users/corbin/PycharmProjects/agi_env_v1/ucs2/prompts/",
        "anchor_tools": ["search_files", "list_directory"],
    },
    {
        "id": "fs-read-config",
        "category": "filesystem",
        "prompt": "What is the daily spend cap in /Users/corbin/PycharmProjects/agi_env_v1/ucs2/src/config.py?",
        "anchor_tools": ["read_file"],
    },
    {
        "id": "fs-count-tests",
        "category": "filesystem",
        "prompt": "How many test files are in /Users/corbin/PycharmProjects/agi_env_v1/ucs2/tests/?",
        "anchor_tools": ["search_files", "list_directory"],
    },

    # --- GitHub (3) ---
    {
        "id": "gh-last-commit",
        "category": "github",
        "prompt": "What was the last commit message on the c42cc/ucs repo on GitHub?",
        "anchor_tools": ["list_commits"],
    },
    {
        "id": "gh-open-prs",
        "category": "github",
        "prompt": "How many open pull requests are there on c42cc/ucs?",
        "anchor_tools": ["list_pulls"],
    },
    {
        "id": "gh-recent-commits",
        "category": "github",
        "prompt": "List the 5 most recent commit messages on c42cc/ucs.",
        "anchor_tools": ["list_commits"],
    },

    # --- Cross-tool (3) ---
    {
        "id": "cross-email-calendar",
        "category": "cross",
        "prompt": "Do I have any calendar events today AND any emails from today? Give me counts for both.",
        "anchor_tools": ["search_emails", "list-events"],
    },
    {
        "id": "cross-github-files",
        "category": "cross",
        "prompt": "What was the last commit on c42cc/ucs and how many .py files are in the src directory?",
        "anchor_tools": ["list_commits", "search_files"],
    },
    {
        "id": "cross-email-save",
        "category": "cross",
        "prompt": "Search my Gmail for the most recent email with 'receipt' in the subject. Tell me the subject and sender.",
        "anchor_tools": ["search_emails"],
    },
]
