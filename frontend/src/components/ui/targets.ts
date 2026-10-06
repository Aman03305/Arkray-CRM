/*
 * Hit areas for small links (WCAG 2.2 2.5.8 asks at least 24 x 24 CSS px; the final audit
 * measured 18-20 px for one-line links in lists). Both grow what can be clicked or tapped
 * without moving anything on the page.
 */

/** A list row that opens its record from anywhere on it: the link's hit area is stretched
 * over the nearest positioned ancestor (give the row `relative`). Only for rows with no other
 * control in them: the stretched area would cover it. */
export const ROW_LINK = "after:absolute after:inset-0";

/** A one-line link or text button 8 px taller to hit (28 px for 14 px text), taking no more
 * room: the padding is given back as negative margin. On a block or inline-block element. */
export const TALLER_HIT = "-my-1 py-1";
