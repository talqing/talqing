// The canonical public origin.
//
// Hard-coded rather than read from the environment, deliberately. One static
// export is served on four hostnames — talqing.com, app.talqing.com,
// app.dev.talqing.com and the project's .pages.dev alias — and every one of them
// serves byte-identical HTML. The canonical link, the sitemap and robots.txt are
// what tell a crawler which of those four is the real site, so their value must
// be the same on all four. An environment variable would make it whatever the
// build happened to be for, which is exactly the bug it looks like it prevents.
export const SITE_URL = "https://talqing.com";

// The documentation site: a separate Cloudflare Pages project on its own
// origin, so every link to it is absolute and leaves this app. Here rather
// than in marketing-chrome because app/sitemap.ts needs it too, and a
// metadata route must not import a module that pulls in fonts and React.
export const DOCS_URL = "https://docs.talqing.com";

// The source repository. Linked as a plain link with no live star count: the
// count would be a request to GitHub from every visitor, and /privacy lists
// what these pages reach out to.
export const GITHUB_URL = "https://github.com/talqing/talqing";

// The demo scheduler. Every "Book a demo" link opens it in a new tab and never
// frames it: /privacy promises no third-party cookie on this site.
export const DEMO_URL = "https://calendly.com/hello-talqing-tcsb/30min";
