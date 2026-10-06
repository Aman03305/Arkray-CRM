/**
 * A stage move is sent once per activation burst: a second move of the same card before the
 * first settles (a double click, Enter then a click in the dialog, both before the pending
 * state renders) would carry the same version and come back 409, "changed by someone else",
 * about the user's own first move.
 */
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { makeOpportunity, OPPORTUNITY_ID, STAGES } from "@/test/pipeline-fixtures";
import { createTestQueryClient, mockApi } from "@/test/render";

import { type MoveRequest, useMoveOpportunity } from "./useMoveOpportunity";

const ME: { kind: "self" } = { kind: "self" };
const OTHER_ID = "5e4d3c2b-1a09-4f8e-9d7c-6b5a49382716";
const path = (id: string) => `/api/v1/workspaces/me/opportunities/${id}/move`;

function setup() {
  const api = mockApi({
    [`POST ${path(OPPORTUNITY_ID)}`]: async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
      return { status: 200, body: makeOpportunity({ version: 2 }) };
    },
    [`POST ${path(OTHER_ID)}`]: { status: 200, body: makeOpportunity({ id: OTHER_ID, version: 2 }) },
  });
  const client = createTestQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const { result } = renderHook(() => useMoveOpportunity(ME), { wrapper });
  return { api, result };
}

const request = (id: string): MoveRequest => ({ id, title: "Deal", version: 1, target: STAGES.proposal });

describe("useMoveOpportunity", () => {
  it("sends one move for two activations before the first settles, and the next one after", async () => {
    const { api, result } = setup();
    const settled = vi.fn();
    act(() => {
      result.current.mutate(request(OPPORTUNITY_ID), { onSettled: settled });
      result.current.mutate(request(OPPORTUNITY_ID), { onSettled: settled });
    });
    await waitFor(() => expect(settled).toHaveBeenCalledTimes(1));
    expect(api.callsTo("POST", path(OPPORTUNITY_ID))).toHaveLength(1);
    // The guard opens again once it settled: a later, deliberate move is sent.
    act(() => result.current.mutate({ ...request(OPPORTUNITY_ID), version: 2 }, { onSettled: settled }));
    await waitFor(() => expect(settled).toHaveBeenCalledTimes(2));
    expect(api.callsTo("POST", path(OPPORTUNITY_ID))).toHaveLength(2);
  });

  it("doesn't hold back another card's move", async () => {
    const { api, result } = setup();
    act(() => {
      result.current.mutate(request(OPPORTUNITY_ID));
      result.current.mutate(request(OTHER_ID));
    });
    await waitFor(() => expect(api.callsTo("POST", path(OTHER_ID))).toHaveLength(1));
    await waitFor(() => expect(api.callsTo("POST", path(OPPORTUNITY_ID))).toHaveLength(1));
  });
});
