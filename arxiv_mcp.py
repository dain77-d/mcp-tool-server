"""arXiv MCP server: lets an AI agent search arXiv and read papers section by section.

Tools
  search_papers(query, max_results, sort_by)  -> ranked list with ids
  get_paper(arxiv_id)                         -> metadata + abstract
  read_paper(arxiv_id, section)               -> table of contents, or one section's full text

Stdlib only apart from the MCP SDK. Run: python arxiv_mcp.py  (stdio transport)
"""
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from mcp.server import MCPServer

API = "https://export.arxiv.org/api/query"
HTML = "https://arxiv.org/html/{}"
UA = {"User-Agent": "mcp-tool-server/0.1 (arXiv reader for MCP clients)"}
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
MAX_SECTION_CHARS = 20_000  # keeps one tool result well inside a model's context

# new style 2305.18290(v3) or old style hep-th/9901001(v1)
ID_RE = re.compile(r"(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)?")

server = MCPServer(
    "arxiv-reader",
    instructions="Search arXiv with search_papers, then read_paper(id) for the table of contents "
    "and read_paper(id, section) for the full text of a section. Cite arXiv ids.",
)


# ---------- helpers (pure, tested offline) ----------
def normalize_id(raw: str) -> str:
    """Accept '2305.18290', 'arXiv:2305.18290v3' or an arxiv.org URL; reject anything else."""
    m = ID_RE.search(raw.strip())
    if not m:
        raise ValueError(f"not an arXiv id: {raw!r}")
    return m.group(0)


def fetch(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_feed(xml: str) -> list[dict]:
    papers = []
    for e in ET.fromstring(xml).findall("a:entry", NS):
        text = lambda tag: " ".join((e.findtext(tag, "", NS) or "").split())
        papers.append(dict(
            id=normalize_id(text("a:id")),
            title=text("a:title"),
            authors=[" ".join(a.findtext("a:name", "", NS).split()) for a in e.findall("a:author", NS)],
            published=text("a:published")[:10],
            updated=text("a:updated")[:10],
            categories=[c.get("term") for c in e.findall("a:category", NS)],
            summary=text("a:summary"),
        ))
    return papers


def build_query(query: str) -> str:
    """Plain words -> all:w1 AND all:w2 (arXiv ORs bare words, which buries the relevant papers).
    Queries already using field syntax (ti:, au:, cat:, AND, quotes ...) pass through untouched."""
    q = query.strip()
    if ":" in q or '"' in q or re.search(r"\b(AND|OR|ANDNOT)\b", q):
        return q
    return " AND ".join(f"all:{w}" for w in q.split())


def authors_str(p: dict, limit: int = 4) -> str:
    a = p["authors"]
    return ", ".join(a[:limit]) + (f" et al. ({len(a)} authors)" if len(a) > limit else "")


class _PaperHTML(HTMLParser):
    """arXiv's LaTeXML HTML -> {title, abstract, sections:[(num, title, text)]}. Math becomes $latex$."""

    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "table", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.abstract, self.sections = "", "", []
        self._buf, self._target = [], None  # where text goes: "title" | "abstract" | "section"
        self._section_depth = self._depth = 0
        self._math = self._skip = 0
        self._skip_tag = ""
        self._in_heading = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if self._math:
            self._math += tag == "math"
            return
        if tag == "math":
            self._math = 1
            self._buf.append(f" ${a.get('alttext', '')}$ ")
            return
        if self._skip:  # inside a skipped element: only track nesting of the same tag
            self._skip += tag == self._skip_tag
            return
        if tag in ("script", "style", "nav", "header", "footer") or "ltx_bibliography" in cls:
            self._skip, self._skip_tag = 1, tag
            return
        if tag == "h1" and "ltx_title_document" in cls:
            self._flush()
            self._target = "title"
        elif tag == "div" and "ltx_abstract" in cls and not self.abstract:
            self._flush()
            self._target, self._depth = "abstract", 1
            return
        elif tag == "section" and {"ltx_section", "ltx_appendix"} & set(cls.split()):
            self._flush()
            self._target, self._section_depth = "section", 1
            self.sections.append([a.get("id", "").lstrip("S"), "", ""])
            return
        elif tag == "section" and self._section_depth:
            self._section_depth += 1
        if tag == "div" and self._target == "abstract":
            self._depth += 1
        if tag == "h2" and self._target == "section" and self._section_depth == 1 and not self.sections[-1][1]:
            self._in_heading = True
        if tag in self.BLOCK:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if self._math:
            self._math -= tag == "math"
            return
        if self._skip:
            self._skip -= tag == self._skip_tag
            return
        if tag == "h1" and self._target == "title":
            self.title = self._take()
            self._target = None
        elif tag == "div" and self._target == "abstract":
            self._depth -= 1
            if not self._depth:
                self.abstract = re.sub(r"^Abstract\s*", "", self._take())
                self._target = None
        elif tag == "h2" and self._in_heading:
            self.sections[-1][1] = self._take()
            self._in_heading = False
        elif tag == "section" and self._section_depth:
            self._section_depth -= 1
            if not self._section_depth:
                self.sections[-1][2] = self._take()
                self._target = None
        if tag in self.BLOCK:
            self._buf.append("\n")

    def handle_data(self, data):
        if not self._math and not self._skip and self._target:
            self._buf.append(data)

    def _take(self) -> str:
        text = "".join(self._buf)
        self._buf = []
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return re.sub(r"\s*\n\s*(\n\s*)+", "\n\n", text).strip()

    def _flush(self):
        self._buf = []


def parse_paper_html(html: str) -> dict:
    p = _PaperHTML()
    p.feed(html)
    sections = [(num, re.sub(r"^\d+\s*", "", title) or f"Section {num}", text) for num, title, text in p.sections]
    return dict(title=" ".join(p.title.split()), abstract=p.abstract, sections=sections)


def pick_section(sections: list, query: str):
    q = query.strip().lower().lstrip("s").rstrip(".")
    for s in sections:  # exact number first ("3", "S3", "3.")
        if s[0].lower() == q:
            return s
    return next((s for s in sections if query.strip().lower() in s[1].lower()), None)  # then title substring


# ---------- tools ----------
@server.tool()
def search_papers(query: str, max_results: int = 5, sort_by: str = "relevance") -> str:
    """Search arXiv. Plain words must all match (e.g. 'direct preference optimization'); arXiv field syntax
    also works: ti:, abs:, au:, cat:, quoted phrases, AND/OR (e.g. 'ti:"diffusion" AND cat:cs.CV').
    sort_by: relevance | submittedDate | lastUpdatedDate. Returns ids to pass to get_paper / read_paper."""
    if sort_by not in ("relevance", "submittedDate", "lastUpdatedDate"):
        return "sort_by must be relevance, submittedDate or lastUpdatedDate"
    params = dict(search_query=build_query(query), start=0, max_results=max(1, min(int(max_results), 25)), sortBy=sort_by)
    papers = parse_feed(fetch(f"{API}?{urllib.parse.urlencode(params)}"))
    if not papers:
        return f"No results for {query!r}."
    return "\n\n".join(
        f"{i}. **{p['title']}** — `{p['id']}` ({p['published']})\n   {authors_str(p, 3)}\n   {p['summary'][:280]}…"
        for i, p in enumerate(papers, 1)
    )


@server.tool()
def get_paper(arxiv_id: str) -> str:
    """Metadata and full abstract for one paper (id like 2305.18290 or an arxiv.org URL)."""
    pid = normalize_id(arxiv_id)
    papers = parse_feed(fetch(f"{API}?{urllib.parse.urlencode(dict(id_list=pid))}"))
    if not papers:
        return f"No paper found for {pid}."
    p = papers[0]
    return (
        f"# {p['title']}\n\n- id: `{p['id']}`\n- authors: {authors_str(p, 12)}\n"
        f"- published: {p['published']} (updated {p['updated']})\n- categories: {', '.join(p['categories'])}\n"
        f"- abs: https://arxiv.org/abs/{p['id']} · pdf: https://arxiv.org/pdf/{p['id']}\n\n## Abstract\n{p['summary']}"
    )


@server.tool()
def read_paper(arxiv_id: str, section: str = "") -> str:
    """Read a paper's full text from arXiv's HTML version.
    Without `section`: title, abstract and the table of contents.
    With `section`: that section's full text — pass its number ('3') or part of its title ('Experiments').
    Math is returned as inline LaTeX ($...$)."""
    pid = normalize_id(arxiv_id)
    try:
        doc = parse_paper_html(fetch(HTML.format(pid)))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"No HTML full text for {pid} (older or non-LaTeX submission). PDF: https://arxiv.org/pdf/{pid}"
        raise
    if not doc["sections"]:
        return f"Could not find sections in the HTML of {pid}. PDF: https://arxiv.org/pdf/{pid}"
    if not section:
        toc = "\n".join(f"{n}. {t}  ({len(x):,} chars)" for n, t, x in doc["sections"])
        return f"# {doc['title']}\n\n## Abstract\n{doc['abstract']}\n\n## Sections\n{toc}\n\nCall read_paper with section=<number or title>."
    hit = pick_section(doc["sections"], section)
    if not hit:
        return f"No section matching {section!r}. Available: " + "; ".join(f"{n}. {t}" for n, t, _ in doc["sections"])
    num, title, text = hit
    if len(text) > MAX_SECTION_CHARS:
        text = text[:MAX_SECTION_CHARS] + f"\n\n[… truncated, {len(hit[2]) - MAX_SECTION_CHARS:,} more chars]"
    return f"# {doc['title']}\n## {num}. {title}\n\n{text}"


def main():
    server.run()


if __name__ == "__main__":
    main()
