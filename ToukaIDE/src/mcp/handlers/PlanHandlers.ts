export function handleCreatePlan(
    title: string, 
    steps: string[], 
    onUpdate: (data: any) => void
): string {
    onUpdate({ type: 'plan_created', title, steps });
    return `已建立任務計畫: "${title}"，共 ${steps.length} 個步驟。UI 已更新。`;
}

export function handleUpdateTaskStatus(
    stepIndex: number, 
    status: string, 
    details: string | undefined, 
    onUpdate: (data: any) => void
): string {
    onUpdate({ type: 'plan_step_update', stepIndex, status, details });
    return `步驟 ${stepIndex} 狀態已更新為: ${status}。UI 已更新。`;
}
