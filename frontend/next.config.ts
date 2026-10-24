import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // 立绘与换装素材由 stands 那个 nginx 容器发出（`docker compose` 里的服务名就是 `stands`）。
  // 页面用同源路径 `/stands/...` 取它们（`EIDOLON_STAND_ASSETS_URL=/stands`），所以手机用局域网地址、
  // 电脑用 Tailscale 地址、或者以后走 HTTPS，都自动跟着当前地址走，不必改配置。
  async rewrites() {
    const upstream = process.env.STANDS_UPSTREAM ?? "http://stands";
    return [{ source: "/stands/:path*", destination: `${upstream}/:path*` }];
  },
  // 铃声、接通音、挂断音（public/sounds/）默认是 max-age=0，每次进对话页都要回源确认一次。文件名不带哈希，
  // 换素材后老用户最多再听一天旧的，所以缓存一天。
  async headers() {
    return [{ source: "/sounds/:path*", headers: [{ key: "Cache-Control", value: "public, max-age=86400" }] }];
  },
};

export default nextConfig;
