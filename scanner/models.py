"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass, field

from .date_verification import DateEvidence
from .urlutils import get_domain, normalize_url


@dataclass
class DiscoveredItem:
    url: str
    title: str = ""
    summary: str = ""
    discovery_source: str = ""  # e.g. "rss:https://site/feed", "sitemap:...", "search:google_news_rss"
    discovery_query: str = ""  # the query/variation that found it (search sources)
    query_origin: str = ""  # user | variation | combination | synonym | mined_title
    user_keyword: str = ""  # the user-entered keyword the query came from
    date_evidence: list[DateEvidence] = field(default_factory=list)
    publisher_url: str = ""  # e.g. Google News <source url=...>
    resolved: bool = True  # False when the URL is a search-engine redirect we could not decode
    # Positions observed in relevance-ranked search feeds at scan time:
    # [{"source": "Google News RSS", "query": "...", "rank": 3, "observed_at": "...Z"}]
    search_ranks: list[dict] = field(default_factory=list)

    @property
    def normalized_url(self) -> str:
        return normalize_url(self.url)

    @property
    def domain(self) -> str:
        return get_domain(self.url)

    def merge(self, other: "DiscoveredItem") -> None:
        """Combine evidence when the same URL is found by several methods."""
        if other.discovery_source and other.discovery_source not in self.discovery_source.split(" + "):
            self.discovery_source = " + ".join(filter(None, [self.discovery_source, other.discovery_source]))
        if other.discovery_query and other.discovery_query not in self.discovery_query.split(" | "):
            self.discovery_query = " | ".join(filter(None, [self.discovery_query, other.discovery_query]))
        if other.user_keyword and other.user_keyword not in self.user_keyword.split(" | "):
            self.user_keyword = " | ".join(filter(None, [self.user_keyword, other.user_keyword]))
        self.title = self.title or other.title
        self.summary = self.summary or other.summary
        self.query_origin = self.query_origin or other.query_origin
        self.date_evidence.extend(other.date_evidence)
        self.search_ranks.extend(other.search_ranks)
