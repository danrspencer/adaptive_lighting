/**
 * Adds the current page's own headings to the sidebar, nested under
 * whichever nav entry is the page you're on, and highlights the section
 * you're currently reading.
 *
 * just-the-docs nests *pages* in the sidebar (a parent with children),
 * but has no notion of nesting a page's headings - so on a single-level
 * site like this one the sidebar can only ever say which page you're on,
 * never where you are inside it. On the long reference pages, which run
 * to a dozen or more sections, that's most of the navigating anyone
 * actually does.
 *
 * Client-side rather than built into the layout because the headings and
 * their anchor ids are produced by kramdown at build time and are only
 * reliably available from the rendered document. The in-page "On this
 * page" index is the server-rendered version of the same information, so
 * nothing here is the only route to it: with JavaScript off the sidebar
 * is simply the plain page list it was before.
 */

(function () {
  'use strict';

  // h2 and h3 only. h1 is the page title, which the nav entry already
  // is; h4 and below are detail that would make a narrow sidebar
  // unreadable.
  const HEADING_SELECTOR = 'main h2[id], main h3[id]';

  /** Trailing slashes and index.html make the same page look like three
   *  different URLs; compare them in one normalised form. */
  function normalisePath(path) {
    return path.replace(/index\.html$/, '').replace(/\/+$/, '') || '/';
  }

  function findCurrentNavItem() {
    const here = normalisePath(window.location.pathname);
    for (const link of document.querySelectorAll('.site-nav .nav-list-link')) {
      if (normalisePath(new URL(link.href, window.location.origin).pathname) === here) {
        return link.closest('.nav-list-item');
      }
    }
    return null;
  }

  function buildSectionList(headings) {
    const list = document.createElement('ul');
    list.className = 'nav-list section-nav-list';

    for (const heading of headings) {
      const item = document.createElement('li');
      item.className = 'nav-list-item section-nav-item';
      if (heading.tagName === 'H3') item.classList.add('section-nav-item--sub');

      const link = document.createElement('a');
      link.className = 'nav-list-link section-nav-link';
      link.href = `#${heading.id}`;
      link.textContent = heading.textContent.trim();
      link.dataset.sectionId = heading.id;

      item.appendChild(link);
      list.appendChild(item);
    }
    return list;
  }

  /** Marks the section you're currently reading.
   *
   *  Deliberately not an IntersectionObserver "most visible heading"
   *  scheme: headings are points, not regions, so several are off screen
   *  at once and the observer says nothing about which section you're
   *  *inside*. Tracking the last heading to have passed the top of the
   *  viewport answers that directly. */
  function trackActiveSection(headings, links) {
    if (!headings.length) return;

    // A heading counts as passed once it's within the top quarter of the
    // viewport, so the highlight moves as a section reaches reading
    // position rather than only when it leaves the screen.
    const threshold = () => window.innerHeight * 0.25;

    let frame = null;
    const update = () => {
      frame = null;
      let current = headings[0].id;
      for (const heading of headings) {
        if (heading.getBoundingClientRect().top <= threshold()) {
          current = heading.id;
        } else {
          break;
        }
      }
      // At the very bottom the last section may be too short to reach the
      // threshold; if the page is scrolled to the end, it's the one being
      // read.
      if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 2) {
        current = headings[headings.length - 1].id;
      }
      for (const link of links) {
        link.classList.toggle('is-current', link.dataset.sectionId === current);
      }
    };

    const schedule = () => {
      if (frame === null) frame = window.requestAnimationFrame(update);
    };

    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule, { passive: true });
    update();
  }

  function init() {
    const navItem = findCurrentNavItem();
    if (!navItem) return;

    const headings = [...document.querySelectorAll(HEADING_SELECTOR)];
    // Nothing worth indexing: a page with a single section gains only
    // clutter from a one-item list repeating its own title.
    if (headings.length < 2) return;

    const list = buildSectionList(headings);
    navItem.appendChild(list);
    navItem.classList.add('has-section-nav');

    trackActiveSection(headings, [...list.querySelectorAll('.section-nav-link')]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
