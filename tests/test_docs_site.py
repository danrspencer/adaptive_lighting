"""
Guards docs/_build/prepare.py, which is load-bearing for the docs site.

The site publishes docs/BLUEPRINT.md and docs/HELPERS.md, which are also
read directly on GitHub. Keeping both audiences happy means the published
copies are GENERATED - front matter prepended, cross-links rewritten -
rather than the source files being edited. Several of the rules that
makes necessary are silent when broken (a link that 404s only on the
site, a Jinja example that renders blank, a generated filename that
overwrites its own source), so they're pinned here.
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREPARE = REPO / "docs" / "_build" / "prepare.py"


def _load_prepare():
    spec = importlib.util.spec_from_file_location("docs_prepare", PREPARE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = _load_prepare()


def test_generated_names_are_not_case_variants_of_their_sources():
    """The macOS trap, pinned.

    macOS filesystems are case-insensitive, so docs/blueprint.md and
    docs/BLUEPRINT.md are the SAME FILE there - generating the former
    silently overwrites the source document, and the loss is only
    visible via git. Linux CI would never catch it, which is exactly why
    it needs a test rather than care.
    """
    for source_name, generated_name, *_ in prepare.PAGES:
        assert source_name.lower() != generated_name.lower(), (
            f"{generated_name!r} is a case variant of its own source {source_name!r} - "
            "on a case-insensitive filesystem, generating it would overwrite the source"
        )


def test_every_page_declares_a_distinct_permalink():
    permalinks = [permalink for *_, permalink in prepare.PAGES]
    assert len(permalinks) == len(set(permalinks))
    for permalink in permalinks:
        # Directory-style, so "../" from either page is the site root -
        # which is what the relative cross-links below rely on.
        assert permalink.startswith("/") and permalink.endswith("/")


def test_front_matter_disables_liquid():
    """Without this, Liquid eats the Jinja examples in both documents.

    Liquid and Jinja share the {{ }} delimiters, and Jekyll's default lax
    filter handling renders an unknown filter as an empty string - so
    `{{ today_at('06:00:00') | as_timestamp }}` would publish as blank,
    with no build error to notice.
    """
    fm = prepare.front_matter("Title", 4, "/somewhere/")
    assert "render_with_liquid: false" in fm
    assert fm.startswith("---\n") and fm.rstrip().endswith("---")


@pytest.mark.parametrize(
    "source_link, expected",
    [
        # Sibling reference docs -> the other one's permalink.
        ("see [helpers](HELPERS.md)", "see [helpers](../helpers/)"),
        ("see [x](HELPERS.md#override-protection)", "see [x](../helpers/#override-protection)"),
        ("see [b](BLUEPRINT.md)", "see [b](../blueprint/)"),
        ("see [b](BLUEPRINT.md#bring-your-own-sensor)", "see [b](../blueprint/#bring-your-own-sensor)"),
        # The README is the site's home page.
        ("see [readme](../README.md)", "see [readme](../)"),
        (
            "see [why](../README.md#why-four-phases-not-a-continuous-curve)",
            "see [why](../#why-four-phases-not-a-continuous-curve)",
        ),
        # Files that aren't published at all -> out to the repo.
        (
            "see [c](../CONTRIBUTING.md#previewing-the-dashboard-cards)",
            "see [c](https://github.com/danrspencer/adaptive_lighting/blob/main/CONTRIBUTING.md#previewing-the-dashboard-cards)",
        ),
        (
            "see [y](../dashboard/adaptive-lighting-section.yaml)",
            "see [y](https://github.com/danrspencer/adaptive_lighting/blob/main/dashboard/adaptive-lighting-section.yaml)",
        ),
    ],
)
def test_link_rewrites(source_link, expected):
    assert prepare.rewrite_links(source_link) == expected


def test_rewrites_leave_ordinary_text_and_anchors_alone():
    """Same-page anchors and external links must survive untouched -
    they're already correct on both GitHub and the site."""
    for unchanged in [
        "jump to [self](#self-healing)",
        "read [the study](https://pubmed.ncbi.nlm.nih.gov/36058557/)",
        "a literal BLUEPRINT.md mention outside a link",
    ]:
        assert prepare.rewrite_links(unchanged) == unchanged


def test_no_sibling_md_links_survive_in_the_generated_pages():
    """Whole-document check: after rewriting, nothing should still point
    at a sibling .md file, which would 404 on the published site."""
    for source_name, *_ in prepare.PAGES:
        rewritten = prepare.rewrite_links((REPO / "docs" / source_name).read_text(encoding="utf-8"))
        # Only the link TARGETS matter. Several links legitimately have
        # "docs/HELPERS.md" as their visible text while pointing at the
        # rewritten target, and that text is correct on both GitHub and
        # the site - matching on whole lines flags those as failures.
        targets = re.findall(r"\]\(([^)]+)\)", rewritten)
        stale = [t for t in targets if ".md" in t and "github.com" not in t]
        assert not stale, f"{source_name} still has site-relative .md links after rewriting:\n" + "\n".join(stale[:5])


def test_rewriting_preserves_jinja_examples_verbatim():
    """The link rewriting must not disturb the Home Assistant templates
    the documents are teaching - they're the content most likely to be
    silently mangled and least likely to be noticed."""
    for source_name, *_ in prepare.PAGES:
        source = (REPO / "docs" / source_name).read_text(encoding="utf-8")
        rewritten = prepare.rewrite_links(source)
        jinja_before = [line for line in source.splitlines() if "{{" in line]
        jinja_after = [line for line in rewritten.splitlines() if "{{" in line]
        assert jinja_before == jinja_after, f"{source_name}: Jinja lines changed during link rewriting"
