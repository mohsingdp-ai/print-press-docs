# docs-index

A Cursor-style `@Docs` for the terminal. Point it at any docs URL and it
builds a local SQLite + FTS5 index of every page on that host. Search,
read, and serve it to Claude via MCP — without re-fetching, without
context blowup, without an embeddings provider.

- **Generic** — works with Mintlify, Docusaurus, Gatsby, GitBook, plain HTML.
- **Filesystem-shaped read API (MCP)** — `ls` walks the path tree, `cat`
  reads one page. No search tool. Navigate top-down; path segments
  self-describe on real docs sites.
- **Section-level FTS5 (CLI)** — the `search` CLI command returns the
  matched section's full markdown, not a 12-word snippet, so one query
  usually answers. (Kept as a CLI convenience; intentionally not exposed
  via MCP.)
- **Markdown-native storage** — prefers `<url>.md` source when the platform
  publishes it; otherwise converts HTML to markdown via `html2text`.
- **Cheap to keep fresh** — `ETag` / `Last-Modified` incremental sync, plus
  background auto-sync of stale sites on next read.
- **MCP server** — exposes 7 tools so an MCP-aware client (Claude desktop,
  Claude Code, etc.) can navigate the index as a filesystem (`ls` + `cat`)
  inside a conversation. No search tool by design — drill down by path.

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

Restart Claude. **7 tools** appear:

```
ls            cat            sync_site     clear_site
prune_index   index_stats    help
```

Read tools (`ls`, `cat`) treat the index as a filesystem:

```
ls("/")                                     -> indexed sites
ls("/docs.foo.com/")                        -> top-level structure of one site
ls("/docs.foo.com/api-reference/")          -> drill into a subtree
cat("/docs.foo.com/api-reference/foo")      -> read a page
```

There is no search tool — navigate top-down by path. Path segments in
real docs sites self-describe (e.g. `/api-reference/onboarding/respond-to-an-information-request`),
which is usually enough to find the right leaf in 2-3 hops. Call `help`
once at the start of any session for the orchestration playbook.

Typical agent flow:

> What's the right way to verify a webhook signature in the Equals API?

The agent calls `ls("/")` to see what's indexed, `ls("/docs.equalsmoney.com/pages/webhooks/")` to find the relevant section, and `cat("/docs.equalsmoney.com/pages/webhooks/create-webhooks")` to read the answer.

## How it stays cheap on context

- Stored content is **extracted markdown only** (script / style / nav /
  header / footer / aside / svg / sidebar / toc / feedback widgets are
  stripped at ingest, by tag, role, class, and id).
- `ls` returns just the immediate children of a path — directory names
  and one-line page titles — never page bodies. The full page only
  enters context when you `cat` it.
- `ls` derives directories from the path tree on the fly; no synthetic
  TOC pages to maintain, nothing to regenerate at sync time.
- Re-sync sends `If-None-Match` / `If-Modified-Since` per page — most of
  a weekly refresh is 304s.
- CLI `search` (kept for terminal use) returns ranked section excerpts,
  not whole pages, via FTS5 + `bm25`. Not exposed via MCP by design.

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


## Commands

```bash
cd /c/Mohsin/RnD-mock-projects/print-press-equals-docs
python docs-index.py clear docs.equalsmoney.com --yes
time python docs-index.py sync https://docs.equalsmoney.com/ --workers 32 --verbose 2>&1 | tail -20
```

```bash
python docs-index.py sites
```
# cross-vendor search — webhooks across both APIs
python docs-index.py search "webhook signature" --all

# set Stripe as the active site for follow-up questions
python docs-index.py use docs.equalsmoney.com
python docs-index.py search "webhook"

---

cd /c/Mohsin/RnD-mock-projects/print-press-equals-docs
python docs-index.py clear docs.equalsmoney.com --yes
python docs-index.py sync https://docs.equalsmoney.com/ --workers 32 --force -v 2>&1 | tail -5
python docs-index.py cat /pages/transactions/view-all-transactions | head -80