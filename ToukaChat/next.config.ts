import type { NextConfig } from "next";
import packageJson from "./package.json";

const nextConfig: NextConfig = {
  output: "export",
  assetPrefix: "./",
  typescript: {
    tsconfigPath: "tsconfig.build.json",
  },
  images: {
    unoptimized: true,
  },
  // 只把版本號內嵌進前端，不把整份 package.json 打包進去（app_meta.ts 經 config.ts 讀取）。
  env: {
    NEXT_PUBLIC_APP_VERSION: packageJson.version,
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

