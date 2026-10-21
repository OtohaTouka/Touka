import { PermissionConfig, IdentityMap, AutonomyConfig, Tool, McpServerStatus, Skill } from "@/lib/types";
import { AgentInfo } from "@/lib/providers/AgentProvider";
import { RbacMatrix } from "./security/RbacMatrix";
import { IdentityMapping } from "./security/IdentityMapping";
import { AutonomyBudget } from "./security/AutonomyBudget";
import { AutoReviewerSettings } from "./security/AutoReviewerSettings";

interface Props {
    permissions: PermissionConfig | null;
    setPermissions: (p: PermissionConfig | null) => void;
    identityMap: IdentityMap | null;
    setIdentityMap: (i: IdentityMap | null) => void;
    allAgents: AgentInfo[];
    autonomyConfig: AutonomyConfig | null;
    setAutonomyConfig: (a: AutonomyConfig | null) => void;
    isSavingPermissions: boolean;
    isSavingIdentity: boolean;
    isSavingAutonomy: boolean;
    savePermissions: (p: PermissionConfig) => Promise<boolean>;
    saveIdentity: (i: IdentityMap) => Promise<boolean>;
    saveAutonomy: (a: AutonomyConfig) => Promise<boolean>;
    systemTools: Tool[];
    mcpServers: McpServerStatus[];
    skills: Skill[];
}

export function SecurityTab({
    permissions, setPermissions,
    identityMap, setIdentityMap,
    allAgents,
    autonomyConfig, setAutonomyConfig,
    isSavingPermissions, isSavingIdentity, isSavingAutonomy,
    savePermissions, saveIdentity, saveAutonomy,
    systemTools, mcpServers, skills
}: Props) {
    return (
        <div className="space-y-12 animate-in fade-in slide-in-from-bottom-2 duration-300">
            {/* 1. RBAC Matrix Section */}
            <RbacMatrix 
                permissions={permissions}
                setPermissions={setPermissions}
                isSavingPermissions={isSavingPermissions}
                savePermissions={savePermissions}
                systemTools={systemTools}
                mcpServers={mcpServers}
                skills={skills}
            />

            {/* 2. Identity Mapping Section */}
            <IdentityMapping 
                identityMap={identityMap}
                setIdentityMap={setIdentityMap}
                allAgents={allAgents}
                permissions={permissions}
                isSavingIdentity={isSavingIdentity}
                saveIdentity={saveIdentity}
            />

            {/* 3. Autonomy Budget Section */}
            <AutonomyBudget 
                autonomyConfig={autonomyConfig}
                setAutonomyConfig={setAutonomyConfig}
                isSavingAutonomy={isSavingAutonomy}
                saveAutonomy={saveAutonomy}
            />

            {/* 4. Auto Reviewer Section（ADR-0246） */}
            <AutoReviewerSettings
                autonomyConfig={autonomyConfig}
                setAutonomyConfig={setAutonomyConfig}
                isSavingAutonomy={isSavingAutonomy}
                saveAutonomy={saveAutonomy}
            />
        </div>
    );
}