import type { MetadataRoute } from "next";

import { SITE_URL } from "./site";

// Everything is crawlable, including the dashboard routes this export also
// serves. Keeping those out of the index is the `robots` default in
// app/layout.tsx, and a crawler has to be allowed to fetch a page to read it:
// a route disallowed here can still be indexed, URL-only, from a link.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: "*", allow: "/" },
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
