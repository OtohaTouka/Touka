import { describe, it, expect } from "vitest";
import { extractArtifacts } from "../artifact-extractor";

const fence = (lang: string, body: string) => "```" + lang + "\n" + body + "\n```";
const typeOf = (text: string) => extractArtifacts(text)[0]?.type;

describe("extractArtifacts — react vs web routing (ADR-0114)", () => {
  it("routes a tsx component to 'react'", () => {
    expect(typeOf(fence("tsx", "export default function App(){ return <div>hi</div>; }"))).toBe("react");
  });

  it("routes a jsx code block with hooks to 'react'", () => {
    expect(typeOf(fence("jsx", "function App(){ const [n,setN]=useState(0); return <button onClick={()=>setN(n+1)}>{n}</button>; }"))).toBe("react");
  });

  it("routes `import React` + JSX to 'react' even when fenced as js", () => {
    expect(typeOf(fence("js", "import React from 'react';\nexport default () => <div className='x'>y</div>;"))).toBe("react");
  });

  it("routes a full HTML document to 'web' (not react)", () => {
    expect(typeOf(fence("html", "<!DOCTYPE html><html><body><div>hello</div></body></html>"))).toBe("web");
  });

  it("routes a plain HTML fragment (class, not className) to 'web'", () => {
    expect(typeOf(fence("html", "<div class='box'><button onclick='alert(1)'>go</button></div>"))).toBe("web");
  });

  it("keeps SVG as 'web'", () => {
    expect(typeOf(fence("svg", "<svg><circle cx='1' cy='1' r='1'/></svg>"))).toBe("web");
  });

  it("keeps mermaid as 'mermaid'", () => {
    expect(typeOf(fence("mermaid", "graph TD; A-->B;"))).toBe("mermaid");
  });

  it("honors an explicit <touka_artifact type='react'> tag", () => {
    const text = '<touka_artifact title="Demo" type="react">\nexport default () => <div/>;\n</touka_artifact>';
    const arts = extractArtifacts(text);
    expect(arts[0]?.type).toBe("react");
  });

  it("downgrades an unknown explicit tag type to safe 'code' (ADR-0114)", () => {
    const text = '<touka_artifact title="x" type="evil-exec">\nhello world\n</touka_artifact>';
    expect(extractArtifacts(text)[0]?.type).toBe("code");
  });

  it("normalizes a miscased explicit tag type (React → react)", () => {
    const text = '<touka_artifact title="x" type="React">\nexport default () => <div/>;\n</touka_artifact>';
    expect(extractArtifacts(text)[0]?.type).toBe("react");
  });

  it("downgrades explicit type='react' wrapping a full HTML document to 'web'", () => {
    const text = '<touka_artifact title="x" type="react">\n<!DOCTYPE html><html><body>hi</body></html>\n</touka_artifact>';
    expect(extractArtifacts(text)[0]?.type).toBe("web");
  });
});
