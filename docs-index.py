#!/usr/bin/env python3
"""
docs-index - a Cursor-style local docs index. Point it at any docs URL and
it builds a local SQLite + FTS5 index of every page on that host. Content
is stored as markdown (Mintlify .md source when available, else converted
from HTML). Search returns the matched section, not just a snippet.

Indexing:
  docs-index sync <URL>                  # discover + index
  docs-index sync <URL> --workers 32 -v  # parallel, verbose per-URL
  docs-index sync <URL> --force          # re-fetch everything, ignore ETags
  docs-index sync <URL> --prefix-only    # only URLs under exact seed prefix
  docs-index sync --seeds-file urls.txt  # explicit URL list, skip discovery

Querying:
  docs-index search "rate limit"             # FTS5; auto-filters to active site
  docs-index search "webhook" --all          # span every indexed host
  docs-index search "..." --site host.com    # one-shot filter
  docs-index cat /pages/get-started/about-the-api
  docs-index cat about-the-api               # fuzzy substring match
  docs-index tree                            # host + path + title
  docs-index urls                            # raw URL list (pipe-friendly)
  docs-index sites                           # hosts indexed + page counts
  docs-index stats                           # aggregate counts + last sync

Active docs (default filter for search/cat/tree/urls):
  docs-index use docs.equalsmoney.com
  docs-index use --clear

Maintenance:
  docs-index prune                       # drop noise rows + dedupe canon URLs
  docs-index clear <host> [--yes]        # wipe one site only
  docs-index reset [--yes]               # wipe the entire index

Serving to Claude / agents:
  docs-index mcp                         # stdio MCP server (12 tools)

Storage:  ~/.docs-index/index.db
Optional deps:  beautifulsoup4 (better HTML cleanup), html2text (HTML->md
                fallback). Both pip-installable; script works without them.

Discovery chain (fast to slow): sitemap.xml -> robots.txt sitemap ->
llms.txt -> mint.json/docs.json -> Wayback Machine CDX -> BFS crawl.

Each sync prefers <url>.md when the docs platform publishes raw markdown
source (most Mintlify and Docusaurus sites do); falls back to HTML
extraction with aggressive nav/chrome stripping.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

try:
    import html2text
    HAVE_H2T = True
except Exception:
    HAVE_H2T = False

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False

USER_AGENT = "docs-index/0.2 (+local-indexer)"
DB_PATH = Path.home() / ".docs-index" / "index.db"
REQUEST_TIMEOUT = 30
DEFAULT_WORKERS = 16
DEFAULT_MAX_PAGES = 5000

# URLs that look like assets/auth/Gatsby plumbing, not real docs pages.
NOISE_RE = re.compile(
    r"(?ix)"
    r"\.(?:js|css|svg|png|jpe?g|gif|webp|ico|woff2?|ttf|otf|eot|map"
    r"|webmanifest|xml|zip|map|json)(?:[?#]|$)"
    r"|/login(?:[?/]|$)"
    r"|/page-data/"
    r"|/_next/"
    r"|/static/"
    r"|/icons?/"
    r"|/images?/"
    r"|/manifest\."
    r"|\.bundle\.js"
    r"|\.chunk\.js"
)

def is_doc_url(u):
    return not NOISE_RE.search(u)


def canonicalize_url(u):
    """Strip trailing slash (except root) so /foo and /foo/ are stored once."""
    try:
        p = urllib.parse.urlparse(u)
    except Exception:
        return u
    path = p.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urllib.parse.urlunparse((p.scheme, p.netloc, path, p.params, p.query, ""))


def get_active_site(conn):
    r = conn.execute("SELECT value FROM meta WHERE key='active_site'").fetchone()
    return r["value"] if r and r["value"] else None


def set_active_site(conn, site):
    if site:
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('active_site', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (site,))
    else:
        conn.execute("DELETE FROM meta WHERE key='active_site'")
    conn.commit()


def resolve_site(args, conn):
    """Pick which host to filter on.
    Explicit --site wins; --all forces no filter; otherwise active_site.
    """
    if getattr(args, "all_sites", False):
        return None
    if getattr(args, "site", None):
        return args.site
    return get_active_site(conn)


# ---------- markdown + section extraction ----------

_CHROME_TAGS = ("script", "style", "nav", "header", "footer", "aside",
                "noscript", "svg", "form", "iframe")
_CHROME_ROLES = ("navigation", "banner", "contentinfo", "complementary",
                 "search", "tablist", "dialog")
_CHROME_CLS_PATTERNS = (
    "sidebar", "side-nav", "navigation", "navbar", "topbar",
    "footer", "header-", "site-header",
    "breadcrumb", "table-of-contents", "toc", "on-this-page",
    "feedback", "page-helpful", "was-this", "edit-page",
    "search-bar", "search-box", "skip-link", "anchor-link",
    "doc-footer", "site-footer", "scroll-to-top",
    "cookie", "banner-ad",
)


def _clean_doc_soup(soup):
    """Aggressively strip nav/chrome elements from a BeautifulSoup tree.
    Handles semantic tags AND class/role-based nav (Mintlify, Docusaurus).
    """
    for tag in soup(list(_CHROME_TAGS)):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": list(_CHROME_ROLES)}):
        tag.decompose()
    for tag in list(soup.find_all(class_=True)):
        cls_list = tag.get("class") or []
        cls = " ".join(cls_list).lower()
        if any(p in cls for p in _CHROME_CLS_PATTERNS):
            tag.decompose()
    # also strip elements whose id contains the same patterns
    for tag in list(soup.find_all(id=True)):
        tid = (tag.get("id") or "").lower()
        if any(p in tid for p in _CHROME_CLS_PATTERNS):
            tag.decompose()


def _find_main_content(soup):
    """Pick the element most likely to hold the actual article body."""
    for sel in [
        ("article", {}),
        ("main", {}),
        ("div",  {"class": "prose"}),
        ("div",  {"class": "content"}),
        ("div",  {"id":    "content"}),
        ("div",  {"role":  "main"}),
    ]:
        tag = soup.find(sel[0], **sel[1]) if sel[1] else soup.find(sel[0])
        if tag and len(tag.get_text(strip=True)) > 200:
            return tag
    return soup.body or soup


def html_to_markdown(body):
    """Convert page HTML to markdown. Uses html2text + bs4 cleanup when
    available; otherwise falls back to plain-text extraction.
    Returns (markdown_text, title).
    """
    if HAVE_H2T:
        try:
            title = None
            if HAVE_BS4:
                soup = BeautifulSoup(body, "html.parser")
                t = soup.find("title")
                if t:
                    title = t.get_text(strip=True)
                _clean_doc_soup(soup)
                main = _find_main_content(soup)
                src = str(main)
            else:
                src = body.decode("utf-8", errors="ignore")
            h = html2text.HTML2Text()
            h.body_width = 0
            h.ignore_images = True
            h.ignore_links = False
            h.protect_links = True
            h.skip_internal_links = True
            h.single_line_break = True
            md = h.handle(src).strip()
            md = re.sub(r"\n{3,}", "\n\n", md)
            return md, title
        except Exception:
            pass
    text, title = html_to_text(body)
    return text, title


def clean_mintlify_mdx(md):
    """Strip Mintlify-specific MDX components and convert to clean markdown.
    Reduces verbosity and removes JSX-style tags that confuse LLMs.

    Transforms:
      <Note>x</Note> / <Info>x</Info> / <Warning>x</Warning>  -> blockquote
      <ParamField body="X" type="Y" required>desc</ParamField> -> - **X** (Y, required) -- desc
      <CodeGroup>...</CodeGroup> -> just the inner code blocks
      ```bash Sample request theme={null} -> ```bash
      Drops the duplicate "Response structure" placeholder block following
      a "Sample response" block.
    """
    if not md:
        return md

    # Strip theme={...} (and similar prop) from code fence info strings.
    md = re.sub(r"(^```[^\n]*)\s+theme=\{[^}]*\}", r"\1", md, flags=re.M)
    # Strip any other JSX-style {...} props on the fence line.
    md = re.sub(r"(^```[a-zA-Z0-9_+-]*[^\n]*?)\s+\{[^}]*\}",
                lambda m: re.sub(r"\s+\{[^}]*\}", "", m.group(0)),
                md, flags=re.M)
    # Normalize code fence info strings: keep just "```<lang>" and an
    # optional descriptive caption that follows the language.
    # (e.g. "```bash Sample request" -> "```bash") -- agents rarely need
    # the caption, and it clutters tool output.
    md = re.sub(r"^```([a-zA-Z0-9_+-]+)[ \t]+[^\n]+$",
                r"```\1", md, flags=re.M)

    # <Note>/<Info>/<Tip>/<Warning>/<Check>/<Danger> blocks -> blockquote
    def callout_to_blockquote(m):
        kind = m.group(1).capitalize()
        body = m.group(2).strip()
        body = re.sub(r"\n", "\n> ", body)
        return f"> **{kind}:** {body}"
    md = re.sub(
        r"<(Note|Info|Tip|Warning|Check|Danger|Caution)>([\s\S]*?)</\1>",
        callout_to_blockquote, md)

    # <ParamField body="name" type="..." [required]>desc</ParamField>
    # -> - **name** (type[, required]) -- desc
    def paramfield_to_li(m):
        attrs = m.group(1)
        body = m.group(2).strip()
        name = re.search(r'body=["\']([^"\']+)["\']', attrs)
        typ  = re.search(r'type=["\']([^"\']+)["\']', attrs)
        req  = " required" in attrs.lower() and not re.search(r'required=["\']false["\']', attrs)
        name_s = name.group(1) if name else "?"
        typ_s  = typ.group(1) if typ else ""
        meta   = typ_s + (", required" if req else "")
        meta_p = f" ({meta})" if meta else ""
        # Collapse multi-line bodies to a single line, keep prose tight
        body = re.sub(r"\s+", " ", body)
        return f"- **{name_s}**{meta_p} — {body}"
    md = re.sub(
        r"<ParamField\b([^>]*)>([\s\S]*?)</ParamField>",
        paramfield_to_li, md)

    # <ResponseField> mirrors ParamField in newer Mintlify
    md = re.sub(
        r"<ResponseField\b([^>]*)>([\s\S]*?)</ResponseField>",
        paramfield_to_li, md)

    # <CodeGroup>...</CodeGroup>: drop the wrapper, keep inner blocks.
    md = re.sub(r"<CodeGroup>\s*", "", md)
    md = re.sub(r"\s*</CodeGroup>", "", md)

    # <Card>, <Steps>, <Step>, <Tabs>, <Tab>, <Frame>, <Accordion> wrappers:
    # remove opening/closing tags, keep inner content.
    for tag in ("Card", "Cards", "Steps", "Step", "Tabs", "Tab", "Frame",
                "Accordion", "AccordionGroup", "Expandable", "Columns",
                "Column", "Section", "Update"):
        md = re.sub(rf"</?{tag}\b[^>]*>", "", md)

    # Drop "Response structure" placeholder blocks that immediately follow
    # a "Sample response" block — they're shape-only duplicates.
    md = re.sub(
        r"(```json\s+Sample response[\s\S]*?```)\s*```json\s+Response structure[\s\S]*?```",
        r"\1", md)

    # Tidy: collapse 3+ blank lines and strip trailing whitespace.
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


def split_markdown_sections(md, page_title):
    """Split a markdown document into sections at # / ## / ### boundaries.
    Returns list of {heading_path, heading, content, seq}.
    """
    if not md or not md.strip():
        return [{"heading_path": page_title or "(intro)",
                 "heading": page_title or "(intro)",
                 "content": "", "seq": 0}]

    sections = []
    heading_stack = []
    current_parts = []
    seq = [0]
    in_code = False

    def flush():
        text = "\n".join(current_parts).strip()
        if not text and not heading_stack:
            return
        sections.append({
            "heading_path": " > ".join(h for _, h in heading_stack) or "(intro)",
            "heading": heading_stack[-1][1] if heading_stack else "(intro)",
            "content": text,
            "seq": seq[0],
        })
        seq[0] += 1
        current_parts.clear()

    for line in md.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            current_parts.append(line)
            continue
        if not in_code:
            m = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
            if m:
                flush()
                level = len(m.group(1))
                heading = m.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, heading))
                continue
        current_parts.append(line)
    flush()

    if not sections:
        sections = [{"heading_path": page_title or "(intro)",
                     "heading": page_title or "(intro)",
                     "content": md, "seq": 0}]
    return sections


def extract_sections(html):
    """Split a page into sections at # / ## / ### heading boundaries
    after converting HTML to markdown. Each section's content is markdown.
    """
    md, page_title = html_to_markdown(html)
    return split_markdown_sections(md, page_title)


def _looks_like_markdown(body):
    """Cheap heuristic: is this body raw markdown, not HTML?"""
    try:
        s = body.decode("utf-8", errors="ignore")
    except Exception:
        return False
    head = s[:1024].lstrip()
    if not head:
        return False
    if "<!DOCTYPE" in head[:200].upper() or "<HTML" in head[:200].upper():
        return False
    if head.startswith("<") and "</html>" in s[-200:].lower():
        return False
    # markdown: starts with a heading, frontmatter, or has heading lines early
    if head.startswith("#") or head.startswith("---"):
        return True
    if re.search(r"\n#{1,3}\s+\S", s[:2000]):
        return True
    return False


def _md_title(text):
    for line in text.split("\n")[:30]:
        m = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if m:
            return m.group(1).strip()
    return None


# ---------- auto-sync helper ----------

_sync_lock = None
_syncing_hosts = set()
AUTO_SYNC_STALE_DAYS = 7

def _ensure_sync_lock():
    global _sync_lock
    if _sync_lock is None:
        import threading
        _sync_lock = threading.Lock()
    return _sync_lock


def maybe_auto_sync(conn, host):
    """If `host` hasn't been synced in AUTO_SYNC_STALE_DAYS, kick off a
    background re-sync. Non-blocking. Idempotent per-host while running.
    """
    lock = _ensure_sync_lock()
    with lock:
        if host in _syncing_hosts:
            return
        row = conn.execute(
            "SELECT MAX(fetched_at) AS t FROM pages WHERE host = ?", (host,)
        ).fetchone()
        if not row or not row["t"]:
            return
        try:
            ts = datetime.fromisoformat(row["t"])
        except Exception:
            return
        from datetime import timedelta
        if datetime.now(timezone.utc) - ts < timedelta(days=AUTO_SYNC_STALE_DAYS):
            return
        seed_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"seed:{host}",)
        ).fetchone()
        if not seed_row or not seed_row["value"]:
            return
        seed_url = seed_row["value"]
        _syncing_hosts.add(host)

    def runner():
        try:
            ns = argparse.Namespace(
                seed=seed_url, prefix_only=False, force=False,
                urls=None, seeds_file=None,
                workers=DEFAULT_WORKERS, max_pages=DEFAULT_MAX_PAGES,
                verbose=False)
            print(f"[auto-sync] {host} is stale; refreshing in background",
                  file=sys.stderr)
            cmd_sync(ns)
        except Exception as e:
            print(f"[auto-sync] {host}: {e}", file=sys.stderr)
        finally:
            with lock:
                _syncing_hosts.discard(host)

    import threading
    threading.Thread(target=runner, daemon=True).start()


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url           TEXT PRIMARY KEY,
    host          TEXT NOT NULL,
    path          TEXT NOT NULL,
    title         TEXT,
    content       TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL,
    bytes         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_host_idx ON pages(host);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    path, title, content,
    content='pages', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, path, title, content)
    VALUES (new.rowid, new.path, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, path, title, content)
    VALUES ('delete', old.rowid, old.path, old.title, old.content);
    INSERT INTO pages_fts(rowid, path, title, content)
    VALUES (new.rowid, new.path, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, path, title, content)
    VALUES ('delete', old.rowid, old.path, old.title, old.content);
END;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    page_url      TEXT NOT NULL,
    heading_path  TEXT,
    heading       TEXT,
    content       TEXT NOT NULL,
    seq           INTEGER
);
CREATE INDEX IF NOT EXISTS sections_page_idx ON sections(page_url);

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    heading, content, heading_path,
    content='sections', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO sections_fts(rowid, heading, content, heading_path)
    VALUES (new.id, new.heading, new.content, new.heading_path);
END;

CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
    INSERT INTO sections_fts(sections_fts, rowid, heading, content, heading_path)
    VALUES ('delete', old.id, old.heading, old.content, old.heading_path);
    INSERT INTO sections_fts(rowid, heading, content, heading_path)
    VALUES (new.id, new.heading, new.content, new.heading_path);
END;

CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
    INSERT INTO sections_fts(sections_fts, rowid, heading, content, heading_path)
    VALUES ('delete', old.id, old.heading, old.content, old.heading_path);
END;
"""


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the MCP server (readers) and `sync` (writer) coexist without
    # locking each other out. busy_timeout retries on contention.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
    if "host" not in cols:
        conn.execute("ALTER TABLE pages ADD COLUMN host TEXT")
        for row in conn.execute("SELECT url FROM pages"):
            h = urllib.parse.urlparse(row["url"]).netloc
            conn.execute("UPDATE pages SET host = ? WHERE url = ?", (h, row["url"]))
        conn.execute("CREATE INDEX IF NOT EXISTS pages_host_idx ON pages(host)")
        conn.commit()
    # Backfill: any page without sections gets one fallback section (whole text).
    missing = conn.execute(
        "SELECT url, title, content FROM pages "
        "WHERE url NOT IN (SELECT DISTINCT page_url FROM sections)"
    ).fetchall()
    for r in missing:
        conn.execute(
            "INSERT INTO sections(page_url, heading_path, heading, content, seq) "
            "VALUES (?, ?, ?, ?, 0)",
            (r["url"], r["title"] or "(intro)", r["title"] or "(intro)",
             r["content"] or ""),
        )
    if missing:
        conn.commit()
    return conn


def http_get(url, headers=None):
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        return e.code, dict(e.headers or {}), body


def discover_urls(seed_url, prefix_only=False, max_pages=DEFAULT_MAX_PAGES,
                  verbose=False):
    """Discovery — UNION of every method, deduped:
       1. /sitemap.xml + /sitemap_index.xml
       2. /robots.txt -> Sitemap: lines
       3. /llms.txt + /llms-full.txt
       4. /mint.json + /docs.json (Mintlify nav)
       5. Wayback Machine CDX API
       (BFS crawl runs only if every method above returned zero URLs.)

    Mintlify-style sites often expose manual /pages/* in mint.json while
    /api-reference/* pages are auto-generated and only visible via Wayback
    or the sitemap. Unioning catches both.
    """
    parsed = urllib.parse.urlparse(seed_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    has_path = parsed.path not in ("", "/")
    if prefix_only or has_path:
        prefix = seed_url if seed_url.endswith("/") else seed_url + "/"
    else:
        prefix = origin + "/"

    def _try(label, func):
        """Run one discovery method, return set of clean URLs (may be empty).
        Always logs an outcome line so silent failures are visible.
        """
        _DISCOVERY_LOG.clear()
        try:
            urls = func()
        except Exception as e:
            print(f"[discover] {label}: error: {e}", file=sys.stderr)
            return set()
        # Always surface the per-fetch diagnostics in verbose mode
        if verbose:
            for line in _DISCOVERY_LOG:
                print(f"[discover]   {line}", file=sys.stderr)
        if not urls:
            print(f"[discover] {label}: no URLs returned", file=sys.stderr)
            return set()
        kept = set()
        skipped_noise = []
        skipped_prefix = []
        for u in urls:
            cu = canonicalize_url(u)
            if not cu.startswith(prefix):
                skipped_prefix.append(cu)
                continue
            if not is_doc_url(cu):
                skipped_noise.append(cu)
                continue
            kept.add(cu)
        dropped = len(skipped_noise) + len(skipped_prefix)
        extra = f" ({dropped} filtered out)" if dropped else ""
        print(f"[discover] {label} -> {len(kept)} urls{extra}",
              file=sys.stderr)
        if verbose:
            for u in skipped_noise:
                print(f"[discover]  SKIP noise   {u}", file=sys.stderr)
            for u in skipped_prefix:
                print(f"[discover]  SKIP prefix  {u}", file=sys.stderr)
        return kept

    all_urls = set()
    for sm in ("/sitemap.xml", "/sitemap_index.xml"):
        all_urls |= _try(f"sitemap {sm}",
                         lambda sm=sm: _fetch_sitemap(origin + sm, origin))
    all_urls |= _try("robots.txt -> sitemap",
                     lambda: _fetch_sitemaps_from_robots(origin))
    for fname in ("/llms.txt", "/llms-full.txt"):
        all_urls |= _try(fname,
                         lambda fname=fname: _fetch_llms(origin + fname,
                                                          origin, prefix))
    for fname in ("/mint.json", "/docs.json"):
        all_urls |= _try(fname,
                         lambda fname=fname: _fetch_mintlify(origin + fname,
                                                              origin))
    all_urls |= _try("SPA pages (Next.js __NEXT_DATA__ + Gatsby page-data + HTML)",
                     lambda: _fetch_spa_pages(origin))
    all_urls |= _try("Playwright (JS-rendered nav)",
                     lambda: _fetch_playwright_pages(origin))
    all_urls |= _try("Wayback Machine CDX",
                     lambda: _fetch_wayback_cdx(parsed.netloc))

    if all_urls:
        print(f"[discover] union total: {len(all_urls)} unique URLs",
              file=sys.stderr)
        return sorted(all_urls)

    # Last resort: BFS crawl
    print(f"[discover] no fast methods returned URLs; BFS from {seed_url}",
          file=sys.stderr)
    return bfs_crawl(seed_url, prefix=prefix, max_pages=max_pages)


# Per-method outcome ring: appended by each _fetch_* helper.
# Consumed by discover_urls verbose output.
_DISCOVERY_LOG = []

def _diag(msg):
    _DISCOVERY_LOG.append(msg)


def _looks_like_html(body):
    """Detect HTML so we can skip SPA-fallback responses when XML/JSON expected."""
    if not body:
        return False
    head = body[:512].decode("utf-8", errors="ignore").lstrip().lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _fetch_sitemap(url, origin):
    # Ask for XML explicitly; many CDNs do content negotiation and would
    # otherwise return the SPA HTML shell.
    status, _, body = http_get(
        url, headers={"Accept": "application/xml, text/xml, */*;q=0.5"})
    if status != 200:
        _diag(f"{url}: HTTP {status}")
        return []
    if not body:
        _diag(f"{url}: empty body")
        return []
    if _looks_like_html(body):
        _diag(f"{url}: HTTP 200 but body is HTML (SPA fallback, not sitemap)")
        return []
    out = parse_sitemap(body, origin)
    _diag(f"{url}: HTTP 200, {len(body)} bytes, {len(out)} URLs parsed")
    return out


def _fetch_sitemaps_from_robots(origin):
    rurl = origin + "/robots.txt"
    status, _, body = http_get(rurl)
    if status != 200 or not body:
        _diag(f"{rurl}: HTTP {status}")
        return []
    sitemap_lines = []
    for ln in body.decode("utf-8", errors="ignore").splitlines():
        m = re.match(r"(?i)\s*Sitemap:\s*(\S+)", ln)
        if m:
            sitemap_lines.append(m.group(1))
    if not sitemap_lines:
        _diag(f"{rurl}: 200 but no Sitemap: lines")
        return []
    urls = []
    for sm in sitemap_lines:
        try:
            urls.extend(_fetch_sitemap(sm, origin))
        except Exception as e:
            _diag(f"{sm}: {e}")
    return urls


def _fetch_llms(url, origin, prefix):
    # Mintlify and friends serve different responses based on Accept;
    # ask for plain text / markdown so we don't get the SPA HTML shell.
    status, _, body = http_get(
        url, headers={"Accept": "text/plain, text/markdown, */*;q=0.5"})
    if status != 200:
        _diag(f"{url}: HTTP {status}")
        return []
    if not body:
        _diag(f"{url}: empty body")
        return []
    if _looks_like_html(body):
        _diag(f"{url}: HTTP 200 but body is HTML (SPA fallback, not llms.txt)")
        return []
    out = parse_llms_txt(body, origin, prefix)
    _diag(f"{url}: HTTP 200, {len(body)} bytes, "
          f"{len(out)} URLs parsed (prefix={prefix})")
    return out


def _fetch_mintlify(url, origin):
    """Mintlify config files have a `navigation` tree of page slugs. Flatten it."""
    status, _, body = http_get(
        url, headers={"Accept": "application/json, */*;q=0.5"})
    if status != 200:
        _diag(f"{url}: HTTP {status}")
        return []
    if not body:
        _diag(f"{url}: empty body")
        return []
    if _looks_like_html(body):
        _diag(f"{url}: HTTP 200 but body is HTML (SPA fallback, not JSON)")
        return []
    try:
        cfg = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception as e:
        _diag(f"{url}: 200 but JSON parse failed: {e}")
        return []
    pages = []

    def walk(node):
        if isinstance(node, str):
            slug = node.lstrip("/")
            pages.append(f"{origin}/{slug}")
        elif isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for key in ("pages", "navigation", "groups", "tabs", "anchors", "items"):
                if key in node:
                    walk(node[key])
    walk(cfg)
    _diag(f"{url}: HTTP 200, {len(body)} bytes, {len(pages)} URLs flattened")
    return pages


def _walk_for_paths(node, origin, out):
    """Recursively collect any string that looks like a site-relative path."""
    if isinstance(node, str):
        s = node.strip()
        if s.startswith("/") and not s.startswith("//") and not s.startswith("/_"):
            # plain site-absolute path
            out.add(origin + s.split("#", 1)[0])
        elif s.startswith(origin):
            out.add(s.split("#", 1)[0])
    elif isinstance(node, list):
        for x in node:
            _walk_for_paths(x, origin, out)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_for_paths(v, origin, out)


def _fetch_spa_pages(origin):
    """Discover URLs from SPA/Next.js/Gatsby sites that don't expose sitemap
    or llms files. Strategy:
      1. __NEXT_DATA__ JSON blob          (Pages Router Next.js)
      2. self.__next_f.push([n, "..."])   (App Router Next.js / Mintlify v3)
      3. /page-data/index/page-data.json  (Gatsby)
      4. Brute-force path-regex sweep of the entire HTML
    """
    urls = set()

    # Fetch home page HTML
    try:
        status, _, body = http_get(origin + "/")
    except Exception as e:
        _diag(f"{origin}/: error {e}")
        return []
    if status != 200 or not body:
        _diag(f"{origin}/: HTTP {status}")
        return []
    html = body.decode("utf-8", errors="ignore")
    _diag(f"{origin}/: HTTP 200, {len(body)} bytes")

    # 1. Pages Router Next.js
    m = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.+?)</script>',
        html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            before = len(urls)
            _walk_for_paths(data, origin, urls)
            _diag(f"__NEXT_DATA__: +{len(urls) - before} URLs")
        except Exception as e:
            _diag(f"__NEXT_DATA__ parse failed: {e}")

    # 2. App Router Next.js — self.__next_f.push payloads carry the React
    #    tree as a JSON-encoded string. Extract every path-shaped token.
    payloads = re.findall(
        r'self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*(["\'])((?:\\.|(?!\1).)*)\1',
        html, re.DOTALL)
    if payloads:
        before = len(urls)
        for _, payload in payloads:
            # The payload is a JS string with escaped quotes. We don't need to
            # un-escape rigorously — paths show up as either "/foo/bar" or
            # \"/foo/bar\". Match both forms.
            for pm in re.finditer(r'(?:\\")(/[A-Za-z0-9][\w./-]{3,})(?:\\")',
                                  payload):
                p = pm.group(1).split("#", 1)[0].split("?", 1)[0]
                urls.add(origin + p.rstrip("/"))
            for pm in re.finditer(r'"(/[A-Za-z0-9][\w./-]{3,})"', payload):
                p = pm.group(1).split("#", 1)[0].split("?", 1)[0]
                urls.add(origin + p.rstrip("/"))
        _diag(f"__next_f payloads ({len(payloads)}): "
              f"+{len(urls) - before} URLs")

    # 3. Gatsby's root page-data
    pd_url = origin + "/page-data/index/page-data.json"
    try:
        status2, _, body2 = http_get(pd_url)
        if status2 == 200 and body2 and not _looks_like_html(body2):
            try:
                data = json.loads(body2.decode("utf-8", errors="ignore"))
                before = len(urls)
                _walk_for_paths(data, origin, urls)
                _diag(f"{pd_url}: +{len(urls) - before} URLs")
            except Exception:
                pass
    except Exception:
        pass

    # 4. Brute-force path regex over the entire HTML. Catches embedded
    #    nav in any form we haven't specifically targeted above. The
    #    is_doc_url filter (run by _try) drops asset paths.
    before = len(urls)
    # quoted-path tokens, escaped or not: /pages/foo, /api-reference/bar
    for pm in re.finditer(r'(?:\\"|")(/[A-Za-z0-9][\w./-]{3,})(?:\\"|")', html):
        p = pm.group(1).split("#", 1)[0].split("?", 1)[0]
        # require at least one slash beyond the leading one
        if p.count("/") >= 2:
            urls.add(origin + p.rstrip("/"))
    if len(urls) > before:
        _diag(f"path-regex sweep: +{len(urls) - before} URLs "
              f"(total {len(urls)})")

    # 5. <a href="..."> as a final pass
    before = len(urls)
    for mm in re.finditer(r'href=["\']([^"\']+)["\']', html):
        href = mm.group(1).split("#", 1)[0]
        if href.startswith("/") and not href.startswith("//"):
            urls.add(origin + href)
        elif href.startswith(origin):
            urls.add(href)
    if len(urls) > before:
        _diag(f"anchor hrefs: +{len(urls) - before} URLs")

    _diag(f"spa pages total raw: {len(urls)}")
    return sorted(urls)


def _fetch_playwright_pages(origin):
    """Headless-browser discovery for JS-rendered SPAs (Mintlify v3,
    Next.js App Router, Gatsby, Docusaurus). Renders the home page,
    waits for hydration, clicks every visible nav toggle to expand
    collapsed sections, then extracts every <a href> from the live DOM.
    Falls through silently if Playwright isn't installed.
    """
    if not HAVE_PLAYWRIGHT:
        _diag("playwright: not installed (pip install playwright && "
              "playwright install chromium)")
        return []

    urls = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=USER_AGENT)
                page = ctx.new_page()
                page.goto(origin + "/", wait_until="networkidle",
                          timeout=30000)
                # Give React time to fully hydrate after networkidle
                page.wait_for_timeout(1500)

                # First pass: hrefs visible without expanding anything
                hrefs = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]'))"
                    "       .map(a => a.href)")
                for h in hrefs:
                    if h.startswith(origin):
                        urls.add(h.split("#", 1)[0])
                    elif h.startswith("/") and not h.startswith("//"):
                        urls.add(origin + h.split("#", 1)[0])
                first = len(urls)
                _diag(f"playwright initial render: {len(hrefs)} anchors, "
                      f"{first} internal URLs")

                # Expand collapsed nav: Mintlify uses <button aria-expanded="false">
                # inside <nav>. Click each (best-effort) and re-extract.
                try:
                    page.evaluate(
                        "() => { document.querySelectorAll('nav button,"
                        " [role=\"button\"], summary')"
                        "          .forEach(b => { try { b.click(); } catch(e){} }); }")
                    page.wait_for_timeout(1500)
                    hrefs2 = page.evaluate(
                        "() => Array.from(document.querySelectorAll('a[href]'))"
                        "       .map(a => a.href)")
                    added = 0
                    for h in hrefs2:
                        if h.startswith(origin):
                            u = h.split("#", 1)[0]
                            if u not in urls:
                                urls.add(u)
                                added += 1
                        elif h.startswith("/") and not h.startswith("//"):
                            u = origin + h.split("#", 1)[0]
                            if u not in urls:
                                urls.add(u)
                                added += 1
                    _diag(f"playwright after nav-expand: +{added} URLs "
                          f"(total {len(urls)})")
                except Exception as e:
                    _diag(f"playwright nav-expand failed: {e}")
            finally:
                browser.close()
    except Exception as e:
        _diag(f"playwright error: {e}")
        return []
    return sorted(urls)


def _fetch_wayback_cdx(netloc):
    """Ask Wayback Machine for every URL it has seen under this host.

    The CDX API returns JSON in the shape [["original"], ["url1"], ["url2"], ...].
    We collapse by urlkey (dedupe) and filter to 2xx responses.
    """
    api = (
        "https://web.archive.org/cdx/search/cdx"
        f"?url={urllib.parse.quote(netloc)}/*"
        "&output=json&fl=original&collapse=urlkey&filter=statuscode:200&limit=20000"
    )
    status, _, body = http_get(api)
    if status != 200 or not body:
        return []
    try:
        rows = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        return []
    out = set()
    for r in rows[1:]:  # skip the header row ["original"]
        if r and r[0]:
            u = r[0].split("#", 1)[0]
            # Wayback sometimes returns http:// when the live site is https://
            if u.startswith("http://"):
                out.add("https://" + u[len("http://"):])
            out.add(u)
    return sorted(out)


def parse_llms_txt(body, origin, prefix):
    """Extract URLs from an llms.txt / llms-full.txt file.

    Accepts:
      - [Title](https://docs.example.com/path)   absolute markdown link
      - [Title](/pages/foo)                      relative markdown link
      - https://docs.example.com/path            bare URL line
    """
    text = body.decode("utf-8", errors="ignore")
    urls = set()
    # markdown-style links — both absolute and relative paths
    for m in re.finditer(r"\]\(\s*([^)\s]+)\s*\)", text):
        href = m.group(1).strip()
        if href.startswith(("http://", "https://")):
            u = href.split("#", 1)[0]
        elif href.startswith("/"):
            u = origin + href.split("#", 1)[0]
        else:
            continue
        if u.startswith(prefix):
            urls.add(u)
    # bare URLs on their own line
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith(("http://", "https://")):
            u = ln.split()[0].split("#", 1)[0]
            if u.startswith(prefix):
                urls.add(u)
    return sorted(urls)


def parse_sitemap(body, origin):
    out = []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return out
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for sm in root.findall("sm:sitemap/sm:loc", ns):
        if sm.text:
            try:
                _, _, child = http_get(sm.text.strip())
                out.extend(parse_sitemap(child, origin))
            except Exception:
                pass
    for u in root.findall("sm:url/sm:loc", ns):
        if u.text:
            href = u.text.strip()
            if href.startswith(origin):
                out.append(href)
    return sorted(set(out))


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.links.append(v)


def bfs_crawl(seed_url, prefix, max_pages=DEFAULT_MAX_PAGES):
    seen = set()
    queue = [seed_url]
    out = []
    while queue and len(out) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            status, _, body = http_get(url)
        except Exception:
            continue
        if status != 200 or not body:
            continue
        out.append(url)
        ext = LinkExtractor()
        try:
            ext.feed(body.decode("utf-8", errors="ignore"))
        except Exception:
            continue
        for href in ext.links:
            absu = urllib.parse.urljoin(url, href).split("#", 1)[0]
            if absu.startswith(prefix) and absu not in seen:
                queue.append(absu)
    return sorted(set(out))


class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.title = None
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.SKIP:
            self._skip_depth += 1
        elif t == "title":
            self._in_title = True
        elif t in {"p", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif t == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title and self.title is None:
            self.title = data.strip()
            return
        s = data.strip()
        if s:
            self.parts.append(s + " ")


def html_to_text(html):
    if HAVE_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else None
            _clean_doc_soup(soup)
            main = _find_main_content(soup)
            text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
            return text, title
        except Exception:
            pass
    parser = TextExtractor()
    try:
        parser.feed(html.decode("utf-8", errors="ignore"))
    except Exception:
        return html.decode("utf-8", errors="ignore"), None
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, parser.title


def _fetch_one(url, prev_etag, prev_lm, force):
    url = canonicalize_url(url)
    headers = {}
    if not force:
        if prev_etag:
            headers["If-None-Match"] = prev_etag
        if prev_lm:
            headers["If-Modified-Since"] = prev_lm

    # Try raw-markdown source first (Mintlify/Docusaurus often expose <url>.md).
    # Skip the .md probe if the URL already ends in .md or a file extension.
    md_url = None
    if not re.search(r"\.[a-z0-9]{2,5}$", url):
        md_url = url + ".md"
    if md_url:
        try:
            status_md, resp_md, body_md = http_get(md_url, headers=headers)
        except Exception:
            status_md, resp_md, body_md = 0, {}, b""
        if status_md == 200 and body_md and _looks_like_markdown(body_md):
            md_text = clean_mintlify_mdx(body_md.decode("utf-8", errors="ignore"))
            title = _md_title(md_text)
            sections = split_markdown_sections(md_text, title)
            parsed = urllib.parse.urlparse(url)
            return {
                "url": url, "status": 200,
                "host": parsed.netloc, "path": parsed.path or "/",
                "title": title, "text": md_text, "sections": sections,
                "etag": resp_md.get("ETag") or resp_md.get("etag"),
                "last_mod": resp_md.get("Last-Modified") or resp_md.get("last-modified"),
                "bytes": len(body_md),
                "source": "md",
            }

    # Fall back to HTML
    try:
        status, resp_headers, body = http_get(url, headers=headers)
    except Exception as e:
        return {"url": url, "error": str(e)}
    if status == 304:
        return {"url": url, "status": 304}
    if status != 200:
        return {"url": url, "error": f"HTTP {status}"}
    md_text, title = html_to_markdown(body)
    md_text = clean_mintlify_mdx(md_text)
    sections = split_markdown_sections(md_text, title)
    parsed = urllib.parse.urlparse(url)
    return {
        "url": url, "status": 200,
        "host": parsed.netloc, "path": parsed.path or "/",
        "title": title, "text": md_text, "sections": sections,
        "etag": resp_headers.get("ETag") or resp_headers.get("etag"),
        "last_mod": resp_headers.get("Last-Modified") or resp_headers.get("last-modified"),
        "bytes": len(body),
        "source": "html",
    }


def cmd_sync(args):
    conn = open_db()
    seeds_urls = []
    if getattr(args, "seeds_file", None):
        with open(args.seeds_file) as fh:
            seeds_urls = [ln.strip() for ln in fh
                          if ln.strip() and not ln.startswith("#")]
        print(f"[sync] {len(seeds_urls)} URLs loaded from {args.seeds_file}",
              file=sys.stderr)

    discovered_urls = []
    if args.urls and not args.seed:
        discovered_urls = list(args.urls)
        print(f"[sync] using {len(discovered_urls)} explicit URLs", file=sys.stderr)
    elif args.seed:
        seed = args.seed
        if not seed.startswith(("http://", "https://")):
            seed = "https://" + seed
        discovered_urls = discover_urls(seed, prefix_only=args.prefix_only,
                             max_pages=args.max_pages,
                             verbose=getattr(args, "verbose", False))
        host = urllib.parse.urlparse(seed).netloc
        conn.execute(
            "INSERT INTO meta(key,value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"seed:{host}", seed),
        )
        conn.commit()

    # Union seeds-file + discovery, canonicalize, dedupe
    urls = sorted({canonicalize_url(u)
                   for u in (seeds_urls + discovered_urls) if u})
    if seeds_urls and discovered_urls:
        print(f"[sync] union: {len(seeds_urls)} seeds + "
              f"{len(discovered_urls)} discovered -> {len(urls)} unique",
              file=sys.stderr)

    if not urls:
        print("[sync] no URLs to index (provide a seed URL or --seeds-file)",
              file=sys.stderr)
        return 1
    workers = max(1, args.workers)
    print(f"[sync] {len(urls)} pages to consider, {workers} workers", file=sys.stderr)
    prev = {row["url"]: (row["etag"], row["last_modified"])
            for row in conn.execute("SELECT url,etag,last_modified FROM pages")}
    new = updated = skipped = failed = done = 0
    fetched_ts = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_one, u, *prev.get(u, (None, None)), args.force): u
                   for u in urls}
        verbose = getattr(args, "verbose", False)
        for fut in as_completed(futures):
            done += 1
            res = fut.result()
            url = res["url"]
            tag = ""
            if "error" in res:
                print(f"[sync]  ERR {url}: {res['error']}", file=sys.stderr)
                failed += 1
                tag = "ERR"
            elif res["status"] == 304:
                skipped += 1
                tag = "304"
            else:
                existed = url in prev
                conn.execute(
                    "INSERT INTO pages(url,host,path,title,content,etag,last_modified,fetched_at,bytes) "
                    "VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET host=excluded.host,path=excluded.path,"
                    "title=excluded.title,content=excluded.content,etag=excluded.etag,"
                    "last_modified=excluded.last_modified,fetched_at=excluded.fetched_at,"
                    "bytes=excluded.bytes",
                    (url, res["host"], res["path"], res["title"], res["text"],
                     res["etag"], res["last_mod"], fetched_ts, res["bytes"]),
                )
                # refresh sections for this page
                conn.execute("DELETE FROM sections WHERE page_url = ?", (url,))
                for s in res.get("sections", []):
                    conn.execute(
                        "INSERT INTO sections(page_url,heading_path,heading,content,seq) "
                        "VALUES(?,?,?,?,?)",
                        (url, s["heading_path"], s["heading"], s["content"], s["seq"]),
                    )
                if existed:
                    updated += 1
                    tag = "UPD"
                else:
                    new += 1
                    tag = "NEW"
            if verbose and tag and tag != "ERR":
                # ERR already printed above
                print(f"[sync]  {tag} [{done:>4}/{len(urls)}] {url}", file=sys.stderr)
            if done % 25 == 0 or done == len(urls):
                conn.commit()
                if not verbose:
                    print(f"[sync]  {done}/{len(urls)}  new={new} updated={updated} "
                          f"unchanged={skipped} failed={failed}", file=sys.stderr)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('last_sync', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    print(f"[sync] done. new={new} updated={updated} unchanged={skipped} failed={failed}",
          file=sys.stderr)
    return 0


def cmd_search(args):
    conn = open_db()
    site = resolve_site(args, conn)
    sql = (
        "SELECT p.host, p.path, p.title AS page_title, p.url, "
        "       s.heading, s.heading_path, s.content AS section_content, "
        "       snippet(sections_fts, 1, '<<', '>>', '...', 30) AS snip, "
        "       bm25(sections_fts) AS score "
        "FROM sections_fts "
        "JOIN sections s ON s.id = sections_fts.rowid "
        "JOIN pages p ON p.url = s.page_url "
        "WHERE sections_fts MATCH ? ")
    params = [args.query]
    if site:
        sql += "AND p.host = ? "
        params.append(site)
    sql += "ORDER BY score LIMIT ?"
    params.append(args.limit)
    if site and not getattr(args, "site", None):
        print(f"(filtered to active site: {site}; use --all to span hosts)",
              file=sys.stderr)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("(no matches)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
    else:
        for r in rows:
            page_title = r["page_title"] or "(untitled)"
            heading = r["heading"] or "(intro)"
            print(f"[{r['host']}] {r['path']}  -  {page_title}")
            print(f"    § {heading}")
            print(f"    {r['snip']}")
            print(f"    {r['url']}")
            print()
    return 0


def _unmangle_msys_path(target):
    """Git Bash (MSYS) on Windows rewrites a `/foo/bar` CLI arg into
    `C:/Program Files/Git/foo/bar` before the program sees it. Undo that
    when we recognise the prefix so `cat /pages/foo` works in Git Bash.
    """
    if not target:
        return target
    t = target.replace("\\", "/")
    msys_prefixes = (
        "C:/Program Files/Git/",
        "C:/Program Files (x86)/Git/",
    )
    for p in msys_prefixes:
        if t.startswith(p):
            return "/" + t[len(p):]
    return target


def cmd_cat(args):
    conn = open_db()
    site = resolve_site(args, conn)
    target = _unmangle_msys_path(args.path_or_url)
    where = "url = ? OR path = ? OR path = ?"
    params = (target, target, target.rstrip("/"))
    if site:
        where = "(" + where + ") AND host = ?"
        params = params + (site,)
    row = conn.execute(
        f"SELECT host,path,title,url,content,fetched_at FROM pages WHERE {where}",
        params,
    ).fetchone()
    if not row:
        like_w = "path LIKE ?"
        like_p = (f"%{target}%",)
        if site:
            like_w += " AND host = ?"
            like_p = like_p + (site,)
        row = conn.execute(
            f"SELECT host,path,title,url,content,fetched_at FROM pages "
            f"WHERE {like_w} ORDER BY length(path) LIMIT 1",
            like_p,
        ).fetchone()
    if not row:
        print(f"not found: {target}", file=sys.stderr)
        return 1
    print(f"# {row['title']}")
    print(f"# {row['url']}")
    print(f"# fetched: {row['fetched_at']}")
    print()
    print(row["content"])
    return 0


def cmd_tree(args):
    conn = open_db()
    site = resolve_site(args, conn)
    sql = "SELECT host,path,title FROM pages"
    params = ()
    if site:
        sql += " WHERE host = ?"
        params = (site,)
    sql += " ORDER BY host, path"
    for r in conn.execute(sql, params).fetchall():
        title = r["title"] or ""
        print(f"{r['host']:<32}  {r['path']:<60}  {title}")
    return 0


def cmd_urls(args):
    """Print every indexed URL, one per line. Pipe-friendly.
    Honors active site / --site / --all.
    """
    conn = open_db()
    site = resolve_site(args, conn)
    sql = "SELECT url FROM pages"
    params = ()
    if site:
        sql += " WHERE host = ?"
        params = (site,)
    sql += " ORDER BY url"
    for r in conn.execute(sql, params).fetchall():
        print(r["url"])
    return 0


def cmd_sites(args):
    conn = open_db()
    rows = conn.execute(
        "SELECT host, COUNT(*) AS pages, "
        "MIN(fetched_at) AS first_seen, MAX(fetched_at) AS last_seen "
        "FROM pages GROUP BY host ORDER BY pages DESC"
    ).fetchall()
    if not rows:
        print("(no sites indexed yet - run `sync <URL>`)", file=sys.stderr)
        return 1
    for r in rows:
        print(f"{r['host']:<32}  {r['pages']:>5} pages   last sync {r['last_seen']}")
    return 0


def cmd_clear(args):
    """Clear the index for one site only (pages + sections + seed config).
    Other sites and global settings are untouched.
    """
    conn = open_db()
    host = args.host
    if "://" in host:
        host = urllib.parse.urlparse(host).netloc
    host = host.strip("/")
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM pages WHERE host = ?", (host,)
    ).fetchone()["c"]
    if count == 0:
        print(f"no pages indexed for {host}", file=sys.stderr)
        return 1
    if not args.yes:
        ans = input(f"clear {count} pages for {host}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    conn.execute(
        "DELETE FROM sections WHERE page_url IN "
        "(SELECT url FROM pages WHERE host = ?)", (host,))
    conn.execute("DELETE FROM pages WHERE host = ?", (host,))
    conn.execute("DELETE FROM meta WHERE key = ?", (f"seed:{host}",))
    if get_active_site(conn) == host:
        set_active_site(conn, None)
        print(f"(active site was {host}; cleared)", file=sys.stderr)
    conn.commit()
    print(f"cleared {count} pages for {host}")
    return 0


def cmd_reset(args):
    """Wipe the local index entirely. Use --yes to skip confirmation."""
    import shutil
    target_dir = DB_PATH.parent
    if not target_dir.exists():
        print(f"nothing to remove at {target_dir}")
        return 0
    if not args.yes:
        ans = input(f"delete {target_dir} (every indexed page across every site)? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    try:
        shutil.rmtree(target_dir)
    except OSError as e:
        print(f"could not remove {target_dir}: {e}", file=sys.stderr)
        print("if a Claude MCP server is running, quit Claude desktop first.",
              file=sys.stderr)
        return 2
    print(f"removed {target_dir}")
    return 0


def cmd_use(args):
    """Show, set, or clear the active docs site (default filter for search/cat/list)."""
    conn = open_db()
    if args.clear:
        set_active_site(conn, None)
        print("active site cleared")
        return 0
    if args.host:
        host = args.host
        if "://" in host:
            host = urllib.parse.urlparse(host).netloc
        host = host.strip("/")
        hosts = {r["host"] for r in conn.execute("SELECT DISTINCT host FROM pages")}
        if host not in hosts:
            print(f"warning: '{host}' is not indexed yet. Known sites: "
                  + (", ".join(sorted(hosts)) or "(none)"), file=sys.stderr)
        set_active_site(conn, host)
        print(f"active site set to: {host}")
        return 0
    cur = get_active_site(conn)
    if cur:
        print(f"active site: {cur}")
    else:
        print("no active site set (searches span all indexed hosts)")
    return 0


def cmd_prune(args):
    """Clean the index:
       - drop noise rows (assets, login intercepts, Gatsby plumbing)
       - canonicalize URLs (strip trailing slash) and merge duplicates,
         keeping the variant with the largest content.
    """
    conn = open_db()

    # 1. noise removal
    bad = [r["url"] for r in conn.execute("SELECT url FROM pages")
           if not is_doc_url(r["url"])]
    for u in bad:
        conn.execute("DELETE FROM pages WHERE url = ?", (u,))
    if bad:
        print(f"pruned {len(bad)} noise URLs", file=sys.stderr)

    # 2. group every row by its canonical URL; for each group, keep the row
    #    with the most content and store it under the canonical URL.
    rows = list(conn.execute("SELECT url,bytes FROM pages"))
    groups = {}
    for r in rows:
        groups.setdefault(canonicalize_url(r["url"]), []).append(r["url"])
    merged = renamed = 0
    for canon, urls in groups.items():
        if len(urls) == 1 and urls[0] == canon:
            continue  # already canonical, single row, nothing to do

        # find the URL with the largest stored content
        sizes = {u: conn.execute(
            "SELECT bytes FROM pages WHERE url = ?", (u,)).fetchone()["bytes"] or 0
            for u in urls}
        winner = max(sizes, key=sizes.get)
        winner_row = conn.execute(
            "SELECT host,title,content,etag,last_modified,fetched_at,bytes "
            "FROM pages WHERE url = ?", (winner,)).fetchone()

        cp = urllib.parse.urlparse(canon)
        # remove every URL in the group and re-insert under the canonical form
        for u in urls:
            conn.execute("DELETE FROM pages WHERE url = ?", (u,))
        conn.execute(
            "INSERT INTO pages(url,host,path,title,content,etag,last_modified,fetched_at,bytes) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (canon, winner_row["host"], cp.path or "/", winner_row["title"],
             winner_row["content"], winner_row["etag"], winner_row["last_modified"],
             winner_row["fetched_at"], winner_row["bytes"]))
        if len(urls) > 1:
            merged += len(urls) - 1
        elif winner != canon:
            renamed += 1
    if merged:
        print(f"merged {merged} trailing-slash duplicates", file=sys.stderr)
    if renamed:
        print(f"renamed {renamed} URLs to canonical form", file=sys.stderr)
    if not bad and not merged and not renamed:
        print("nothing to prune", file=sys.stderr)
    conn.commit()
    return 0


def cmd_stats(args):
    conn = open_db()
    n = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
    h = conn.execute("SELECT COUNT(DISTINCT host) AS c FROM pages").fetchone()["c"]
    total = conn.execute("SELECT COALESCE(SUM(bytes),0) AS b FROM pages").fetchone()["b"]
    last = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print(f"sites:         {h}")
    print(f"pages indexed: {n}")
    print(f"raw bytes seen: {total:,}")
    print(f"db file: {DB_PATH} ({db_size:,} bytes)")
    print(f"last sync: {last['value'] if last else '(never)'}")
    return 0


MCP_ORCHESTRATION_GUIDE = """\
docs-index MCP — local search over any docs site, sectioned and markdown-native.

==============================
WORKFLOWS (when to use what)
==============================

1) ANSWERING A QUESTION ABOUT A SPECIFIC DOC SITE
   set_active_docs(site="docs.foo.com")        # sticky; survives across calls
   search_docs(query="<terms>")                 # returns ranked SECTIONS
       returns: [{ host, path, page_title, heading, heading_path,
                   section_content, snip, url, score }, ...]
   The full matched section's markdown is in `section_content`, so you can
   usually answer without a follow-up cat_doc. Use cat_doc only when you
   need siblings of the matched section or the entire page.

2) BROWSING / ORIENTATION
   list_sites()                                 # which hosts are indexed
   list_pages(site=..., path_prefix="/api-reference")
   index_stats()                                # total counts + last sync time

3) ADD A NEW DOCS SITE
   sync_site(url="https://docs.bar.com/")       # 10-60s for a typical site
   The tool returns counts; search/cat work immediately after it completes.

4) REFRESH STALE DOCS
   sync_site is also the refresh — re-running fetches only changed pages
   via ETag/Last-Modified. Stale sites (>7 days) trigger a background
   re-sync automatically the next time search_docs hits them, so you
   usually do not need to call this yourself.

5) RESET ONE SITE
   clear_site(site="docs.foo.com")              # wipes pages + sections
   sync_site(url="https://docs.foo.com/")       # rebuild clean

6) CLEAN NOISE FROM A LEGACY INDEX
   prune_index()                                # rarely needed; safe to skip

==============================
FILTER PRECEDENCE
==============================

For search_docs / cat_doc / list_pages, the host filter is resolved as:
    explicit `site` arg  >  `all_sites=true` (clears)  >  active_site  >  no filter

Default = active site if set, else no filter (cross-site).

==============================
FTS5 QUERY SYNTAX
==============================

search_docs queries are FTS5. Supports:
    "rate limit"           exact phrase
    webhook OR signature   either term
    webhook NOT card       boolean
    auth*                  prefix
    NEAR(a b, 10)          a and b within 10 tokens

==============================
COMMON PITFALLS
==============================

- After sync_site, future calls "just work" — no separate "reload" tool.
- set_active_docs sticks in the local DB; remember to call it with empty
  site to clear if the user switches topics across vendors.
- search_docs section_content is already the full section. Don't reflexively
  cat_doc the URL; that's only needed for adjacent content.
- If list_sites is empty, call sync_site first; nothing else will work.
- All read tools are cheap; favour calling search_docs + skim over one
  giant cat_doc.

==============================
TOOL INVENTORY
==============================
search_docs   cat_doc   list_sites   list_pages   index_stats
sync_site     clear_site   prune_index
get_active_docs   set_active_docs (pass site="" to clear)   help
"""


def cmd_mcp(args):
    conn = open_db()

    def reply(req_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()

    tools = [
        {"name": "search_docs",
         "description": "FTS5 search over the local docs index. Optional site filter.",
         "inputSchema": {"type": "object",
                          "properties": {"query": {"type": "string"},
                                          "site":  {"type": "string"},
                                          "limit": {"type": "integer", "default": 10}},
                          "required": ["query"]}},
        {"name": "cat_doc",
         "description": "Return the full text of one indexed page.",
         "inputSchema": {"type": "object",
                          "properties": {"path_or_url": {"type": "string"},
                                          "site":        {"type": "string"}},
                          "required": ["path_or_url"]}},
        {"name": "list_sites",
         "description": "List every host indexed locally with page counts.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "list_pages",
         "description": "Enumerate indexed pages. Use when search misses or you want to browse a section. Filter by site and/or path_prefix; results sorted by path. Returns up to `limit` rows.",
         "inputSchema": {"type": "object",
                          "properties": {"site":        {"type": "string"},
                                          "path_prefix": {"type": "string", "description": "e.g. /api-reference/cards"},
                                          "limit":       {"type": "integer", "default": 200}}}},
        {"name": "sync_site",
         "description": "Index a new docs URL (or re-sync an existing one) into the local store. Long-running: 10-60s for a typical docs site. Returns counts of new/updated/unchanged/failed pages.",
         "inputSchema": {"type": "object",
                          "properties": {"url":     {"type": "string", "description": "seed URL, e.g. https://docs.stripe.com/"},
                                          "workers": {"type": "integer", "default": 16}},
                          "required": ["url"]}},
        {"name": "prune_index",
         "description": "Clean the local index: drop noise rows (assets, login intercepts, page-data plumbing) and merge trailing-slash duplicates into canonical URLs. Use when the index looks polluted.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "index_stats",
         "description": "Aggregate counts and freshness of the local index: total sites, total pages, db file size, last-sync timestamp.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_active_docs",
         "description": "Return the currently active docs site (the default filter for search_docs/cat_doc/list_pages when no `site` is passed). Returns null if not set.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "set_active_docs",
         "description": "Set or clear the active docs site (default filter for search/cat/list). Pass `site` as the host (docs.foo.com) or URL to set, or as empty string / null to clear.",
         "inputSchema": {"type": "object",
                          "properties": {"site": {"type": "string"}},
                          "required": ["site"]}},
        {"name": "clear_site",
         "description": "Delete every indexed page and section for one host. Other sites and settings untouched. Pair with sync_site for a clean re-index.",
         "inputSchema": {"type": "object",
                          "properties": {"site": {"type": "string"}},
                          "required": ["site"]}},
        {"name": "help",
         "description": "Return a usage + orchestration guide for this MCP. Call this first if you're unsure which tool to use or how to chain them. Returns workflows, filter precedence, and common pitfalls.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}
        if method == "initialize":
            reply(rid, {"protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "docs-index", "version": "0.2"}})
        elif method == "tools/list":
            reply(rid, {"tools": tools})
        elif method == "tools/call":
            name = params.get("name")
            a = params.get("arguments") or {}
            try:
                if name == "search_docs":
                    sql = (
                        "SELECT p.host,p.path,p.title AS page_title,p.url,"
                        "s.heading,s.heading_path,s.content AS section_content,"
                        "snippet(sections_fts,1,'<<','>>','...',30) AS snip "
                        "FROM sections_fts "
                        "JOIN sections s ON s.id = sections_fts.rowid "
                        "JOIN pages p ON p.url = s.page_url "
                        "WHERE sections_fts MATCH ? ")
                    qp = [a["query"]]
                    site_filter = a.get("site") or get_active_site(conn)
                    if a.get("all_sites"):
                        site_filter = None
                    if site_filter:
                        sql += "AND p.host=? "
                        qp.append(site_filter)
                        try:
                            maybe_auto_sync(conn, site_filter)
                        except Exception:
                            pass
                    sql += "ORDER BY bm25(sections_fts) LIMIT ?"
                    qp.append(a.get("limit", 10))
                    rows = conn.execute(sql, qp).fetchall()
                    payload = [dict(r) for r in rows]
                elif name == "cat_doc":
                    t = a["path_or_url"]
                    site = a.get("site") or get_active_site(conn)
                    if a.get("all_sites"):
                        site = None
                    where = "url=? OR path=? OR path=?"
                    qp2 = (t, t, t.rstrip("/"))
                    if site:
                        where = "(" + where + ") AND host=?"
                        qp2 = qp2 + (site,)
                    r = conn.execute(
                        f"SELECT host,path,title,url,content FROM pages WHERE {where}",
                        qp2,
                    ).fetchone()
                    if not r:
                        lp = (f"%{t}%",); lw = "path LIKE ?"
                        if site:
                            lw += " AND host=?"
                            lp = lp + (site,)
                        r = conn.execute(
                            f"SELECT host,path,title,url,content FROM pages "
                            f"WHERE {lw} ORDER BY length(path) LIMIT 1", lp,
                        ).fetchone()
                    payload = dict(r) if r else None
                elif name == "list_sites":
                    rows = conn.execute(
                        "SELECT host, COUNT(*) AS pages, MAX(fetched_at) AS last_seen "
                        "FROM pages GROUP BY host ORDER BY pages DESC"
                    ).fetchall()
                    payload = [dict(r) for r in rows]
                elif name == "list_pages":
                    sql = "SELECT host,path,title FROM pages WHERE 1=1"
                    qp = []
                    site_filter = a.get("site") or get_active_site(conn)
                    if a.get("all_sites"):
                        site_filter = None
                    if site_filter:
                        sql += " AND host=?"
                        qp.append(site_filter)
                    if a.get("path_prefix"):
                        sql += " AND path LIKE ?"
                        qp.append(a["path_prefix"].rstrip("%") + "%")
                    sql += " ORDER BY host, path LIMIT ?"
                    qp.append(a.get("limit", 200))
                    rows = conn.execute(sql, qp).fetchall()
                    payload = [dict(r) for r in rows]
                elif name == "sync_site":
                    import argparse as _ap
                    ns = _ap.Namespace(seed=a["url"], prefix_only=False, force=False,
                                       urls=None, seeds_file=None,
                                       workers=int(a.get("workers", 16)),
                                       max_pages=DEFAULT_MAX_PAGES)
                    before = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
                    rc = cmd_sync(ns)
                    after = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
                    payload = {"exit_code": rc, "pages_before": before, "pages_after": after,
                               "site": urllib.parse.urlparse(a["url"]).netloc}
                elif name == "prune_index":
                    import argparse as _ap
                    before = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
                    cmd_prune(_ap.Namespace())
                    after = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
                    payload = {"pages_before": before, "pages_after": after,
                               "removed": before - after}
                elif name == "get_active_docs":
                    payload = {"active_site": get_active_site(conn)}
                elif name == "set_active_docs":
                    site = a.get("site") or ""
                    if site:
                        if "://" in site:
                            site = urllib.parse.urlparse(site).netloc
                        site = site.strip("/")
                    if site:
                        set_active_site(conn, site)
                        payload = {"active_site": site}
                    else:
                        set_active_site(conn, None)
                        payload = {"active_site": None}
                elif name == "help":
                    payload = {"guide": MCP_ORCHESTRATION_GUIDE}
                elif name == "clear_site":
                    site = a["site"]
                    if "://" in site:
                        site = urllib.parse.urlparse(site).netloc
                    site = site.strip("/")
                    before = conn.execute(
                        "SELECT COUNT(*) AS c FROM pages WHERE host = ?",
                        (site,)).fetchone()["c"]
                    conn.execute(
                        "DELETE FROM sections WHERE page_url IN "
                        "(SELECT url FROM pages WHERE host = ?)", (site,))
                    conn.execute("DELETE FROM pages WHERE host = ?", (site,))
                    conn.execute("DELETE FROM meta WHERE key = ?", (f"seed:{site}",))
                    if get_active_site(conn) == site:
                        set_active_site(conn, None)
                    conn.commit()
                    payload = {"site": site, "pages_removed": before}
                elif name == "index_stats":
                    n  = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()["c"]
                    h  = conn.execute("SELECT COUNT(DISTINCT host) AS c FROM pages").fetchone()["c"]
                    tb = conn.execute("SELECT COALESCE(SUM(bytes),0) AS b FROM pages").fetchone()["b"]
                    ls = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
                    payload = {
                        "sites": h, "pages": n, "raw_bytes_seen": tb,
                        "db_file": str(DB_PATH),
                        "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
                        "last_sync": ls["value"] if ls else None,
                    }
                else:
                    reply(rid, error={"code": -32601, "message": f"unknown tool {name}"})
                    continue
                reply(rid, {"content": [{"type": "text",
                                         "text": json.dumps(payload, indent=2)}]})
            except Exception as e:
                reply(rid, error={"code": -32000, "message": str(e)})
        else:
            reply(rid, error={"code": -32601, "message": f"unknown method {method}"})
    return 0


def main(argv=None):
    # Windows console defaults to cp1252 which can't encode many docs chars
    # (<=, =>, en-dash, arrows, emoji). Force UTF-8 on stdout/stderr so cat,
    # search, etc. never crash on non-Latin-1 characters.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="docs-index", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser(
        "sync",
        help="crawl + index pages under a docs URL",
        description=(
            "Discover and index every page under a docs URL.\n"
            "\n"
            "Discovery chain (fast -> slow): sitemap.xml, robots.txt sitemap,\n"
            "llms.txt, mint.json/docs.json, Wayback Machine CDX, BFS crawl.\n"
            "\n"
            "Each page is fetched as raw markdown when the platform exposes it\n"
            "at <url>.md (Mintlify, Docusaurus); otherwise HTML -> markdown via\n"
            "html2text. Re-runs are incremental (ETag/Last-Modified per page).\n"
            "\n"
            "Examples:\n"
            "  sync https://docs.equalsmoney.com/\n"
            "  sync https://docs.stripe.com/ --workers 32 -v\n"
            "  sync https://docs.foo.com/v2/ --prefix-only --force\n"
            "  sync --seeds-file urls.txt --workers 32"))
    ps.add_argument("seed", nargs="?",
                    help="seed URL, e.g. https://docs.equalsmoney.com/")
    ps.add_argument("--prefix-only", action="store_true",
                    help="only index URLs that start with the exact seed prefix")
    ps.add_argument("--force", action="store_true",
                    help="ignore cached etag/last-modified; re-fetch everything")
    ps.add_argument("--urls", nargs="*",
                    help="explicit URLs to sync (skip sitemap/BFS discovery)")
    ps.add_argument("--seeds-file",
                    help="path to a file with one URL per line; skip discovery")
    ps.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"concurrent fetches (default {DEFAULT_WORKERS})")
    ps.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                    help=f"safety cap on pages discovered (default {DEFAULT_MAX_PAGES})")
    ps.add_argument("-v", "--verbose", action="store_true",
                    help="print each URL as it is fetched, with NEW/UPD/304 status")
    ps.set_defaults(func=cmd_sync)

    psr = sub.add_parser(
        "search",
        help="FTS5 search over the local index",
        description=(
            "Section-level FTS5 search. Returns the matched section's full\n"
            "markdown content (not just a 12-word snippet), so one query is\n"
            "usually enough to answer.\n"
            "\n"
            "FTS5 syntax: phrase, AND, OR, NOT, prefix*, NEAR(x y, 10).\n"
            "  \"rate limit\"     exact phrase\n"
            "  webhook OR card  either term\n"
            "  auth*            prefix\n"
            "\n"
            "Default filter is the active site (see `use`). --all spans every\n"
            "host. --site picks a host for one call.\n"
            "\n"
            "Examples:\n"
            "  search \"rate limit\"\n"
            "  search webhook --site docs.equalsmoney.com\n"
            "  search idempotency --all --json"))
    psr.add_argument("query")
    psr.add_argument("--site", help="restrict to a single host")
    psr.add_argument("--all", dest="all_sites", action="store_true",
                     help="span all indexed hosts (ignore active site)")
    psr.add_argument("--limit", type=int, default=10)
    psr.add_argument("--json", action="store_true")
    psr.set_defaults(func=cmd_search)

    pc = sub.add_parser(
        "cat",
        help="print one page's text by path or URL",
        description=(
            "Print one indexed page's full markdown content.\n"
            "\n"
            "Accepts three forms:\n"
            "  cat https://docs.foo.com/pages/x   # full URL\n"
            "  cat /pages/x                       # path only\n"
            "  cat x                              # fuzzy substring (shortest match)\n"
            "\n"
            "Git Bash users: MSYS rewrites a leading-`/` arg into a Windows\n"
            "path. The script auto-undoes that for the common `/c/...` case,\n"
            "but the substring form is shortest and safest."))
    pc.add_argument("path_or_url")
    pc.add_argument("--site", help="restrict lookup to a single host")
    pc.add_argument("--all", dest="all_sites", action="store_true",
                    help="span all indexed hosts (ignore active site)")
    pc.set_defaults(func=cmd_cat)

    pt = sub.add_parser(
        "tree",
        help="list indexed paths",
        description=(
            "Print every indexed page as `host  path  title`, sorted.\n"
            "Useful for browsing a site's structure or piping into grep.\n"
            "\n"
            "Examples:\n"
            "  tree --site docs.equalsmoney.com | grep webhook\n"
            "  tree --all | wc -l"))
    pt.add_argument("--site", help="restrict to a single host")
    pt.add_argument("--all", dest="all_sites", action="store_true",
                    help="span all indexed hosts (ignore active site)")
    pt.set_defaults(func=cmd_tree)

    pu = sub.add_parser(
        "urls",
        help="print every indexed URL, one per line (pipe-friendly)",
        description=(
            "Print every indexed URL, one per line. Use for grep, wc, sort,\n"
            "or saving as a seeds-file backup.\n"
            "\n"
            "Examples:\n"
            "  urls --site docs.equalsmoney.com\n"
            "  urls --all | wc -l\n"
            "  urls --all | grep webhook\n"
            "  urls --all > my-seeds.txt"))
    pu.add_argument("--site", help="restrict to a single host")
    pu.add_argument("--all", dest="all_sites", action="store_true",
                    help="span all indexed hosts (ignore active site)")
    pu.set_defaults(func=cmd_urls)

    psi = sub.add_parser(
        "sites",
        help="list hosts indexed with page counts",
        description=(
            "One row per indexed host: page count + last-sync timestamp.\n"
            "Quick situational awareness — what do I have, when was it fresh?"))
    psi.set_defaults(func=cmd_sites)

    pst = sub.add_parser(
        "stats",
        help="aggregate stats",
        description=(
            "Total sites, total pages, raw bytes seen, db file size,\n"
            "and the last sync timestamp across all hosts."))
    pst.set_defaults(func=cmd_stats)

    ppr = sub.add_parser(
        "prune",
        help="remove noise URLs + canonicalize duplicates",
        description=(
            "Clean the index in two passes:\n"
            "  1) Delete rows matching the noise filter (assets, login\n"
            "     intercepts, page-data, static, icons, manifests).\n"
            "  2) Collapse trailing-slash duplicates into the canonical form,\n"
            "     keeping whichever row has the most content.\n"
            "\n"
            "Mostly idle now that discovery and sync apply these rules at\n"
            "ingest time; kept for legacy data and one-off cleanups."))
    ppr.set_defaults(func=cmd_prune)

    pus = sub.add_parser(
        "use",
        help="set / show / clear the active docs site",
        description=(
            "Set the active docs site so subsequent search/cat/tree/urls\n"
            "default to it. Equivalent to passing --site every time.\n"
            "\n"
            "Examples:\n"
            "  use                          # show current active site\n"
            "  use docs.equalsmoney.com     # set active\n"
            "  use --clear                  # back to cross-site searches"))
    pus.add_argument("host", nargs="?",
                     help="host or URL to mark active; omit to show current")
    pus.add_argument("--clear", action="store_true",
                     help="clear the active site so searches span all hosts")
    pus.set_defaults(func=cmd_use)

    prs = sub.add_parser(
        "reset",
        help="wipe the local index entirely",
        description=(
            "Delete the whole ~/.docs-index directory (DB + WAL files).\n"
            "Use when you want a clean slate across every host. Asks to\n"
            "confirm unless --yes is passed.\n"
            "\n"
            "If Claude desktop is running, fully quit it first — the MCP\n"
            "server holds the DB open and rm fails with 'busy'."))
    prs.add_argument("--yes", action="store_true",
                     help="skip the confirmation prompt")
    prs.set_defaults(func=cmd_reset)

    pcl = sub.add_parser(
        "clear",
        help="clear the index for one site only",
        description=(
            "Delete every page + section for one host. Other sites and\n"
            "your active-site setting (unless it pointed here) are kept.\n"
            "\n"
            "Common pattern: clear then re-sync for a clean rebuild.\n"
            "  clear docs.equalsmoney.com --yes\n"
            "  sync https://docs.equalsmoney.com/ --workers 32 -v"))
    pcl.add_argument("host", help="host or URL to clear, e.g. docs.equalsmoney.com")
    pcl.add_argument("--yes", action="store_true",
                     help="skip the confirmation prompt")
    pcl.set_defaults(func=cmd_clear)

    pm = sub.add_parser(
        "mcp",
        help="stdio MCP server",
        description=(
            "Run as a stdio MCP server. Exposes 11 tools so a Claude agent\n"
            "(or any MCP client) can search, browse, sync, and curate the\n"
            "local docs index from inside a conversation.\n"
            "\n"
            "Wire it up by adding this to claude_desktop_config.json:\n"
            "  {\"mcpServers\":{\"docs-index\":{\"command\":\"python\",\n"
            "    \"args\":[\"<path-to-this-file>\",\"mcp\"]}}}\n"
            "\n"
            "Tools: search_docs, cat_doc, list_sites, list_pages,\n"
            "sync_site, clear_site, prune_index, index_stats,\n"
            "get_active_docs, set_active_docs, help."))
    pm.set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
