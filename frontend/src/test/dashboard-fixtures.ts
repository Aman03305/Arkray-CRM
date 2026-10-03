import type { Activity, Dashboard, DashboardActivity, DashboardLead } from "@/lib/api/types";

import { makeActivity, makeMeeting } from "./activity-fixtures";
import { LEAD_ID, PRIYA_ID, RAHUL_ID } from "./fixtures";

export const RAHUL = { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true };
export const PRIYA = { id: PRIYA_ID, full_name: "Priya Patel", is_active: true };

export function makeDashboardLead(overrides: Partial<DashboardLead> = {}): DashboardLead {
  return {
    id: LEAD_ID,
    display_name: "Asha Mehta",
    organization_name: "Apollo Diagnostics",
    owner: RAHUL,
    created_at: "2026-10-03T04:42:00Z", // 10:12 am IST
    ...overrides,
  };
}

/** A meeting or task as a dashboard row: only what the dashboard shows. */
export function asRow(activity: Activity): DashboardActivity {
  const { id, type, title, status, due_at, starts_at, is_overdue, lead, owner } = activity;
  return { id, type, title, status, due_at, starts_at, is_overdue, lead, owner };
}

export function makeDashboard(overrides: Partial<Dashboard> = {}): Dashboard {
  return {
    currency: "INR",
    time_zone: "Asia/Kolkata",
    business_date: "2026-10-03",
    leads: { total: 1234, new_today: 7 },
    pipeline: { pipeline_value: "1500000.00", weighted_pipeline: "900000.00", open_count: 2 },
    activities: { open_tasks: 9, tasks_due_today: 2, overdue_tasks: 1, meetings_today: 3, upcoming_meetings: 6 },
    new_leads: [makeDashboardLead()],
    upcoming_meetings: [asRow(makeMeeting())],
    next_tasks: [asRow(makeActivity({ is_overdue: true, due_at: "2026-10-01T06:30:00Z" }))],
    ...overrides,
  };
}

export const EMPTY_DASHBOARD: Dashboard = makeDashboard({
  leads: { total: 0, new_today: 0 },
  pipeline: { pipeline_value: "0.00", weighted_pipeline: "0.00", open_count: 0 },
  activities: { open_tasks: 0, tasks_due_today: 0, overdue_tasks: 0, meetings_today: 0, upcoming_meetings: 0 },
  new_leads: [],
  upcoming_meetings: [],
  next_tasks: [],
});

/** Two users' dashboards whose every value differs, so any mix-up is visible. */
export const RAHUL_DASHBOARD: Dashboard = makeDashboard({
  leads: { total: 111, new_today: 11 },
  pipeline: { pipeline_value: "111111.00", weighted_pipeline: "55555.50", open_count: 1 },
  activities: { open_tasks: 13, tasks_due_today: 1, overdue_tasks: 0, meetings_today: 4, upcoming_meetings: 5 },
  new_leads: [makeDashboardLead({ id: "11111111-1111-4111-8111-111111111111", display_name: "Rahul's prospect" })],
  upcoming_meetings: [asRow(makeMeeting({ title: "Rahul's demo" }))],
  next_tasks: [asRow(makeActivity({ title: "Rahul's quotation" }))],
});

export const PRIYA_DASHBOARD: Dashboard = makeDashboard({
  leads: { total: 777, new_today: 77 },
  pipeline: { pipeline_value: "777777.00", weighted_pipeline: "77777.70", open_count: 7 },
  activities: { open_tasks: 70, tasks_due_today: 7, overdue_tasks: 17, meetings_today: 27, upcoming_meetings: 37 },
  new_leads: [
    makeDashboardLead({ id: "77777777-7777-4777-8777-777777777777", display_name: "Priya's prospect", owner: PRIYA }),
  ],
  upcoming_meetings: [asRow(makeMeeting({ title: "Priya's review", owner: PRIYA }))],
  next_tasks: [asRow(makeActivity({ title: "Priya's follow-up", owner: PRIYA }))],
});

/** Text that appears only on Rahul's dashboard. */
export const RAHUL_ONLY = ["1,11,111", "55,555.50", "Rahul's prospect", "Rahul's demo", "Rahul's quotation"];
