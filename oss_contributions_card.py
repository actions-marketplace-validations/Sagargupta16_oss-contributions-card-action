"""Render animated SVG cards of your pull requests to other people's repositories.

Finds your merged and open pull requests with the GitHub GraphQL search API,
drops repositories you own, private repositories, and anything matched by
OSS_EXCLUDE_OWNERS or OSS_EXCLUDE_REPOS, then writes up to three SVG cards:

- a summary: merged upstream, in review, projects contributed to, combined
  stars, and a chip per project ranked by stars
- the merged pull requests, newest first
- the pull requests in review, grouped by repository

GitHub serves README images through an <img>, so every card is self-contained:
no scripts, no external fonts or images, CSS and SMIL animation only.

If the API fails, the cards already on disk are kept and the run exits 0, so a
flaky API never blanks a README. With no earlier card to keep it exits 1.
Stdlib only: action.yml installs nothing.

Run standalone:
  GITHUB_TOKEN=... OSS_USERNAME=your-login python oss_contributions_card.py
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html import escape
from pathlib import Path

# Palette and fonts of the profile README cards these designs come from: warm
# near-black, one blue family, green for merged work, amber for work in review.
BG = "#0b1012"
CARD = "#0f171c"
BLUE_LIGHT = "#60a5fa"
SKY = "#38bdf8"
GREEN = "#22c55e"
AMBER = "#f59e0b"
MONO = "'JetBrains Mono','SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"
SANS = "'Segoe UI',Inter,-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif"
SVG_CLOSE = "</svg>"

API_URL = "https://api.github.com/graphql"
ALLOWED_HOSTS = ("https://api.github.com/",)
MAX_PAGES = 10  # 100 results a page, and the search API stops at 1000
REVIEW_NUMBERS = 4  # PR numbers listed per repository before "+N"
OUTPUTS = (
    ("summary", "OSS_OUTPUT_SUMMARY", "assets/oss.svg"),
    ("merged", "OSS_OUTPUT_MERGED", "assets/oss-merged.svg"),
    ("review", "OSS_OUTPUT_REVIEW", "assets/oss-review.svg"),
)

# A login goes into the search query, so nothing else may: no spaces, no colons.
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
HEX_COLOR = re.compile(r"#(?:[0-9a-f]{3}){1,2}")
# An exclude-repos entry: owner/repo, or owner/* for every repository of an owner.
REPO = re.compile(r"[A-Za-z0-9_.-]+/(?:\*|[A-Za-z0-9_.-]+)")

# Code points dropped from API text: emoji and pictographs with their
# presentation selector, keycap and tag characters, then everything XML 1.0
# does not allow. Written as numbers so this file stays plain ASCII.
DROPPED_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x231A, 0x231B),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x20E3, 0x20E3),
    (0xFE0F, 0xFE0F),
    (0xE0020, 0xE007F),
    (0x00, 0x08),
    (0x0B, 0x0C),
    (0x0E, 0x1F),
    (0xD800, 0xDFFF),
    (0xFFFE, 0xFFFF),
)
CLEAN = {cp: None for lo, hi in DROPPED_RANGES for cp in range(lo, hi + 1)}
# figure dash, en dash, em dash and horizontal bar all become a plain hyphen
CLEAN.update(dict.fromkeys(range(0x2012, 0x2016), "-"))

SEARCH_QUERY = (
    "query($q: String!, $after: String) {"
    " search(query: $q, type: ISSUE, first: 100, after: $after) {"
    " issueCount pageInfo { hasNextPage endCursor }"
    " nodes { ... on PullRequest { title number url mergedAt"
    " repository { nameWithOwner stargazerCount isPrivate owner { login } } } } } }"
)

EMPTY_MERGED = (
    "No merged pull requests yet",
    "Pull requests merged into repositories you do not own are listed here, newest first.",
    "",
)
EMPTY_REVIEW = (
    "No pull requests in review",
    "Open pull requests to repositories you do not own are listed here, grouped by repository.",
    "",
)


class ConfigError(Exception):
    """An input the action cannot run with."""


class ApiError(Exception):
    """The GraphQL API answered, but not with search results."""


# Every way a request or its response can fail. OSError covers URLError,
# HTTPError and timeouts; ValueError covers a body that is not JSON.
FETCH_ERRORS = (ApiError, OSError, ValueError, http.client.HTTPException)


@dataclass(frozen=True)
class PullRequest:
    repo: str
    owner: str
    title: str
    number: int
    url: str
    merged_at: str
    stars: int
    private: bool


@dataclass(frozen=True)
class Config:
    username: str
    token: str
    exclude_owners: frozenset[str]
    exclude_repos: frozenset[str]
    outputs: dict[str, Path]
    max_rows: int
    accent: str


def load_config(env: Mapping[str, str]) -> Config:
    """Read the action inputs from the environment, or raise ConfigError."""
    username = env.get("OSS_USERNAME", "").strip()
    if not LOGIN.fullmatch(username):
        raise ConfigError(
            f"username must be a GitHub login, got {username!r} "
            "(the username input, or OSS_USERNAME when running by hand)"
        )
    token = (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "no token: set the token input, or GITHUB_TOKEN when running by hand"
        )
    outputs = {
        card: Path(path)
        for card, name, default in OUTPUTS
        if (path := env.get(name, default).strip())
    }
    if not outputs:
        raise ConfigError("every output path is empty, so there is nothing to render")
    rows = (env.get("OSS_MAX_ROWS") or "12").strip()
    if not re.fullmatch(r"[1-9][0-9]*", rows):
        raise ConfigError(f"max-rows must be a whole number of 1 or more, got {rows!r}")
    accent = env.get("OSS_ACCENT", "").strip().lower()
    if accent and not HEX_COLOR.fullmatch(accent):
        raise ConfigError(f"accent must be a hex color like #a78bfa, got {accent!r}")
    if len(accent) == 4:  # #abc to #aabbcc, so an alpha suffix can be appended
        accent = "#" + "".join(c * 2 for c in accent[1:])
    owners = env.get("OSS_EXCLUDE_OWNERS", "").split(",")
    repos = [
        r.strip() for r in env.get("OSS_EXCLUDE_REPOS", "").split(",") if r.strip()
    ]
    bad = [r for r in repos if not REPO.fullmatch(r)]
    if bad:
        raise ConfigError(
            f"exclude-repos entries must be owner/repo or owner/*, got {', '.join(map(repr, bad))}"
        )
    return Config(
        username=username,
        token=token,
        exclude_owners=frozenset(o.strip() for o in owners if o.strip()),
        exclude_repos=frozenset(repos),
        outputs=outputs,
        max_rows=int(rows),
        accent=accent,
    )


# ---------------------------------------------------------------- GitHub API


def fetch(url: str, body: dict, token: str, timeout: int = 30) -> bytes:
    """POST a JSON body and return the raw response. https to allowlisted hosts only."""
    if not url.startswith(ALLOWED_HOSTS):
        raise ValueError(f"refusing to fetch {url}")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "oss-contributions-card-action",
        },
    )
    # scheme and host were checked against ALLOWED_HOSTS above
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return resp.read()


def graphql(query: str, variables: dict, token: str) -> dict:
    """Run one GraphQL request and return its data. Any reported error raises ApiError."""
    payload = json.loads(
        fetch(API_URL, {"query": query, "variables": variables}, token)
    )
    if not isinstance(payload, dict):
        raise ApiError("the GraphQL API sent an unexpected response")
    if payload.get("errors"):
        errors = json.dumps(payload["errors"])[:300]
        raise ApiError(f"the GraphQL API reported errors: {errors}")
    if not isinstance(payload.get("data"), dict):
        raise ApiError("the GraphQL API sent no data")
    return payload["data"]


def plain(text: str) -> str:
    """Return API text with dashes flattened and emoji and XML-invalid characters dropped."""
    return " ".join(text.translate(CLEAN).split())


def parse_node(node: dict | None) -> PullRequest | None:
    """Return the pull request in one search node, or None for an empty node."""
    if not node or not node.get("repository"):
        return None  # a result the PullRequest fragment did not match
    repo = node["repository"]
    return PullRequest(
        repo=repo["nameWithOwner"],
        owner=repo["owner"]["login"],
        title=plain(node["title"]),
        number=int(node["number"]),
        url=node["url"],
        merged_at=node.get("mergedAt") or "",
        stars=int(repo.get("stargazerCount") or 0),
        private=bool(repo.get("isPrivate")),
    )


def search_prs(username: str, state: str, token: str) -> list[PullRequest]:
    """Return every pull request by username, in one state, to repositories they do not own."""
    query = f"is:pr author:{username} is:{state} -user:{username}"
    found: dict[str, PullRequest] = {}
    after = None
    total = 0
    for _ in range(MAX_PAGES):
        data = graphql(SEARCH_QUERY, {"q": query, "after": after}, token)
        try:
            search = data["search"]
            total = search["issueCount"]
            for node in search["nodes"]:
                pr = parse_node(node)
                if pr:
                    found.setdefault(pr.url, pr)  # a page boundary can repeat a result
            after = search["pageInfo"]["endCursor"]
            more = search["pageInfo"]["hasNextPage"]
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ApiError(f"unexpected search response ({exc!r})") from exc
        if not (more and after):
            break
    if total > MAX_PAGES * 100:
        print(
            f"::warning::GitHub search found {total} {state} pull requests but "
            f"returns at most {MAX_PAGES * 100}; the cards count {len(found)}."
        )
    return list(found.values())


def select_upstream(
    prs: Iterable[PullRequest],
    username: str,
    exclude_owners: Iterable[str],
    exclude_repos: Iterable[str] = (),
) -> list[PullRequest]:
    """Return the pull requests to public repositories someone else owns, minus exclusions.

    Every comparison is case-insensitive. An exclude_repos entry is owner/repo,
    or owner/* to drop every repository of that owner.
    """
    owners = {o.lower() for o in exclude_owners} | {username.lower()}
    repos: set[str] = set()
    for entry in exclude_repos:
        owner, _, name = entry.lower().partition("/")
        if name == "*":
            owners.add(owner)
        else:
            repos.add(f"{owner}/{name}")
    return [
        pr
        for pr in prs
        if not pr.private
        and pr.owner.lower() not in owners
        and pr.repo.lower() not in repos
    ]


def projects(merged: Iterable[PullRequest]) -> dict[str, tuple[int, int]]:
    """Return {repository: (merged pull requests, stars)}, biggest projects first."""
    counts: Counter[str] = Counter()
    stars: dict[str, int] = {}
    for pr in merged:
        counts[pr.repo] += 1
        stars[pr.repo] = pr.stars
    ranked = sorted(counts, key=lambda r: (-stars[r], -counts[r], r.lower()))
    return {r: (counts[r], stars[r]) for r in ranked}


def summarize(merged: list[PullRequest], open_prs: list[PullRequest]) -> dict[str, int]:
    """Return the numbers the action reports as outputs."""
    ranked = projects(merged)
    return {
        "merged": len(merged),
        "open": len(open_prs),
        "projects": len(ranked),
        "stars": sum(stars for _, stars in ranked.values()),
    }


# ---------------------------------------------------------------- cards


def svg_open(w: int, h: int, label: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="{escape(label)}">'
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rsplit(" ", 1)[0] + "..."


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _compact(n: int) -> str:
    """Return a star total the way its tile shows it: 850, 12K+, 1.2M+."""
    if n >= 1_000_000:
        return f"{n // 100_000 / 10:g}M+"
    if n >= 1000:
        return f"{n // 1000}K+"
    return str(n)


def _oss_chip_label(repo: str, count: int, stars: int) -> str:
    """Return a chip label: repo, a count when merged more than once, and its stars."""
    label = _clip(repo, 60)
    if count > 1:
        label += f"  x{count}"
    if stars >= 1000:
        label += f"  {stars / 1000:.1f}K"
    elif stars:
        label += f"  {stars}"
    return label


def render_summary(
    merged: list[PullRequest], open_prs: list[PullRequest], accent: str = ""
) -> str:
    """Return the summary card: four count tiles, then a chip per project by stars."""
    ranked = projects(merged)
    reach = sum(stars for _, stars in ranked.values())
    kicker = accent or BLUE_LIGHT
    chip = accent or SKY
    w, h = 840, 250
    tiles = [
        (str(len(merged)), "merged upstream"),
        (str(len(open_prs)), "in review"),
        (str(len(ranked)), "projects contributed to"),
        (_compact(reach), "combined stars"),
    ]
    parts = [
        svg_open(
            w,
            h,
            f"Open source: {len(merged)} merged upstream, {len(open_prs)} in review, across {len(ranked)} projects",
        ),
        f"<style>.m{{font-family:{MONO};font-weight:700;letter-spacing:1.2px}}.v{{font-family:{SANS};font-weight:800}}</style>",
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="14" fill="{BG}" stroke="rgba(255,255,255,0.08)"/>',
        f'<text class="m" x="24" y="30" fill="{kicker}" font-size="10">OPEN SOURCE  |  MERGED UPSTREAM</text>',
    ]
    tw = (w - 32 - 3 * 10) / 4
    for i, (value, label) in enumerate(tiles):
        x = 16 + i * (tw + 10)
        parts.append(
            f'<g opacity="0"><animate attributeName="opacity" to="1" begin="{0.1 + i * 0.1:.1f}s" dur="0.4s" fill="freeze"/>'
            f'<rect x="{x:.1f}" y="44" width="{tw:.1f}" height="62" rx="10" fill="{CARD}" stroke="rgba(255,255,255,0.07)"/>'
            f'<text class="v" x="{x + 14:.1f}" y="76" fill="#f3f4f6" font-size="22">{value}</text>'
            f'<text class="m" x="{x + 14:.1f}" y="95" fill="rgba(255,255,255,0.5)" font-size="8">{label.upper()}</text></g>'
        )
    if not ranked:
        parts.append(
            '<g opacity="0"><animate attributeName="opacity" to="1" begin="0.5s" dur="0.4s" fill="freeze"/>'
            '<text class="m" x="24" y="143" fill="rgba(255,255,255,0.5)" font-size="9">'
            "NO MERGED PULL REQUESTS YET  |  PROJECTS SHOW UP HERE AS YOUR WORK LANDS</text></g>"
        )
    # merged-into chips, biggest projects first
    labels = [_oss_chip_label(r, count, stars) for r, (count, stars) in ranked.items()]
    for i, (x, y, cw, label) in enumerate(_layout_chips(labels, w, h)):
        parts.append(
            f'<g opacity="0"><animate attributeName="opacity" to="1" begin="{0.5 + i * 0.06:.2f}s" dur="0.4s" fill="freeze"/>'
            f'<rect x="{x:.1f}" y="{y}" width="{cw:.1f}" height="22" rx="11" fill="{chip}12" stroke="{chip}44"/>'
            f'<text class="m" x="{x + cw / 2:.1f}" y="{y + 15}" text-anchor="middle" fill="rgba(255,255,255,0.8)" font-size="9">{escape(label)}</text></g>'
        )
    parts.append(SVG_CLOSE)
    return "".join(parts)


def _layout_chips(
    labels: list[str], w: int, h: int
) -> list[tuple[float, int, float, str]]:
    """Return (x, y, width, label) per chip, wrapping rows; overflow ends on a "+N more" chip."""
    placed: list[tuple[float, int, float, str]] = []
    x, y = 24.0, 128
    for label in labels:
        cw = 16 + len(label) * 6.3
        if x + cw > w - 24:
            x, y = 24.0, y + 30
        if y > h - 30:
            break
        placed.append((x, y, cw, label))
        x += cw + 8
    hidden = len(labels) - len(placed)
    # A chip is under 500px wide (repo clipped to 60 characters), so "+N more"
    # always fits after the first chip of a row: this only pops within the
    # last row.
    while hidden:
        more = f"+{hidden} more"
        mw = 16 + len(more) * 6.3
        lx, ly, lw, _ = placed[-1]
        if lx + lw + 8 + mw <= w - 24:
            placed.append((lx + lw + 8, ly, mw, more))
            break
        placed.pop()
        hidden += 1
    return placed


def render_list_card(
    kicker: str, rows: list[tuple[str, str, str]], accent: str, footer: str = ""
) -> str:
    """Return a card of rows: bold primary, dimmed secondary line, mono meta on the right."""
    w = 840
    row_h = 46
    top = 50
    h = top + len(rows) * row_h + (34 if footer else 12)
    parts = [
        svg_open(
            w, h, kicker.title() + ": " + "; ".join(f"{a} {b} {c}" for a, b, c in rows)
        ),
        (
            f"<style>.m{{font-family:{MONO};font-weight:700;letter-spacing:1.2px}}.t{{font-family:{SANS};font-weight:700}}"
            f".s{{font-family:{SANS};font-weight:500}}</style>"
        ),
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="14" fill="{BG}" stroke="rgba(255,255,255,0.08)"/>',
        f'<text class="m" x="24" y="31" fill="{accent}" font-size="10">{escape(kicker)}</text>',
    ]
    for i, (primary, secondary, meta) in enumerate(rows):
        y = top + i * row_h
        begin = 0.15 + i * 0.07
        parts.append(
            f'<g opacity="0"><animate attributeName="opacity" to="1" begin="{begin:.2f}s" dur="0.4s" fill="freeze"/>'
            f'<animateTransform attributeName="transform" type="translate" from="-8 0" to="0 0" begin="{begin:.2f}s" dur="0.4s" fill="freeze"/>'
            f'<line x1="24" y1="{y}" x2="{w - 24}" y2="{y}" stroke="rgba(255,255,255,0.06)"/>'
            f'<circle cx="30" cy="{y + 23}" r="3" fill="{accent}"/>'
            f'<text class="t" x="44" y="{y + 20}" fill="#f3f4f6" font-size="13.5">{escape(_clip(primary, 60))}</text>'
            f'<text class="s" x="44" y="{y + 37}" fill="rgba(255,255,255,0.6)" font-size="11.5">{escape(_clip(secondary, 104))}</text>'
            f'<text class="m" x="{w - 24}" y="{y + 20}" text-anchor="end" fill="rgba(255,255,255,0.45)" font-size="9">{escape(meta.upper())}</text></g>'
        )
    if footer:
        parts.append(
            f'<text class="s" x="24" y="{h - 14}" fill="rgba(255,255,255,0.55)" font-size="11">{escape(_clip(footer, 150))}</text>'
        )
    parts.append(SVG_CLOSE)
    return "".join(parts)


def render_merged(merged: list[PullRequest], max_rows: int) -> str:
    """Return the merged card: one row per pull request, newest first."""
    newest = sorted(merged, key=lambda pr: (pr.merged_at, pr.url), reverse=True)
    rows = [(pr.repo, pr.title, f"#{pr.number}") for pr in newest[:max_rows]]
    if not rows:
        return render_list_card("0 MERGED UPSTREAM", [EMPTY_MERGED], GREEN)
    hidden = len(newest) - len(rows)
    footer = f"+{_plural(hidden, 'more merged pull request')}" if hidden else ""
    return render_list_card(
        f"{len(newest)} MERGED UPSTREAM  |  NEWEST FIRST", rows, GREEN, footer
    )


def _numbers(prs: list[PullRequest]) -> str:
    labels = [f"#{pr.number}" for pr in prs[:REVIEW_NUMBERS]]
    if len(prs) > REVIEW_NUMBERS:
        labels.append(f"+{len(prs) - REVIEW_NUMBERS}")
    return ", ".join(labels)


def render_review(open_prs: list[PullRequest], max_rows: int) -> str:
    """Return the in-review card: one row per repository, most open pull requests first."""
    grouped: dict[str, list[PullRequest]] = {}
    for pr in sorted(open_prs, key=lambda pr: pr.number):
        grouped.setdefault(pr.repo, []).append(pr)
    ordered = sorted(
        grouped.values(),
        key=lambda prs: (-len(prs), -prs[0].stars, prs[0].repo.lower()),
    )
    rows = [
        (prs[0].repo, "; ".join(pr.title for pr in prs), _numbers(prs))
        for prs in ordered[:max_rows]
    ]
    count = len(open_prs)
    kicker = f"{count} PR{'' if count == 1 else 'S'} IN REVIEW"
    if not rows:
        return render_list_card(kicker, [EMPTY_REVIEW], AMBER)
    hidden = sum(len(prs) for prs in ordered[max_rows:])
    footer = f"+{_plural(hidden, 'more pull request')} in review" if hidden else ""
    return render_list_card(kicker, rows, AMBER, footer)


# ---------------------------------------------------------------- files and outputs


def write(path: Path, content: str) -> None:
    """Write one card, leaving the file untouched when nothing changed."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        print(f"{path} unchanged")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"wrote {path} ({len(content):,} bytes)")


def set_outputs(target: str, values: dict[str, int]) -> None:
    """Append key=value lines to $GITHUB_OUTPUT when running inside Actions."""
    if target:
        with Path(target).open("a", encoding="utf-8") as fh:
            fh.writelines(f"{key}={value}\n" for key, value in values.items())


def keep_existing(paths: Iterable[Path], exc: Exception) -> int:
    """Handle a failed fetch: keep every earlier card, or fail when one is missing."""
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(
            f"::error::GitHub API request failed ({exc}) and there is no earlier "
            f"card to keep for {', '.join(missing)}."
        )
        return 1
    print(f"::warning::GitHub API request failed ({exc}); kept the existing cards.")
    return 0


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        config = load_config(env)
    except ConfigError as exc:
        print(f"::error::{exc}")
        return 1
    print(
        f"Searching pull requests by {config.username} to repositories they do not own"
    )
    try:
        merged = search_prs(config.username, "merged", config.token)
        open_prs = search_prs(config.username, "open", config.token)
    except FETCH_ERRORS as exc:
        return keep_existing(config.outputs.values(), exc)
    owners, repos = config.exclude_owners, config.exclude_repos
    merged = select_upstream(merged, config.username, owners, repos)
    open_prs = select_upstream(open_prs, config.username, owners, repos)
    cards = {
        "summary": render_summary(merged, open_prs, config.accent),
        "merged": render_merged(merged, config.max_rows),
        "review": render_review(open_prs, config.max_rows),
    }
    for card, path in config.outputs.items():
        write(path, cards[card])
    stats = summarize(merged, open_prs)
    print(
        f"{stats['merged']} merged and {stats['open']} in review across "
        f"{stats['projects']} projects with {stats['stars']:,} combined stars"
    )
    set_outputs(env.get("GITHUB_OUTPUT", ""), stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
