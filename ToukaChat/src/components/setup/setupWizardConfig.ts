export type WizardStepId =
  | "system_health"
  | "admin_password"
  | "api_keys"
  | "secrets"
  | "data_path"
  | "create_user"
  | "complete";

export interface WizardStep {
  id: WizardStepId;
  title: string;
  isRequired: boolean;
}

export const SETUP_WIZARD_STEPS: WizardStep[] = [
  { id: "system_health", title: "System & Language", isRequired: true },
  { id: "admin_password", title: "Admin Password", isRequired: true },
  { id: "api_keys", title: "API Keys", isRequired: true },
  { id: "secrets", title: "Security Secrets", isRequired: true },
  { id: "data_path", title: "Data Directory", isRequired: false },
  { id: "create_user", title: "Create Users", isRequired: false },
  { id: "complete", title: "Complete", isRequired: true },
];
