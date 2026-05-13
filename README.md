# docs-index

A Cursor-style `@Docs` for the terminal. Point it at any docs URL and it
builds a local SQLite + FTS5 index of every page on that host. Search,
read, and serve it to Claude via MCP — without re-fetching, without
context blowup, without an embeddings provider.

- **Generic** — works with Mintlify, Docusaurus, Gatsby, GitBook, plain HTML.
- **Section-level search** — FTS5 returns the matched section's full
  markdown, not a 12-word snippet, so one query usually answers.
- **Markdown-native storage** — prefers `<url>.md` source when the platform
  publishes it; otherwise converts HTML to markdown via `html2text`.
- **Cheap to keep fresh** — `ETag` / `Last-Modified` incremental sync, plus
  background auto-sync of stale sites on next read.
- **MCP server** — exposes 11 tools so an MCP-aware client (Claude desktop,
  Claude Code, etc.) can search and curate the index inside a conversation.

## Quick start

```bash
git clone https://github.com/mohsingdp-ai/print-press-docs.git
cd print-press-docs

# optional but recommended (cleaner HTML -> markdown extraction)
pip install --user beautifulsoup4 html2text

# optional, for client-rendered SPAs (Mintlify v3, Next.js App Router,
# Docusaurus, Gatsby) — uses a headless Chromium to discover the JS-only
# navigation that HTTP-only scraping can't see
pip install --user playwright
python -m playwright install chromium

# index any docs site
python docs-index.py sync https://docs.equalsmoney.com/ --workers 32 -v

# search locally (no web calls after sync)
python docs-index.py search "rate limit"
python docs-index.py cat about-the-api
python docs-index.py sites
```

`docs-index.py` is a single file. No build step, no third-party required
to run — every optional dep degrades gracefully if missing.

## CLI

Every subcommand has rich `--help`. Headline summary:

| Command          | Purpose |
| ---------------- | --- |
| `sync <URL>`     | Discover + index pages under URL. Incremental on re-run. |
| `search "<q>"`   | FTS5 search; returns matched sections (full markdown). |
| `cat <path>`     | Print one indexed page (full markdown). Fuzzy substring match supported. |
| `tree`           | List indexed paths with host + title. |
| `urls`           | Flat URL list, pipe-friendly. |
| `sites`          | Indexed hosts with page counts and last-sync time. |
| `stats`          | Aggregate counts and freshness. |
| `use <host>`     | Set the active site (default filter for search/cat/tree/urls). |
| `clear <host>`   | Wipe one site. |
| `reset`          | Wipe the entire index. |
| `prune`          | Drop noise rows + canonicalize trailing-slash duplicates. |
| `mcp`            | Run as stdio MCP server. |

Sync options worth knowing: `--workers N` (default 16), `--force`
(re-fetch everything), `--prefix-only` (scope to seed path), `-v`
(verbose per-URL output with NEW/UPD/304/ERR tags), `--seeds-file`
(explicit URL list, skip discovery).

Discovery chain, fast to slow: `/sitemap.xml`, `robots.txt` Sitemap
lines, `/llms.txt`, `/mint.json` + `/docs.json`, Wayback Machine CDX,
BFS crawl. Each page is then fetched as raw markdown from `<url>.md`
when the platform publishes it; otherwise HTML is converted to markdown
with aggressive nav/chrome stripping.

## MCP

After your first sync, expose the index to Claude desktop by adding this
to `claude_desktop_config.json` (location: `%APPDATA%\Claude\` on Windows,
`~/Library/Application Support/Claude/` on macOS):

```json
{
  "mcpServers": {
    "docs-index": {
      "command": "python",
      "args": ["/absolute/path/to/docs-index.py", "mcp"]
    }
  }
}
```

Restart Claude. **11 tools** appear:

```
search_docs       cat_doc           list_sites        list_pages
sync_site         clear_site        prune_index       index_stats
get_active_docs   set_active_docs   help
```

Call `help` once at the start of any session for the orchestration
playbook (workflows, filter precedence, FTS5 syntax, common pitfalls).

Typical agent flow:

> Set docs.stripe.com as the active docs.
> What's the right way to verify a webhook signature?

The agent calls `set_active_docs("docs.stripe.com")`, then `search_docs("verify webhook signature")`, and answers from the returned section's full markdown — no follow-up needed.

## How it stays cheap on context

- Stored content is **extracted markdown only** (script / style / nav /
  header / footer / aside / svg / sidebar / toc / feedback widgets are
  stripped at ingest, by tag, role, class, and id).
- Search returns ranked **section excerpts**, not whole pages. The full
  page only enters context when you `cat` it.
- Re-sync sends `If-None-Match` / `If-Modified-Since` per page — most of
  a weekly refresh is 304s.
- Section-level FTS5 with `bm25` ranking gives precise hits without
  embeddings.

## Storage

`~/.docs-index/index.db` (SQLite + FTS5, WAL mode). Plus sidecar
`.db-wal` and `.db-shm` files. Total footprint for a typical 200-page
docs site is ~2–3 MB.

## Examples

`examples/equals-money-urls.txt` is a real `--seeds-file` for
`docs.equalsmoney.com`. Useful when a docs site has no working
auto-discovery and you want to feed the URL list directly:

```bash
python docs-index.py sync \
  --seeds-file examples/equals-money-urls.txt \
  --workers 32 -v
```

## License

MIT — see [LICENSE](LICENSE).
