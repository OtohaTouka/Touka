"use client";

import React, { useState, useEffect } from "react";
import { CheckCircle2, Circle, Loader2, AlertCircle } from "lucide-react";
import { motion } from "framer-motion";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useTranslation } from "react-i18next";
// import { Progress } from "@/components/ui/progress"; // 假設有 Progress 元件或自行實作

interface PlanStep {
  text: string;
  status: "todo" | "in_progress" | "completed" | "failed";
  details?: string;
}

interface PlanData {
  title: string;
  steps: PlanStep[];
}

export const PlanTracker: React.FC = () => {
  const { t } = useTranslation();
  const [plan, setPlan] = useState<PlanData | null>(null);

  useEffect(() => {
    // 監聽來自 VS Code 的訊息 (透過 window 事件)
    const handleMessage = (event: Event | MessageEvent) => {
      // P10 fix: reject cross-origin postMessage to prevent spoofed plan injection
      if (event instanceof MessageEvent && event.origin && event.origin !== window.location.origin) return;
      const message = (event as CustomEvent).detail ?? (event as MessageEvent).data;
      
      if (message.type === "plan_created") {
        setPlan({
          title: message.title,
          steps: message.steps.map((s: string) => ({ text: s, status: "todo" })),
        });
      } else if (message.type === "plan_step_update") {
        setPlan((prev) => {
          if (!prev) return prev;
          const newSteps = [...prev.steps];
          if (newSteps[message.stepIndex]) {
            newSteps[message.stepIndex] = {
              ...newSteps[message.stepIndex],
              status: message.status,
              details: message.details,
            };
          }
          return { ...prev, steps: newSteps };
        });
      }
    };

    window.addEventListener("vscode-message", handleMessage);
    window.addEventListener("message", handleMessage);
    window.addEventListener("touka_bridge_message", handleMessage);
    return () => {
      window.removeEventListener("vscode-message", handleMessage);
      window.removeEventListener("message", handleMessage);
      window.removeEventListener("touka_bridge_message", handleMessage);
    };
  }, []);

  if (!plan) return null;

  const completedCount = plan.steps.filter((s) => s.status === "completed").length;
  const progress = (completedCount / plan.steps.length) * 100;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="my-4 w-full"
    >
      <Card className="border-primary/20 bg-muted/30 backdrop-blur-sm">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium flex justify-between items-center">
            <span>{t("layout.plan_tracker.task_plan_title", { title: plan.title })}</span>
            <span className="text-xs text-muted-foreground">{t("layout.plan_tracker.completed_count", { completed: completedCount, total: plan.steps.length })}</span>
          </CardTitle>
          <div className="h-1.5 w-full bg-secondary rounded-full overflow-hidden mt-2">
            <motion.div 
              className="h-full bg-primary"
              initial={{ width: 0 }}
              animate={{ width: `${progress}%` }}
              transition={{ duration: 0.5 }}
            />
          </div>
        </CardHeader>
        <CardContent className="space-y-3 pt-2">
          {plan.steps.map((step, index) => (
            <div key={index} className="flex items-start gap-3 text-sm">
              <div className="mt-0.5">
                {step.status === "completed" && <CheckCircle2 className="w-4 h-4 text-status-success" />}
                {step.status === "in_progress" && <Loader2 className="w-4 h-4 text-primary animate-spin" />}
                {step.status === "todo" && <Circle className="w-4 h-4 text-muted-foreground" />}
                {step.status === "failed" && <AlertCircle className="w-4 h-4 text-destructive" />}
              </div>
              <div className="flex-1">
                <p className={`${step.status === "completed" ? "line-through text-muted-foreground" : "font-medium"}`}>
                  {step.text}
                </p>
                {step.details && step.status === "in_progress" && (
                  <motion.p 
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="text-xs text-muted-foreground mt-1"
                  >
                    🔍 {step.details}
                  </motion.p>
                )}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </motion.div>
  );
};