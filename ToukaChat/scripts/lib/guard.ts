import fs from 'fs';
import path from 'path';
import { Project, SyntaxKind } from 'ts-morph';
import glob from 'fast-glob';

/**
 * 後端 OpenAPI 解析器 (SSOT Provider)
 */
export class OpenApiParser {
  constructor(private schemaPath: string) {}

  getRoutes(): string[] {
    if (!fs.existsSync(this.schemaPath)) {
      throw new Error(`找不到 OpenAPI Schema: ${this.schemaPath}`);
    }
    const schema = JSON.parse(fs.readFileSync(this.schemaPath, 'utf-8'));
    return Object.keys(schema.paths || {});
  }
}

/**
 * 前端代碼掃描器 (Call Site Provider)
 */
export class FrontendScanner {
  constructor(private projectRoot: string) {}

  async scanCalls(): Promise<{ file: string; line: number; url: string }[]> {
    const project = new Project();
    const files = await glob('src/**/*.{ts,tsx}', { cwd: this.projectRoot, absolute: true });
    const calls: { file: string; line: number; url: string }[] = [];

    for (const file of files) {
      const sourceFile = project.addSourceFileAtPath(file);
      
      // 搜尋 fetchTouka 或 useSWR 的呼叫
      const callExpressions = sourceFile.getDescendantsOfKind(SyntaxKind.CallExpression);
      
      for (const call of callExpressions) {
        const expression = call.getExpression();
        const name = expression.getText();
        
        if (name === 'fetchTouka' || name === 'useSWR') {
          const args = call.getArguments();
          const argIndex = name === 'fetchTouka' ? 1 : 0;
          
          if (args.length > argIndex) {
            const targetArg = args[argIndex];
            let url = '';

            // 處理字串字面量
            if (targetArg.getKind() === SyntaxKind.StringLiteral || targetArg.getKind() === SyntaxKind.NoSubstitutionTemplateLiteral) {
              const literal = targetArg.asKindOrThrow(SyntaxKind.StringLiteral);
              url = literal.getLiteralValue();
            } 
            // 處理模板字串 (TemplateExpression)
            else if (targetArg.getKind() === SyntaxKind.TemplateExpression) {
              const template = targetArg.asKindOrThrow(SyntaxKind.TemplateExpression);
              url = template.getHead().getLiteralText();
              for (const span of template.getTemplateSpans()) {
                url += '{param}' + span.getLiteral().getLiteralText();
              }
            }

            if (url && (url.startsWith('/') || url.startsWith('v1'))) {
              // 標準化路徑 (確保以 / 開頭)
              const normalizedUrl = url.startsWith('/') ? url : `/${url}`;
              // 排除外部連結
              if (!normalizedUrl.startsWith('http')) {
                calls.push({
                  file: path.relative(this.projectRoot, file),
                  line: call.getStartLineNumber(),
                  url: normalizedUrl
                });
              }
            }
          }
        }
      }
    }
    return calls;
  }
}

/**
 * 比對引擎 (Validator)
 */
export class ParityEngine {
  private routes: Set<string>;
  private regexRoutes: { pattern: RegExp; original: string }[];

  constructor(routes: string[]) {
    this.routes = new Set(routes);
    this.regexRoutes = routes.map(r => ({
      // 將 {param} 或 {id} 轉換為通配符 [^/]+
      pattern: new RegExp('^' + r.replace(/{[^}]+}/g, '[^/]+') + '$'),
      original: r
    }));
  }

  validate(url: string): { valid: boolean; match?: string; error?: string } {
    const purePath = url.split('?')[0];
    
    // 1. 檢查是否符合 API Prefix SSOT (強制優先使用 /v1)
    if (!purePath.startsWith('/v1/')) {
      const v1Counterpart = `/v1${purePath}`;
      const hasV1Match = Array.from(this.routes).some(r => r === v1Counterpart) || 
                         this.regexRoutes.some(r => r.pattern.test(v1Counterpart.replace(/{param}/g, 'anyid')));
      
      if (hasV1Match) {
        return { 
          valid: false, 
          error: `偵測到未使用 '/v1' 前綴的呼叫。後端已支援該端點的 /v1 版本，請更正為: "${v1Counterpart}"`
        };
      }
    }

    // 2. 精確匹配
    if (this.routes.has(purePath)) {
      return { valid: true, match: purePath };
    }

    // 3. Regex 匹配 (後端動態路徑)
    const match = this.regexRoutes.find(r => r.pattern.test(purePath.replace(/{param}/g, 'anyid')));
    if (match) {
      return { valid: true, match: match.original };
    }

    return { valid: false };
  }
}
