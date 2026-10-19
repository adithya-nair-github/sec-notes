"""Polite HTTP client for SEC EDGAR (declared User-Agent, rate limit, retries).

SEC fair-access rules: declare who you are in the User-Agent and stay under
10 requests/second. We default to 5/s.
"""
import json
import time

import requests


class Http:
    def __init__(self, user_agent, rps=5, session=None):
        self.ua = user_agent
        self.min_gap = 1.0 / max(rps, 0.1)
        self.last = 0.0
        self.s = session or requests.Session()
        self.calls = 0
        self.failures = 0

    def _wait(self):
        gap = time.time() - self.last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last = time.time()

    def get(self, url, want="text", sec=True, tries=4, timeout=30):
        """Return text/json, or None on 404. Retries 429/5xx with backoff."""
        headers = {"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"} if sec else {
            "User-Agent": "Mozilla/5.0 (compatible; sec-notes/1.0)"}
        for attempt in range(tries):
            self._wait()
            self.calls += 1
            try:
                r = self.s.get(url, headers=headers, timeout=timeout)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code >= 400:
                self.failures += 1
                return None
            if want == "bytes":
                return r.content
            if want == "json":
                try:
                    return r.json()
                except (ValueError, json.JSONDecodeError):
                    return None
            try:  # SEC pages often send no charset; reading them as Latin-1 turns spaces into "Â"
                return r.content.decode("utf-8")
            except UnicodeDecodeError:
                return r.text
        self.failures += 1
        return None

    def post_json(self, url, payload, tries=2, timeout=30):
        """JSON POST (used by career-site job searches). Returns parsed JSON or None."""
        for attempt in range(tries):
            self._wait()
            self.calls += 1
            try:
                r = self.s.post(url, json=payload, timeout=timeout,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; sec-notes/1.0)",
                                         "Accept": "application/json", "Content-Type": "application/json"})
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code >= 400:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None
