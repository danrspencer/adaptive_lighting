#!/usr/bin/env python3
"""
Prepares docs/ for a Jekyll build, then gets out of the way.

Everything this does exists to keep ONE source of truth for content that
has two audiences - people reading the repo on GitHub, and the published
docs site - without either one degrading the other:

1. Copies the integration's real dashboard card and the curve preview
   art into docs/assets/. Both live outside docs/ (in
   custom_components/.../www/ and dashboard/), and Jekyll can only serve
   files under its own source directory. Copying at build time rather
   than committing a second copy means the site can never show a stale
   card.

2. Generates site pages from docs/BLUEPRINT.md and docs/HELPERS.md.
   Those two files are read directly on GitHub, so they deliberately
   carry no front matter - GitHub renders a front matter block as a
   metadata table at the top of the page, which would be a visible
   regression for anyone reading them there. But Jekyll only treats a
   file as a *page* if it has a literal front matter block: front matter
   defaults in _config.yml are merged into pages, they don't turn a
   static file into one, so without this the files were copied to the
   built site verbatim as raw .md. So the front matter is prepended into
   a generated copy instead, and the pristine originals are excluded
   from the build.

   The generated copies also get their cross-links rewritten - the
   originals link to each other as sibling .md files (correct on GitHub,
   a 404 on the site) and out to repo files that aren't published at all.

Run automatically by .github/workflows/docs.yml before `jekyll build`.
Run it yourself before building the site locally - see CONTRIBUTING.md.
Everything it writes is gitignored; nothing it writes is a source file.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent
REPO = DOCS.parent

GITHUB_BLOB = "https://github.com/danrspencer/adaptive_lighting/blob/main"

# (source, destination) for the two files that live outside docs/.
ASSET_COPIES = [
    (
        REPO / "custom_components" / "adaptive_lighting_helpers" / "www" / "adaptive-lighting-curve-card.js",
        DOCS / "assets" / "js" / "adaptive-lighting-curve-card.js",
    ),
    (REPO / "dashboard" / "curve-preview.svg", DOCS / "assets" / "img" / "curve-preview.svg"),
]

# The reference docs, and the front matter each generated copy gets.
# nav_order continues from index (1) and playground (2); installation is 3.
#
# The generated name deliberately is NOT a case variant of the source
# ("reference-blueprint.md", not "blueprint.md"). macOS filesystems are
# case-INSENSITIVE, so docs/blueprint.md and docs/BLUEPRINT.md are the
# same file there - generating one would silently overwrite the source
# document. It cost one restore-from-git to find out. The permalink is
# what controls the published URL, so the clumsy filename never shows.
PAGES = [
    ("BLUEPRINT.md", "reference-blueprint.md", "Blueprint reference", 4, "/blueprint/"),
    ("HELPERS.md", "reference-helpers.md", "Integration reference", 5, "/helpers/"),
]

# Link rewrites applied to the generated copies only. Order matters: the
# anchored forms must be tried before the bare ones, or a bare rule would
# match first and leave the anchor stranded.
#
# Targets are RELATIVE, not root-relative, and that's deliberate: the
# generated pages have render_with_liquid disabled (see front_matter
# below), so they cannot use Liquid's relative_url filter to prepend the
# site's baseurl. Relative links need no baseurl to be correct. Both
# pages publish at a directory-style permalink (/blueprint/, /helpers/),
# so "../" from either one is the site root.
LINK_REWRITES = [
    # Sibling reference docs -> the other one's permalink.
    (r"\]\(BLUEPRINT\.md#", "](../blueprint/#"),
    (r"\]\(BLUEPRINT\.md\)", "](../blueprint/)"),
    (r"\]\(HELPERS\.md#", "](../helpers/#"),
    (r"\]\(HELPERS\.md\)", "](../helpers/)"),
    # The README's content is the site's home page, and its "why four
    # phases" heading keeps the same slug there, so anchors survive.
    (r"\]\(\.\./README\.md#", "](../#"),
    (r"\]\(\.\./README\.md\)", "](../)"),
    # Not published as part of the site - link out to the repo instead.
    (r"\]\(\.\./CONTRIBUTING\.md#", f"]({GITHUB_BLOB}/CONTRIBUTING.md#"),
    (r"\]\(\.\./CONTRIBUTING\.md\)", f"]({GITHUB_BLOB}/CONTRIBUTING.md)"),
    (r"\]\(\.\./dashboard/", f"]({GITHUB_BLOB}/dashboard/"),
    (r"\]\(\.\./blueprints/", f"]({GITHUB_BLOB}/blueprints/"),
    (r"\]\(\.\./custom_components/", f"]({GITHUB_BLOB}/custom_components/"),
]


def rewrite_links(text: str) -> str:
    for pattern, replacement in LINK_REWRITES:
        text = re.sub(pattern, replacement, text)
    return text


def front_matter(title: str, nav_order: int, permalink: str) -> str:
    # render_with_liquid: false is load-bearing, not tidiness. Both files
    # contain Home Assistant Jinja in their YAML examples ({{ today_at(...)
    # | as_timestamp }}). Liquid uses the same {{ }} delimiters, and with
    # Jekyll's default lax filter handling an unknown filter renders as an
    # empty string - so those example lines would silently come out blank,
    # with no build error. A Jekyll 4 feature; see docs/Gemfile.
    return (
        "---\n"
        f"title: {title}\n"
        f"nav_order: {nav_order}\n"
        f"permalink: {permalink}\n"
        "render_with_liquid: false\n"
        "# GENERATED by docs/_build/prepare.py from the file of the same\n"
        "# name in upper case. Edit that one, not this. This copy is\n"
        "# gitignored and rebuilt on every site build.\n"
        "---\n\n"
    )


def main() -> int:
    for source, destination in ASSET_COPIES:
        if not source.exists():
            print(f"error: expected to copy {source}, but it does not exist", file=sys.stderr)
            return 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        print(f"copied {source.relative_to(REPO)} -> {destination.relative_to(REPO)}")

    for source_name, generated_name, title, nav_order, permalink in PAGES:
        source = DOCS / source_name
        generated = DOCS / generated_name
        if not source.exists():
            print(f"error: expected to generate a page from {source}, but it does not exist", file=sys.stderr)
            return 1
        # Belt and braces against the case-insensitive-filesystem trap
        # described above: never write over the document being read.
        if generated.exists() and generated.samefile(source):
            print(
                f"error: {generated_name} resolves to the same file as {source_name} "
                "(case-insensitive filesystem) - refusing to overwrite the source",
                file=sys.stderr,
            )
            return 1
        body = rewrite_links(source.read_text(encoding="utf-8"))
        generated.write_text(front_matter(title, nav_order, permalink) + body, encoding="utf-8")
        print(f"generated {generated_name} from {source_name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
