import type { MfaQrMatrix } from "@/lib/api/types";

/** Light modules around the code that scanners need (the QR standard's quiet zone). */
const QUIET_ZONE = 4;

/** True when the matrix is square and only "0"/"1": anything else isn't drawn. */
export function isValidMatrix(qr: MfaQrMatrix | null | undefined): qr is MfaQrMatrix {
  if (!qr || !Number.isInteger(qr.size) || qr.size < 1 || qr.size > 177) return false;
  return qr.rows.length === qr.size && qr.rows.every((row) => row.length === qr.size && /^[01]+$/.test(row));
}

/**
 * The path drawing the dark modules: one rectangle per horizontal run of dark modules
 * ("M x y h n v1 h-n z"), offset by the quiet zone.
 */
export function qrPath(qr: MfaQrMatrix): string {
  const parts: string[] = [];
  qr.rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      if (row[x] !== "1") {
        x += 1;
        continue;
      }
      let end = x;
      while (end < row.length && row[end] === "1") end += 1;
      const run = end - x;
      parts.push(`M${x + QUIET_ZONE} ${y + QUIET_ZONE}h${run}v1h-${run}z`);
      x = end;
    }
  });
  return parts.join("");
}

/**
 * The set-up QR code, drawn from the server's module matrix as SVG (ADR-0033): no image
 * data URI and no injected markup, so the page's content security policy holds. Colours are
 * presentation attributes, not inline styles.
 */
export function QrCode({ qr, label, className = "size-48" }: { qr: MfaQrMatrix; label: string; className?: string }) {
  if (!isValidMatrix(qr)) return null;
  const extent = qr.size + QUIET_ZONE * 2;
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${extent} ${extent}`}
      shapeRendering="crispEdges"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect width={extent} height={extent} fill="#ffffff" />
      <path d={qrPath(qr)} fill="#000000" data-qr-modules="" />
    </svg>
  );
}
