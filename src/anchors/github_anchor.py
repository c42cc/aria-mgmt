"""GitHub anchor: list_commits, list_issues, list_pulls re-fetch via REST."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import urllib.request

from .base import AnchorReport
from .normalize import extract_number_claim

log = logging.getLogger(__name__)


def _github_token() -> str:
    return os.getenv("GITHUB_TOKEN", os.getenv("GITHUB_TOKEN_MORE_SCOPE", ""))


def _github_get(url: str) -> dict | list:
    token = _github_token()
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}" if token else "",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ucs2-anchor",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


class GithubAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        tool_name = tool_call.get("tool", "")
        if "commit" in tool_name.lower():
            return await self._check_commits(tool_call, aria_result)
        if "issue" in tool_name.lower():
            return await self._check_issues(tool_call, aria_result)
        if "pull" in tool_name.lower():
            return await self._check_pulls(tool_call, aria_result)
        report = AnchorReport(tool=f"github.{tool_name}")
        report.fact("status", "unrecognized_github_tool", "registry")
        return report

    async def _check_commits(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="github.list_commits")
        args = tool_call.get("args", {})
        owner = args.get("owner", "")
        repo = args.get("repo", "")
        if not owner or not repo:
            report.fact("status", "missing_owner_or_repo", "trace_inspection")
            return report

        per_page = args.get("perPage", args.get("per_page", 10))
        url = f"https://api.github.com/repos/{owner}/{repo}/commits?per_page={per_page}"

        try:
            commits = await asyncio.to_thread(_github_get, url)
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "github_api")
            return report

        ground_shas = [c["sha"][:7] for c in commits]
        report.fact("ground_truth_shas", ground_shas, "github_api")
        report.fact("ground_truth_latest_msg", commits[0]["commit"]["message"][:200] if commits else "", "github_api")

        for sha in ground_shas[:3]:
            if sha in aria_result:
                report.fact("sha_match", sha, "comparison")
                break
        else:
            if ground_shas and per_page == 1:
                report.violate(7, "soft", f"Aria did not cite ground-truth SHA {ground_shas[0]}")

        return report

    async def _check_issues(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="github.list_issues")
        args = tool_call.get("args", {})
        owner = args.get("owner", "")
        repo = args.get("repo", "")
        if not owner or not repo:
            return report

        url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=30"
        try:
            issues = await asyncio.to_thread(_github_get, url)
            issues = [i for i in issues if "pull_request" not in i]
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "github_api")
            return report

        report.fact("ground_truth_count", len(issues), "github_api")
        return report

    async def _check_pulls(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="github.list_pulls")
        args = tool_call.get("args", {})
        owner = args.get("owner", "")
        repo = args.get("repo", "")
        if not owner or not repo:
            return report

        url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=30"
        try:
            pulls = await asyncio.to_thread(_github_get, url)
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "github_api")
            return report

        report.fact("ground_truth_count", len(pulls), "github_api")
        return report

    async def health_check(self) -> bool:
        if not _github_token():
            return False
        try:
            await asyncio.to_thread(_github_get, "https://api.github.com/rate_limit")
            return True
        except Exception:
            return False
