/**
 * API types, generated from the backend's OpenAPI contract (backend/openapi.yaml ->
 * schema.gen.ts via `pnpm api:types`). Never edit schema.gen.ts by hand; CI fails if it
 * drifts from the backend.
 */
import type { components, paths } from "./schema.gen";

type Schemas = components["schemas"];

export type ViewerDto = Schemas["Viewer"];
export type AdminUser = Schemas["AdminUser"];
export type AdminUserPage = Schemas["AdminUserPage"];
export type InvitationState = Schemas["InvitationState"];
export type WorkspaceDto = Schemas["Workspace"];
export type InvitationPreview = Schemas["InvitationPreview"];
export type Role = Schemas["RoleEnum"];
export type UserStatus = Schemas["StatusEnum"];

export type UserRef = Schemas["UserRef"];
// The canonical customer record. No Leads module (ADR-0027): a read-only lead page, reached
// from the dashboard, search and its opportunity (ADR-0028).
export type Lead = Schemas["Lead"];
export type LeadDuplicate = Schemas["LeadDuplicate"];
export type LeadDuplicateList = Schemas["LeadDuplicateList"];
export type Assignee = Schemas["Assignee"];
export type AssigneePage = Schemas["AssigneePage"];
export type LoginRequest = Schemas["LoginRequest"];
export type UserCreateRequest = Schemas["UserCreateRequest"];
export type UserUpdateRequest = Schemas["PatchedUserUpdateRequest"];
export type EmailChangeRequest = Schemas["EmailChangeRequest"];
export type SetPasswordRequest = Schemas["SetPasswordRequest"];
export type SupportSessionDto = Schemas["SupportSession"];
export type SupportSessionStartRequest = Schemas["SupportSessionStartRequest"];
export type SecurityEvent = Schemas["SecurityEvent"];
export type SecurityEventPage = Schemas["SecurityEventPage"];
export type Person = Schemas["Person"];

export type PipelineDto = Schemas["Pipeline"];
export type PipelineList = Schemas["PipelineList"];
export type PipelineRef = Schemas["PipelineRef"];
export type Stage = Schemas["Stage"];
export type StageCategory = Schemas["StageCategoryEnum"];
export type Opportunity = Schemas["Opportunity"];
export type OpportunityCard = Schemas["OpportunityCard"];
export type OpportunityPage = Schemas["OpportunityPage"];
// An opportunity's lead (its customer record), or "restricted" outside this workspace.
export type OpportunityLeadRef = Schemas["LeadRef"];
export type OpportunityOptions = Schemas["OpportunityOptions"];
export type Board = Schemas["Board"];
export type BoardColumn = Schemas["BoardColumn"];
export type PipelineTotals = Schemas["PipelineTotals"];
export type PipelineSummary = Schemas["PipelineSummary"];
export type StageHistoryEntry = Schemas["StageHistory"];
export type StageHistoryPage = Schemas["StageHistoryPage"];
export type OpportunityListQuery = NonNullable<paths["/api/v1/workspaces/{workspace}/opportunities"]["get"]["parameters"]["query"]>;
export type OpportunityOrdering = NonNullable<OpportunityListQuery["ordering"]>;
export type OpportunityCreateRequest = Schemas["OpportunityCreateRequest"];
export type OpportunityUpdateRequest = Schemas["PatchedOpportunityUpdateRequest"];
export type OpportunityMoveRequest = Schemas["OpportunityMoveRequest"];
export type StageType = Schemas["StageTypeEnum"];
export type CustomField = Schemas["CustomField"];
export type FieldType = Schemas["FieldTypeEnum"];
export type FieldOption = Schemas["FieldOption"];
export type PipelineCreateRequest = Schemas["PipelineCreateRequest"];
export type PipelineRenameRequest = Schemas["PatchedPipelineRenameRequest"];
export type StageInput = Schemas["StageInputRequest"];
export type StagesReplaceRequest = Schemas["StagesReplaceRequest"];
export type FieldInput = Schemas["FieldInputRequest"];
export type FieldOptionInput = Schemas["FieldOptionInputRequest"];
export type FieldsReplaceRequest = Schemas["FieldsReplaceRequest"];
export type NegotiationPriceEntry = Schemas["NegotiationPrice"];
export type NegotiationPricePage = Schemas["NegotiationPricePage"];
export type NegotiatedPriceRequest = Schemas["NegotiatedPriceInputRequest"];

export type Activity = Schemas["Activity"];
export type ActivityListItem = Schemas["ActivityListItem"];
export type ActivityPage = Schemas["ActivityPage"];
export type ActivityType = Schemas["ActivityTypeEnum"];
export type ActivityStatus = Schemas["ActivityStatusEnum"];
export type ActivityPriority = Schemas["PriorityEnum"];
export type ActivityLeadRef = Schemas["ActivityLeadRef"];
export type ActivityOpportunityRef = Schemas["ActivityOpportunityRef"];
export type ActivitySummary = Schemas["ActivitySummary"];
export type ActivityCreateRequest = Schemas["ActivityCreateRequest"];
export type ActivityUpdateRequest = Schemas["PatchedActivityUpdateRequest"];
export type ActivityListQuery = NonNullable<paths["/api/v1/workspaces/{workspace}/activities"]["get"]["parameters"]["query"]>;
export type ActivityOrdering = NonNullable<ActivityListQuery["ordering"]>;
export type TimelineEntry = Schemas["TimelineEntry"];
export type TimelineActivity = Schemas["TimelineActivity"];
export type TimelineKind = Schemas["TimelineKindEnum"];
export type TimelinePage = Schemas["TimelinePage"];
export type Note = Schemas["Note"];
export type NotePage = Schemas["NotePage"];
export type Attachment = Schemas["Attachment"];
export type ScanStatus = Schemas["ScanStatusEnum"];

export type Dashboard = Schemas["Dashboard"];
export type DashboardLead = Schemas["DashboardLead"];
export type DashboardActivity = Schemas["DashboardActivity"];

export type SearchResults = Schemas["SearchResults"];
export type SearchLead = Schemas["SearchLead"];
export type SearchOpportunity = Schemas["SearchOpportunity"];
export type SearchTask = Schemas["SearchTask"];
export type SearchMeeting = Schemas["SearchMeeting"];
export type SearchNote = Schemas["SearchNote"];

// --- Ask Arkray (Phase 8) ----------------------------------------------------------------------
export type AskStatus = Schemas["AskStatus"];
export type AskQuestion = Schemas["Question"];
export type AskAnswer = Schemas["Answer"];
export type AskAnswerBlock = Schemas["AnswerBlock"];
export type AskAnswerPart = Schemas["AnswerPart"];
export type AskFact = Schemas["AnswerFact"];
export type AskSource = Schemas["AnswerSource"];
export type AskCitation = Schemas["AnswerCitation"];
export type AskConversation = Schemas["Conversation"];
export type AskConversationSummary = Schemas["ConversationSummary"];
export type AskRecordKind = Schemas["AskRecordKindEnum"];
export type AskRequest = Schemas["AskInputRequest"];
