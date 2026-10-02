/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The dashboard is a pure client-side SPA — every page under app/ is a client
  // component and all data comes from the API at runtime. Exporting to static
  // files lets Cloudflare Pages serve it with no server runtime, no adapter, and
  // no coupling to a Next.js-version-specific deploy tool.
  //
  // The one constraint this imposes: no dynamic route segments, because a static
  // export has to enumerate every path at build time and agent/call/tool ids are
  // unbounded. Detail pages therefore take the id as a query parameter
  // (/agents/detail?id=...) rather than a path segment.
  output: "export",
  webpack: (config) => {
    // `@talqing/sdk`, `@talqing/client` and `@talqing/react` are `file:` deps, so
    // npm symlinks them out of `clients/typescript/packages/`. Webpack resolves a
    // module's real path by default, which means their `@livekit/components-react`
    // and `livekit-client` imports were resolved from the SDK workspace's own
    // node_modules — a SECOND copy of both, with its own React contexts and its
    // own `Room` class. That is why this app used to reimplement the SDK's hooks
    // instead of importing them: a context does not cross two module instances,
    // and `instanceof Room` is false across them.
    //
    // Keeping the link path makes resolution walk up from `frontend/node_modules`
    // instead, so there is one copy of each and the SDK's own hooks work here.
    // The cost is that webpack no longer watches the packages' real files, which
    // is why a change there needs `npm run build` in clients/typescript before
    // the dev server picks it up — as `prebuild` already does for a deploy.
    //
    // And a rebuild alone is not enough in dev. Webpack's filesystem cache lists
    // node_modules as a MANAGED path: it keys those modules by the package's
    // version rather than by file timestamp, so a rebuilt `@talqing/sdk` at the
    // same version is never re-read, however many times the dev server is
    // restarted. The symptom is a method that type-checks and is `undefined` at
    // runtime. `rm -rf .next/cache/webpack` and restart.
    config.snapshot = {
      ...config.snapshot,
      // Not managed, so the three workspace packages are snapshotted by
      // timestamp like first-party source. Everything else in node_modules keeps
      // the fast path.
      managedPaths: [/^(.+?[\\/]node_modules[\\/])(?!@talqing)/],
    };
    config.resolve.symlinks = false;
    return config;
  },
};
module.exports = nextConfig;
