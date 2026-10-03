/**
 * Stage transitions as the UI presents them. The server's move operation is the only
 * authority (docs/pipeline.md#stage-transitions); this decides which confirmation to show
 * and how to place a card while the move is in flight.
 */
import type { Board, OpportunityCard, Stage, StageCategory } from "@/lib/api/types";

export type TransitionKind = "same" | "move" | "win" | "lose" | "reopen" | "reopen-first";

export function transitionKind(from: { stageId: string; status: StageCategory }, target: Stage): TransitionKind {
  if (from.stageId === target.id) return "same";
  const closed = from.status !== "open";
  if (closed && target.category !== "open") return "reopen-first";
  if (closed) return "reopen";
  if (target.category === "won") return "win";
  if (target.category === "lost") return "lose";
  return "move";
}

/** Where a move may go: active stages other than the current one; a closed opportunity
 * only reopens into an open stage. */
export function moveTargets(stages: readonly Stage[], from: { stageId: string; status: StageCategory }): Stage[] {
  return stages.filter((s) => s.is_active && s.id !== from.stageId && (from.status === "open" || s.category === "open"));
}

export function moveLabel(kind: TransitionKind, target: Stage): string {
  switch (kind) {
    case "win":
      return "Mark as won";
    case "lose":
      return "Mark as lost";
    case "reopen":
      return `Reopen in ${target.name}`;
    default:
      return `Move to ${target.name}`;
  }
}

/** Open columns list soonest-closing first (undated last); closed ones latest first. */
function placeIn(cards: readonly OpportunityCard[], card: OpportunityCard): OpportunityCard[] {
  if (card.status !== "open") return [card, ...cards];
  const key = (c: OpportunityCard) => c.expected_close_date ?? "9999-12-31";
  const index = cards.findIndex((c) => key(c) > key(card));
  return index === -1 ? [...cards, card] : [...cards.slice(0, index), card, ...cards.slice(index)];
}

/**
 * The board with one card moved to `target`, as it will look once the server agrees:
 * counts adjusted, the card's stage, status and stage-default probability updated. Money
 * totals are NOT recomputed here (the browser never does arithmetic on amounts): they
 * refresh from the server right after the move.
 */
export function moveCardInBoard(board: Board, cardId: string, target: Stage): Board {
  const source = board.columns.find((c) => c.cards.some((card) => card.id === cardId));
  const card = source?.cards.find((c) => c.id === cardId);
  if (!source || !card) return board;
  const moved: OpportunityCard = {
    ...card,
    stage_id: target.id,
    status: target.category,
    probability: target.probability,
    probability_overridden: false,
    closed_at: target.category === "open" ? null : new Date().toISOString(),
  };
  return {
    ...board,
    columns: board.columns.map((column) => {
      if (column === source) {
        return { ...column, count: Math.max(0, column.count - 1), cards: column.cards.filter((c) => c.id !== cardId) };
      }
      if (column.stage.id === target.id) {
        return { ...column, count: column.count + 1, cards: placeIn(column.cards, moved) };
      }
      return column;
    }),
  };
}
