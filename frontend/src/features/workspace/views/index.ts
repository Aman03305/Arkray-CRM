/*
 * Every module view, for tests. Pages import their module's own file instead (for example
 * "@/features/workspace/views/pipeline"), so that each route ships only its module's code.
 */
export { ActivitiesView, ActivityView } from "./activities";
export { AskWorkspaceView } from "./ask";
export { DashboardView } from "./dashboard";
export { LeadView } from "./leads";
export { EditOpportunityView, NewOpportunityView, OpportunityView, PipelineView } from "./pipeline";
