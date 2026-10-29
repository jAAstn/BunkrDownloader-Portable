"""Utilities to fetch the operational status of servers from the Bunkr status page."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.config import DEFAULT_HEADERS, STATUS_PAGE


# A subdomain is only declared offline after this many consecutive server-down
# failures; a single timeout or reset counts as a transient blip.
OFFLINE_FAILURE_THRESHOLD = 3

# Seconds an offline mark keeps suppressing downloads before the subdomain gets
# a fresh chance. Without it, a host that recovers mid-run would stay skipped
# until the end of the session.
OFFLINE_MARK_TTL = 120

# Consecutive server-down failures per subdomain, and the moment each subdomain
# was marked offline. Both live for the process, just like ``bunkr_status``.
_failure_counts: dict[str, int] = {}
_offline_marks: dict[str, float] = {}


def fetch_status_page() -> BeautifulSoup | None:
    """Fetch the HTML content of the status page."""
    try:
        response = requests.get(STATUS_PAGE, headers=DEFAULT_HEADERS, timeout=5)
        response.raise_for_status()

    except requests.RequestException:
        logging.warning("An error occurred while fetching the status page")
        return None

    return BeautifulSoup(response.text, "html.parser")


def get_bunkr_status() -> dict[str, str]:
    """Fetch the status of servers from the status page and return a dictionary."""
    soup = fetch_status_page()
    if soup is None:
        logging.warning("Unable to fetch status page; continuing without host data.")
        return {}

    bunkr_status: dict[str, str] = {}
    server_items = soup.find_all(
        "div",
        {
            "class": (
                "flex items-center gap-4 py-4 border-b border-soft last:border-b-0"
            ),
        },
    )

    for server_item in server_items:
        server_name_tag = server_item.find("p")
        server_status_tag = server_item.find("span")

        if server_name_tag is None or server_status_tag is None:
            continue

        server_name = server_name_tag.get_text(strip=True)
        server_status = server_status_tag.get_text(strip=True)
        bunkr_status[server_name] = server_status

    return bunkr_status


def get_offline_servers(bunkr_status: dict[str, str] | None = None) -> dict[str, str]:
    """Return a dictionary of servers that are not operational."""
    return {
        server_name: server_status
        for server_name, server_status in bunkr_status.items()
        if server_status != "Operational"
    }


def get_subdomain(download_link: str) -> str:
    """Extract the subdomain from a given URL."""
    netloc = urlparse(download_link).netloc
    return netloc.split(".")[0]


def _mark_has_expired(subdomain: str) -> bool:
    """Return True when a locally recorded offline mark outlived its TTL."""
    marked_at = _offline_marks.get(subdomain)
    if marked_at is None:
        # Not recorded locally (e.g. reported by the status page): no expiry.
        return False
    return time.monotonic() - marked_at >= OFFLINE_MARK_TTL


def subdomain_is_offline(
    download_link: str, bunkr_status: dict[str, str] | None = None,
) -> bool:
    """Check if the subdomain from the given download link is marked as offline."""
    offline_servers = get_offline_servers(bunkr_status)
    subdomain = get_subdomain(download_link)
    if subdomain not in offline_servers:
        return False

    if not _mark_has_expired(subdomain):
        return True

    # The host had its chance: forget the mark so it is tried from scratch.
    if bunkr_status is not None:
        bunkr_status.pop(subdomain, None)
    _offline_marks.pop(subdomain, None)
    _failure_counts.pop(subdomain, None)
    return False


def mark_subdomain_as_offline(
    bunkr_status: dict[str, str],
    download_link: str,
) -> str | None:
    """Record a server-down failure for the subdomain of a download link.

    The subdomain is only marked offline once it has failed
    ``OFFLINE_FAILURE_THRESHOLD`` times in a row, so a single timeout or reset
    does not disable it for the rest of the run.

    Returns the subdomain when it was marked offline, or None while the
    failure is still considered transient.
    """
    subdomain = get_subdomain(download_link)
    failures = _failure_counts.get(subdomain, 0) + 1
    _failure_counts[subdomain] = failures

    if failures < OFFLINE_FAILURE_THRESHOLD:
        return None

    bunkr_status[subdomain] = "Non-operational"
    _offline_marks.setdefault(subdomain, time.monotonic())
    return subdomain


def note_subdomain_success(
    bunkr_status: dict[str, str],
    download_link: str,
) -> None:
    """Forget past failures for a subdomain once a file downloaded from it."""
    subdomain = get_subdomain(download_link)
    _failure_counts.pop(subdomain, None)
    _offline_marks.pop(subdomain, None)
    bunkr_status.pop(subdomain, None)
