import { useTranslation } from "react-i18next";
import { Users, Database, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { IdentityMap, PermissionConfig, AgentIdentity } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { 
  Table, 
  TableBody, 
  TableCell, 
  TableHead, 
  TableHeader, 
  TableRow 
} from "@/components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// 可經身分對應指派的代理角色（鏡像後端 ASSIGNABLE_AGENT_ROLES；Subagent／System 只供內部，
// 選了存檔會 422）。tests/unit/test_soul_only_agent_roles.py 核對兩邊一致。
const ASSIGNABLE_AGENT_ROLES = ["Owner", "Base", "Debugger"];

interface Props {
  identityMap: IdentityMap | null;
  setIdentityMap: (i: IdentityMap | null) => void;
  allAgents: AgentIdentity[];
  permissions: PermissionConfig | null;
  isSavingIdentity: boolean;
  saveIdentity: (i: IdentityMap) => Promise<boolean>;
}

export function IdentityMapping({
  identityMap,
  setIdentityMap,
  allAgents,
  permissions,
  isSavingIdentity,
  saveIdentity
}: Props) {
  const { t } = useTranslation();
  if (!identityMap) return null;

  return (
    <section className="space-y-6 pt-12 border-t border-border">
      <div className="flex items-center justify-between">
        <div className="space-y-1">
          <h4 className="text-sm font-bold flex items-center gap-2 text-foreground">
            <Users className="w-5 h-5 text-primary" /> {t('settings.identity_mapping.heading')}
          </h4>
          <p className="text-xs text-muted-foreground">{t('settings.identity_mapping.description')}</p>
        </div>
        <Button
          variant="outline"
          onClick={() => identityMap && saveIdentity(identityMap)}
          disabled={isSavingIdentity || !identityMap}
          className="border-primary/20 bg-primary/10 text-primary hover:bg-primary/20 rounded-xl text-xs font-bold"
        >
          {isSavingIdentity ? <span className="animate-spin mr-2">⏳</span> : <Database className="w-4 h-4 mr-2" />}
          {t('settings.identity_mapping.save_button')}
        </Button>
      </div>

      <div className="overflow-hidden rounded-3xl border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border bg-muted/30">
              <TableHead className="px-6 py-4 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.identity_mapping.table_header_agent_id_name')}</TableHead>
              <TableHead className="px-6 py-4 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.identity_mapping.table_header_rbac_role')}</TableHead>
              <TableHead className="px-6 py-4 text-xs font-black uppercase tracking-widest text-muted-foreground">{t('settings.identity_mapping.table_header_capabilities')}</TableHead>
              <TableHead className="w-10 px-6 py-4 text-xs font-black uppercase tracking-widest text-muted-foreground"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {allAgents.map(agent => {
              const override = identityMap.agent_overrides[agent.id];
              const hasOverride = !!override;
              // 有 override＝顯示它（即時反映本地編輯）；否則顯示後端算出的有效值
              // （tag_rules-aware，是靈魂即 Owner）——不再一律退 default_role（Base）＝
              // 修「新靈魂顯示 Base」的誤導。
              const effectiveRole = agent.effective_role ?? identityMap.default_role;
              const effectiveCaps = agent.effective_capabilities ?? [];
              const displayRole = override?.role ?? effectiveRole;
              const displayCaps = override?.capabilities ?? effectiveCaps;
              // 建立 override 時的基底＝目前有效值（角色＋能力全帶），故「改一項」不會把
              // 其餘能力清成 []（Sumi 因此掉 can_supervise_dispatch 的靜默降級路徑）。
              const overrideBase = () =>
                identityMap.agent_overrides[agent.id]
                ?? { role: effectiveRole, capabilities: [...effectiveCaps] };
              // 能力清單＝有效能力 ∪ heartbeat_awakening（保底可見可加），避免只列單一
              // 硬編碼能力而遮蔽其他有效能力的存在。
              const capList = Array.from(new Set([...effectiveCaps, "heartbeat_awakening"]));
              return (
                <TableRow key={agent.id} className="border-b border-border/50 hover:bg-accent/5 transition-colors group">
                  <TableCell className="px-6 py-4">
                    <div className="text-sm font-bold text-foreground">{agent.name}</div>
                    <div className="mt-0.5 font-mono text-xs text-muted-foreground">{agent.id}</div>
                  </TableCell>
                  <TableCell className="px-6 py-4">
                    <Select
                      value={displayRole}
                      onValueChange={(val) => {
                        const updated = {
                          ...identityMap,
                          agent_overrides: {
                            ...identityMap.agent_overrides,
                            [agent.id]: { ...overrideBase(), role: val }
                          }
                        };
                        setIdentityMap(updated);
                      }}
                    >
                      <SelectTrigger className="w-48 rounded-lg border border-border bg-muted/50 text-xs font-bold text-foreground">
                        <SelectValue placeholder={t('settings.identity_mapping.select_role_placeholder')} />
                      </SelectTrigger>
                      <SelectContent>
                        {ASSIGNABLE_AGENT_ROLES.filter(r => r in (permissions?.roles || {})).map(r => (
                          <SelectItem key={r} value={r}>{r}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <div className="mt-1 text-xs font-bold uppercase tracking-wider text-muted-foreground">
                      {hasOverride
                        ? t('settings.identity_mapping.override_active')
                        : t('settings.identity_mapping.inherited_from_type')}
                    </div>
                  </TableCell>
                  <TableCell className="px-6 py-4">
                    <div className="flex flex-wrap gap-2">
                      {capList.map(cap => {
                        const isOn = displayCaps.includes(cap);
                        // WCAG 2.1.1/4.1.2（P3）：可點 Badge → 鍵盤可達 toggle（role=button + aria-pressed + Enter/Space）。
                        const toggleCap = () => {
                          const base = overrideBase();
                          const caps = base.capabilities;
                          const newCaps = caps.includes(cap)
                            ? caps.filter((c: string) => c !== cap)
                            : [...caps, cap];
                          setIdentityMap({
                            ...identityMap,
                            agent_overrides: {
                              ...identityMap.agent_overrides,
                              [agent.id]: { ...base, capabilities: newCaps }
                            }
                          });
                        };
                        return (
                        <Badge
                          key={cap}
                          role="button"
                          tabIndex={0}
                          aria-pressed={isOn}
                          aria-label={t('settings.identity_mapping.capability_aria', { capability: cap.replace("_", " "), id: agent.id })}
                          variant={isOn ? "default" : "outline"}
                          className={cn(
                            "cursor-pointer px-2 py-1 text-xs font-black uppercase tracking-tighter transition-all outline-none focus-visible:ring-2 focus-visible:ring-primary/50",
                            isOn
                              ? "bg-primary/10 border-primary/30 text-primary shadow-sm hover:bg-primary/20"
                              : "text-muted-foreground opacity-40 hover:opacity-100"
                          )}
                          onClick={toggleCap}
                          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleCap(); } }}
                        >
                          {cap.replace("_", " ")}
                        </Badge>
                        );
                      })}
                    </div>
                  </TableCell>
                  <TableCell className="px-6 py-4">
                    {hasOverride && (
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={t('settings.identity_mapping.remove_override_aria', { id: agent.id })}
                        onClick={() => {
                          const newOverrides = { ...identityMap.agent_overrides };
                          delete newOverrides[agent.id];
                          setIdentityMap({ ...identityMap, agent_overrides: newOverrides });
                        }}
                        className="text-muted-foreground hover:text-destructive opacity-0 group-hover:opacity-100"
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
