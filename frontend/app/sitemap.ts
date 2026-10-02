import type { MetadataRoute } from "next";

import { DOCS_URL, SITE_URL } from "./site";

// Only the pages a search engine should hold. Everything else in app/ is the
// authenticated dashboard and is disallowed in app/robots.ts.
//
// The documentation lives on its own origin and ships its own sitemap covering
// all of its pages (documentation/sitemap.py). The single entry below is a
// crawl path to it, not a substitute: a cross-host URL in this file is only
// honoured once docs.talqing.com is verified in Search Console in its own
// right, which is where its sitemap should be submitted.
//
// lastModified is a fixed date rather than build time on purpose: a sitemap
// whose dates move on every deploy teaches crawlers that the dates mean nothing.
// Bump a page's date when its content actually changes; the two legal pages
// carry theirs as `UPDATED`.

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    {
      url: `${SITE_URL}/`,
      lastModified: "2026-10-02",
      changeFrequency: "weekly",
      priority: 1,
    },
    {
      url: `${DOCS_URL}/`,
      lastModified: "2026-10-02",
      changeFrequency: "weekly",
      priority: 0.8,
    },
    {
      url: `${SITE_URL}/login`,
      lastModified: "2026-10-02",
      changeFrequency: "yearly",
      priority: 0.5,
    },
    {
      url: `${SITE_URL}/privacy`,
      lastModified: "2026-09-19",
      changeFrequency: "yearly",
      priority: 0.3,
    },
    {
      url: `${SITE_URL}/terms`,
      lastModified: "2026-10-02",
      changeFrequency: "yearly",
      priority: 0.3,
    },
  ];
}
