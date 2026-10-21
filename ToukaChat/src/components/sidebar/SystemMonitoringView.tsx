import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Activity, Cloud, Brain, Database, BarChart3, Server, MessageSquare } from "lucide-react";
import { useSystemMonitoring } from "@/hooks/useSystemMonitoring";
import { SectionWrapper } from "./monitoring/SectionWrapper";
import { ConnectionStatus } from "./monitoring/ConnectionStatus";
import { ModelList } from "./monitoring/ModelList";
import { MemoryStatus } from "./monitoring/MemoryStatus";
import { TokenUsage } from "./monitoring/TokenUsage";
import { DailyQuota } from "./monitoring/DailyQuota";
import { BackgroundActivity } from "./monitoring/BackgroundActivity";
import { InterruptedRunsNotice } from "./monitoring/InterruptedRunsNotice";
import { ServerHealth } from "./monitoring/ServerHealth";
import { FeedbackLog } from "./monitoring/FeedbackLog";

interface SystemMonitoringViewProps {
  isActive: boolean;
}

export function SystemMonitoringView({ isActive }: SystemMonitoringViewProps) {
  const { t } = useTranslation();
  const stats = useSystemMonitoring(isActive);
  const [activeSections, setActiveSections] = useState<string[]>([
    "connection",
    "models",
    "vault",
    "health",
    "tokens",
  ]);

  const toggleSection = (id: string) => {
    setActiveSections((prev) =>
      prev.includes(id) ? prev.filter((s) => s !== id) : [...prev, id]
    );
  };

  return (
    <div className="p-4 flex flex-col gap-4 w-full min-w-0">
      <div className="flex items-center gap-2 mb-2 text-foreground shrink-0 w-full overflow-hidden">
        <Activity className="w-5 h-5 text-primary shrink-0" />
        {/* WCAG 1.3.1（A5 axe heading-order）：此面板為頁面首個標題 → h2 */}
        <h2 className="font-semibold text-sm truncate">System Monitoring</h2>
      </div>

      <InterruptedRunsNotice runs={stats.interruptedRuns} />
      <BackgroundActivity activity={stats.backgroundActivity} />

      <SectionWrapper
        title="Connection Status"
        isOpen={activeSections.includes("connection")}
        toggle={() => toggleSection("connection")}
        icon={<Cloud className="w-4 h-4" />}
      >
        <ConnectionStatus connected={stats.connected} latency={stats.latency} />
      </SectionWrapper>

      <SectionWrapper
        title="Available Models"
        isOpen={activeSections.includes("models")}
        toggle={() => toggleSection("models")}
        icon={<Brain className="w-4 h-4" />}
      >
        <ModelList models={stats.activeModels} />
      </SectionWrapper>

      <SectionWrapper
        title="Memory Status"
        isOpen={activeSections.includes("vault")}
        toggle={() => toggleSection("vault")}
        icon={<Database className="w-4 h-4" />}
      >
        <MemoryStatus
          memStats={stats.memStats}
          vaultHealth={stats.vaultHealth}
          emergence={stats.emergence}
        />
      </SectionWrapper>

      <SectionWrapper
        title="Token Usage"
        isOpen={activeSections.includes("tokens")}
        toggle={() => toggleSection("tokens")}
        icon={<BarChart3 className="w-4 h-4" />}
      >
        <div className="space-y-3">
          <DailyQuota quota={stats.dailyQuota} />
          <TokenUsage tokenUsage={stats.tokenUsage} />
        </div>
      </SectionWrapper>

      <SectionWrapper
        title="Server Status"
        isOpen={activeSections.includes("health")}
        toggle={() => toggleSection("health")}
        icon={<Server className="w-4 h-4" />}
      >
        <ServerHealth healthStats={stats.healthStats} />
      </SectionWrapper>

      {/* HAX G15：擁有者檢視收集到的回饋（讚/倒讚＋原因）。 */}
      <SectionWrapper
        title={t("sidebar.system_monitoring_view.feedback_log_title")}
        isOpen={activeSections.includes("feedback")}
        toggle={() => toggleSection("feedback")}
        icon={<MessageSquare className="w-4 h-4" />}
      >
        <FeedbackLog />
      </SectionWrapper>
    </div>
  );
}
