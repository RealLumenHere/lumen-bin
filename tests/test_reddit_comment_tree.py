"""Unit tests for _reddit_comment_tree() in fleet-search (wq#1002 build, wq#1007 harden).

Fixtures are real-shaped old.reddit HTML (synthetic, hand-built to match the live
DOM: div.thing.comment > div.entry > (p.tagline a.author) + form.usertext
div.usertext-body, nesting via div.child) under tests/fixtures/reddit/. Covers:
deep nesting, flat >cap threads, deleted/removed comments, load-more stubs,
malformed/truncated HTML, and empty/ratelimited responses.

Run: python3 -m pytest tests/test_reddit_comment_tree.py -v
     (or: python3 tests/test_reddit_comment_tree.py)
"""
import importlib.machinery
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")
FIXTURES = os.path.join(HERE, "fixtures", "reddit")


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()
tree = fs._reddit_comment_tree


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


# ---------- basic shape ----------

def test_simple_single_comment():
    html = ('<div class="thing comment"><a class="author">bob</a>'
            '<div class="usertext-body">hello world</div></div>')
    out = tree(html)
    assert out == [{"author": "bob", "depth": 0, "text": "hello world"}]


def test_no_comment_divs_returns_empty_list():
    assert tree("<html><body>nothing here</body></html>") == []


def test_empty_html_returns_empty_list():
    assert tree(_fixture("empty.html")) == []


def test_ratelimited_placeholder_returns_empty_list():
    # page fetched fine but the comment tree never rendered (ratelimit/empty case)
    assert tree(_fixture("ratelimited_empty.html")) == []


# ---------- deep nesting (wq#1007: deep-nested case) ----------

def test_deep_nested_captures_all_18_levels():
    out = tree(_fixture("deep_nested.html"), max_comments=100)
    assert len(out) == 18
    depths = [c["depth"] for c in out]
    assert depths == sorted(depths)              # strictly increasing with nesting
    assert depths[0] == 0
    assert depths[-1] == 17
    # the deepest reply is exactly the one trafilatura was reported to drop
    assert "EDL/dd of block devices" in out[-1]["text"]


def test_deep_nested_depth_is_capped_defensively():
    out = tree(_fixture("deep_nested.html"), max_comments=100)
    assert all(c["depth"] <= fs._MAX_TREE_DEPTH for c in out)


# ---------- flat / cap behavior ----------

def test_flat_thread_respects_max_comments_cap():
    out = tree(_fixture("flat_long.html"), max_comments=40)
    assert len(out) == 40
    assert all(c["depth"] == 0 for c in out)


def test_flat_thread_custom_cap():
    out = tree(_fixture("flat_long.html"), max_comments=5)
    assert len(out) == 5


# ---------- deleted / removed comments ----------

def test_deleted_and_removed_comments_handled():
    out = tree(_fixture("deleted_removed.html"), max_comments=100)
    authors = [c["author"] for c in out]
    # deleted-author comment tagged, mod-removed comment tagged, no-body comment
    # skipped entirely (nothing to show), the comment after the gap still parses
    assert "[deleted]" in authors
    assert "[removed]" in authors
    assert "carol" in authors
    assert "ghost" not in authors                # no usertext-body -> skipped, not crashed
    assert len(out) == 4                          # alice, deleted-child, removed, carol


def test_deleted_removed_texts_preserved():
    out = tree(_fixture("deleted_removed.html"), max_comments=100)
    by_author = {c["author"]: c["text"] for c in out}
    assert by_author["[deleted]"] == "[deleted]"
    assert by_author["[removed]"] == "[removed]"


def test_normal_comment_alongside_deleted_still_has_real_author():
    out = tree(_fixture("deleted_removed.html"), max_comments=100)
    alice = [c for c in out if c["author"] == "alice"]
    assert len(alice) == 1
    assert alice[0]["depth"] == 0


# ---------- load-more / morerecursion stubs ----------

def test_load_more_stubs_counted_not_expanded():
    out = tree(_fixture("load_more_stub.html"), max_comments=100)
    real = [c for c in out if c["author"] != "_more_stub"]
    stub = [c for c in out if c["author"] == "_more_stub"]
    assert len(real) == 2                          # dave (depth 0), erin (depth 1)
    assert len(stub) == 1
    assert stub[0]["depth"] is None
    assert "2 load-more" in stub[0]["text"]         # morechildren + morerecursion


def test_load_more_stub_omitted_once_cap_already_full():
    out = tree(_fixture("load_more_stub.html"), max_comments=2)
    assert len(out) == 2
    assert all(c["author"] != "_more_stub" for c in out)


# ---------- malformed / truncated HTML ----------

def test_malformed_truncated_html_never_raises():
    # must not raise; best-effort partial extraction is fine
    out = tree(_fixture("malformed_truncated.html"), max_comments=100)
    assert isinstance(out, list)


def test_malformed_truncated_html_recovers_the_clean_comment():
    out = tree(_fixture("malformed_truncated.html"), max_comments=100)
    authors = [c["author"] for c in out]
    assert "frank" in authors                      # the well-formed comment before the break


def test_garbage_input_never_raises():
    for bad in ["<div class=comment>", "<<<<<", "\x00\x01binary", "" , "a" * 50000]:
        out = tree(bad)
        assert isinstance(out, list)


def test_deeply_recursive_synthetic_children_never_raises():
    # pathological: thousands of nested .child wrappers with no real payload
    html = "<div class='commentarea'>" + ("<div class='child'>" * 3000) + "</div>" * 3000 + "</div>"
    out = tree(html)
    assert isinstance(out, list)                    # fail-soft, not a RecursionError


# ---------- import ImportError fail-soft path ----------

def test_import_error_path_returns_empty_list(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "bs4":
            raise ImportError("simulated: bs4 not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert tree(_fixture("flat_long.html")) == []
