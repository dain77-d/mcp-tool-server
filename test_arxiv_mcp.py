"""Offline tests: parsing + the MCP tool surface (no network). Run: python test_arxiv_mcp.py"""
import asyncio

from arxiv_mcp import build_query, normalize_id, parse_feed, parse_paper_html, pick_section, server

FEED = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2305.18290v3</id>
    <title>Direct Preference
      Optimization</title>
    <published>2023-05-29T17:57:46Z</published><updated>2024-07-29T22:26:36Z</updated>
    <summary>  RLHF is complex.  </summary>
    <author><name>Rafael Rafailov</name></author><author><name>Archit Sharma</name></author>
    <category term="cs.LG"/><category term="cs.AI"/>
  </entry>
</feed>"""

HTML = """<html><head><script>var x = "<p>no</p>";</script></head><body>
<nav>menu</nav>
<h1 class="ltx_title ltx_title_document">A <em>Tiny</em>
Paper</h1>
<div class="ltx_abstract"><h6>Abstract</h6><p>We study <math alttext="x^2"><mi>x</mi></math> things.</p></div>
<section id="S1" class="ltx_section"><h2 class="ltx_title ltx_title_section"><span>1 </span>Introduction</h2>
  <div class="ltx_para"><p>Hello world.</p></div>
  <section id="S1.SS1" class="ltx_subsection"><h3>1.1 Sub</h3><p>Nested text.</p></section>
</section>
<section id="S2" class="ltx_section"><h2 class="ltx_title ltx_title_section">2 Experiments</h2><p>Loss <math alttext="\\mathcal{L}"><mi>L</mi><mo>x</mo></math> drops.</p></section>
<section id="bib" class="ltx_bibliography"><section><p>[1] cite me</p></section></section>
<section id="A1" class="ltx_appendix"><h2>Appendix A Proofs</h2><p>QED.</p></section>
</body></html>"""


def test_normalize_id():
    assert normalize_id("2305.18290") == "2305.18290"
    assert normalize_id("arXiv:2305.18290v3") == "2305.18290v3"
    assert normalize_id("https://arxiv.org/abs/1706.03762") == "1706.03762"
    assert normalize_id("hep-th/9901001") == "hep-th/9901001"
    for bad in ("", "hello", "../../etc/passwd", "12.34"):
        try:
            normalize_id(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_parse_feed():
    (p,) = parse_feed(FEED)
    assert p["id"] == "2305.18290v3" and p["title"] == "Direct Preference Optimization"
    assert p["authors"] == ["Rafael Rafailov", "Archit Sharma"] and p["categories"] == ["cs.LG", "cs.AI"]
    assert p["published"] == "2023-05-29" and p["summary"] == "RLHF is complex."


def test_parse_html():
    d = parse_paper_html(HTML)
    assert d["title"] == "A Tiny Paper"
    assert d["abstract"] == "We study $x^2$ things."
    assert [(n, t) for n, t, _ in d["sections"]] == [("1", "Introduction"), ("2", "Experiments"), ("A1", "Appendix A Proofs")]
    intro = d["sections"][0][2]
    assert "Hello world." in intro and "Nested text." in intro  # subsections stay in their parent
    assert "$\\mathcal{L}$" in d["sections"][1][2] and "Lx" not in d["sections"][1][2]  # math -> latex only
    full = str(d)
    assert "cite me" not in full and "menu" not in full and "no</p>" not in full


def test_pick_section():
    s = parse_paper_html(HTML)["sections"]
    assert pick_section(s, "2")[1] == "Experiments"
    assert pick_section(s, "S1")[1] == "Introduction"
    assert pick_section(s, "experi")[1] == "Experiments"
    assert pick_section(s, "a1")[1] == "Appendix A Proofs"
    assert pick_section(s, "nope") is None


def test_build_query():
    assert build_query("direct preference  optimization") == "all:direct AND all:preference AND all:optimization"
    assert build_query('ti:"vision transformer"') == 'ti:"vision transformer"'
    assert build_query("diffusion AND cat:cs.CV") == "diffusion AND cat:cs.CV"


def test_tools_are_exposed():
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"search_papers", "get_paper", "read_paper"}, names


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
