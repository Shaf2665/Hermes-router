"""Feedback destinations must survive dashboard layout changes."""
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re


def test_message_targets_exist_once_in_dashboard():
    html = (Path(__file__).resolve().parents[1] / "dashboard.html").read_text(encoding="utf-8")
    ids = Counter()

    class Elements(HTMLParser):
        def handle_starttag(self, tag, attrs):
            element_id = dict(attrs).get("id")
            if element_id:
                ids[element_id] += 1

    Elements().feed(html)
    targets = set(re.findall(r"\bset(?:Warn)?Msg\(\s*['\"]([^'\"]+)['\"]", html))
    assert targets
    assert {target: ids[target] for target in targets if ids[target] != 1} == {}
