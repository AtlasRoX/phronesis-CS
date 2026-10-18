import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  devIndicators: {
    position: "bottom-left",
  },
  // Defence in depth for UI-002: answers never render remote images (markdownSafety turns them into links), and
  // the browser refuses any that slip through. Only same-origin, data: and blob: images (logos, server plots) load.
  async headers() {
    return [{ source: "/:path*", headers: [{ key: "Content-Security-Policy", value: "img-src 'self' data: blob:" }] }];
  },
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        // CSS Modules must bypass the `as` rename or they compile as global CSS.
        condition: { not: { path: /\.module\.css$/ } },
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
