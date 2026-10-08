"""Small index over data/procurement_policy.md so the policy tool can quote it.

The policy file is the source of truth. Rather than pasting policy text into
prompts, the tool reads the real sections out of the file.
"""
from __future__ import annotations

import re
from functools import lru_cache

from src.data_access import load_policy_text

TOPICS: dict[str, int] = {
    "required_information": 1,
    "budget": 2,
    "existing_software": 3,
    "approval_thresholds": 4,
    "security": 5,
    "privacy": 6,
    "legal": 7,
    "ai_tools": 8,
    "untrusted_content": 9,
    "tool_failures": 10,
    "human_authority": 11,
}


class PolicyIndex:
    """Groups the section lookup behind a tiny class, so a future caller could
    swap in a different index implementation (e.g. one backed by a database)
    without changing the module-level functions below, which just delegate to
    a shared instance.
    """

    @staticmethod
    @lru_cache(maxsize=1)
    def sections() -> dict[int, dict[str, str]]:
        text = load_policy_text()
        # splits on headers like "## 4. Financial approval thresholds"
        parts = re.split(r"^##\s+(\d+)\.\s*(.+)$", text, flags=re.MULTILINE)
        found: dict[int, dict[str, str]] = {}
        for i in range(1, len(parts) - 2, 3):
            number = int(parts[i])
            found[number] = {"number": number, "title": parts[i + 1].strip(), "body": parts[i + 2].strip()}
        return found


def sections() -> dict[int, dict[str, str]]:
    return PolicyIndex.sections()


def get_section(number: int) -> dict[str, str] | None:
    return PolicyIndex.sections().get(number)


def get_topic(topic: str) -> dict[str, str] | None:
    number = TOPICS.get(topic.strip().lower())
    if number is None:
        return None
    return get_section(number)


def search(keyword: str) -> list[dict[str, str]]:
    """Fall back to a keyword match when the topic name is not recognised."""
    needle = keyword.strip().lower()
    if not needle:
        return []
    return [s for s in PolicyIndex.sections().values() if needle in f"{s['title']} {s['body']}".lower()]
