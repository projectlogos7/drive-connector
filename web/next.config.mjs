/** Static export served by the Python API at /app. In dev, API calls are proxied to the API server. */
const API = process.env.NOUS_DRIVE_API || "http://localhost:8080";
const isDev = process.env.NODE_ENV !== "production";

/** @type {import('next').NextConfig} */
const config = {
  basePath: "/app",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  ...(isDev
    ? {
        async rewrites() {
          return [
            { source: "/api/:path*", destination: `${API}/api/:path*`, basePath: false },
            { source: "/oauth/:path*", destination: `${API}/oauth/:path*`, basePath: false },
            { source: "/auth/:path*", destination: `${API}/auth/:path*`, basePath: false },
          ];
        },
      }
    : { output: "export" }),
};
export default config;
