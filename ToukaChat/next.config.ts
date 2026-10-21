import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  assetPrefix: "./",
  typescript: {
    tsconfigPath: "tsconfig.build.json",
  },
  images: {
    unoptimized: true,
  },
  // Turbopack 設定（Next.js 16 預設使用 Turbopack）
  turbopack: {
    // 讓 Turbopack 正確處理 .wasm 檔案
    rules: {
      "*.wasm": {
        loaders: [],
        as: "*.wasm",
      },
    },
  },
};

export default nextConfig;

