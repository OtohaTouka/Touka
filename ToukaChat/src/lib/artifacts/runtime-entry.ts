/**
 * Artifact 沙箱 runtime 入口（選項 A，ADR-0114）。
 *
 * 這支檔案**不被 app 程式碼匯入**——它是 `scripts/build-artifact-runtime.mjs` 的 esbuild
 * 入口，會被打包成單一 IIFE（內含 React 19，因 React 19 無 UMD），注入到 artifact 預覽
 * iframe 的 `<script>`。它把 React 掛上 iframe 的全域，並提供一個極簡掛載器；轉譯後的
 * 使用者碼（classic runtime → `React.createElement`）即可在 iframe 內渲染。
 *
 * 在 iframe 內執行（`sandbox="allow-scripts"`、無 same-origin、`connect-src 'none'`），
 * 故拿不到父視窗 token/cookie、也不能外連。
 */
import * as React from "react";
import { createRoot } from "react-dom/client";

interface ArtifactGlobal {
  React: typeof React;
  __touka_mount: (element: React.ReactElement, node: Element) => void;
}

// 以 `as unknown as` 取代 `any`（遵守 no-explicit-any 棘輪），且不污染全域 Window 型別。
const g = window as unknown as ArtifactGlobal;
g.React = React;
g.__touka_mount = (element, node) => {
  createRoot(node).render(element);
};
