"use client";

import { TextField } from "@/components/ui/Field";
import { parseAmountInput } from "@/lib/money";

/** The agreed CPT is free text, like Expected CPT: no unit is defined (ADR-0029). */
export const AGREED_CPT_MAX_LENGTH = 100;

/**
 * What entering a negotiation stage asks for, always together (ADR-0029): the agreed price,
 * an exact decimal string, and the agreed CPT as the salesperson states it.
 */
export interface AgreedTerms {
  price: string;
  cpt: string;
}

export type ParsedTerms = { ok: true; value: AgreedTerms } | { ok: false; errors: { price?: string[]; cpt?: string[] } };

/** Both are required; every problem is reported at once. The CPT is one line, as the server keeps it. */
export function parseAgreedTerms(price: string, cpt: string): ParsedTerms {
  const amount = parseAmountInput(price);
  const text = cpt.split(/\s+/).filter(Boolean).join(" ");
  const errors: { price?: string[]; cpt?: string[] } = {};
  if (!amount.ok) errors.price = [price.trim() ? amount.error : "Enter the agreed price."];
  if (!text) errors.cpt = ["Enter the agreed CPT."];
  else if (text.length > AGREED_CPT_MAX_LENGTH) errors.cpt = [`Use at most ${AGREED_CPT_MAX_LENGTH} characters.`];
  if (amount.ok && !errors.cpt) return { ok: true, value: { price: amount.value, cpt: text } };
  return { ok: false, errors };
}

interface AgreedTermsFieldsProps {
  price: string;
  cpt: string;
  onPriceChange: (value: string) => void;
  onCptChange: (value: string) => void;
  priceErrors?: readonly string[];
  cptErrors?: readonly string[];
  /** The request field the price is sent as (`negotiated_price` on a move or create, `price` on a revision). */
  priceName?: string;
  /** Focus the price when the dialog opens. */
  autoFocus?: boolean;
}

/** The agreed price and agreed CPT inputs, wherever a deal enters (or stays in) negotiation. */
export function AgreedTermsFields({
  price,
  cpt,
  onPriceChange,
  onCptChange,
  priceErrors,
  cptErrors,
  priceName = "negotiated_price",
  autoFocus = false,
}: AgreedTermsFieldsProps) {
  return (
    <>
      <TextField
        label="Agreed price (₹)"
        name={priceName}
        inputMode="decimal"
        autoComplete="off"
        value={price}
        onChange={(e) => onPriceChange(e.target.value)}
        errors={priceErrors}
        data-autofocus={autoFocus || undefined}
      />
      <TextField
        label="Agreed CPT"
        name="agreed_cpt"
        autoComplete="off"
        maxLength={AGREED_CPT_MAX_LENGTH}
        value={cpt}
        onChange={(e) => onCptChange(e.target.value)}
        errors={cptErrors}
      />
    </>
  );
}
