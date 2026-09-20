const backendApiUrl = (process.env.DEVSUPPORT_BACKEND_API_URL ?? "http://127.0.0.1:8002").replace(
  /\/$/,
  "",
);

/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendApiUrl}/:path*`,
      },
    ];
  },
};

export default nextConfig;
