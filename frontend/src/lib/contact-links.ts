const EXTENSION = /\s*(?:;ext=|extension|ext\.?|x|#)\s*([0-9]+)$/i;

/**
 * A dialable tel: URI (RFC 3966) for a number as the user typed it. The extension is kept
 * as ";ext=", never glued onto the number (which would dial a different number).
 */
export function telHref(value: string): string {
  const match = EXTENSION.exec(value);
  const number = (match ? value.slice(0, match.index) : value).replace(/[^\d+]/g, "");
  return `tel:${number}${match ? `;ext=${match[1]}` : ""}`;
}

/**
 * A mailto: link that opens a draft to this one address and nothing else. Characters that
 * delimit mailto headers are encoded (RFC 6068), so a stored address such as
 * "x?bcc=spy@evil.example&body=..." can't add recipients or text (Phase 9 review; the API
 * also refuses such addresses now, this covers ones stored before).
 */
export function mailtoHref(email: string): string {
  return `mailto:${email.replace(/[%?&=#,;\s"<>\\]/g, (char) => encodeURIComponent(char))}`;
}
