import { describe, expect, it } from "vitest";

import { makeBoard, makeCard, OTHER_OPPORTUNITY_ID, STAGE_LIST, STAGES } from "@/test/pipeline-fixtures";

import { moveCardInBoard, moveLabel, moveTargets, transitionKind } from "./transitions";

const open = { stageId: STAGES.proposal.id, status: "open" as const };
const won = { stageId: STAGES.won.id, status: "won" as const };

describe("transition kinds", () => {
  it.each([
    [open, STAGES.proposal, "same"],
    [open, STAGES.negotiation, "move"],
    [open, STAGES.new, "move"],
    [open, STAGES.won, "win"],
    [open, STAGES.lost, "lose"],
    [won, STAGES.negotiation, "reopen"],
    [won, STAGES.lost, "reopen-first"],
  ] as const)("%o -> %s is %s", (from, target, kind) => {
    expect(transitionKind(from, target)).toBe(kind);
  });

  it("offers every other active stage, and only open ones to a closed opportunity", () => {
    expect(moveTargets(STAGE_LIST, open).map((s) => s.key)).toEqual(["new", "qualified", "negotiation", "won", "lost"]);
    expect(moveTargets(STAGE_LIST, won).map((s) => s.key)).toEqual(["new", "qualified", "proposal", "negotiation"]);
    const retired = STAGE_LIST.map((s) => (s.key === "qualified" ? { ...s, is_active: false } : s));
    expect(moveTargets(retired, open).map((s) => s.key)).not.toContain("qualified");
  });

  it("labels", () => {
    expect(moveLabel("win", STAGES.won)).toBe("Mark as won");
    expect(moveLabel("reopen", STAGES.new)).toBe("Reopen in New");
    expect(moveLabel("move", STAGES.negotiation)).toBe("Move to Negotiation");
  });
});

describe("the optimistic board", () => {
  it("moves the card, adjusts counts and never touches money totals", () => {
    const board = makeBoard([makeCard(), makeCard({ id: OTHER_OPPORTUNITY_ID, stage_id: STAGES.negotiation.id, expected_close_date: "2027-01-01" })]);
    const after = moveCardInBoard(board, makeCard().id, STAGES.negotiation);
    const negotiation = after.columns.find((c) => c.stage.key === "negotiation")!;
    const proposal = after.columns.find((c) => c.stage.key === "proposal")!;
    expect(proposal.cards).toEqual([]);
    expect(proposal.count).toBe(0);
    expect(negotiation.count).toBe(2);
    // Soonest close first: 2026-12-15 before 2027-01-01.
    expect(negotiation.cards.map((c) => c.id)).toEqual([makeCard().id, OTHER_OPPORTUNITY_ID]);
    expect(negotiation.cards[0]).toMatchObject({ status: "open", probability: "75.00", probability_overridden: false });
    expect(after.totals).toBe(board.totals);
    expect(negotiation.total_value).toBe(board.columns.find((c) => c.stage.key === "negotiation")!.total_value);
  });

  it("puts a closed card first and marks it closed", () => {
    const after = moveCardInBoard(makeBoard([makeCard()]), makeCard().id, STAGES.won);
    const column = after.columns.find((c) => c.stage.key === "won")!;
    expect(column.cards[0]).toMatchObject({ status: "won", probability: "100.00" });
    expect(column.cards[0]!.closed_at).not.toBeNull();
  });

  it("leaves the board alone for an unknown card", () => {
    const board = makeBoard();
    expect(moveCardInBoard(board, "nope", STAGES.won)).toBe(board);
  });
});
