import { McpToolPolicy, type McpToolPolicyProps } from "./McpToolPolicy";

/** P5 tools 獨立視圖；治理行為仍由既有 ADR-0195 面板唯一實作。 */
export function McpToolsView(props: McpToolPolicyProps) {
  return <McpToolPolicy {...props} />;
}
