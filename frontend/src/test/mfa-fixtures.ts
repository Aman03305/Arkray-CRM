import type { MfaSetup, MfaStatus } from "@/lib/api/types";

/** A tiny QR matrix (a real one is 21 to 57 modules square) with 7 dark modules. */
export const QR_FIXTURE = { size: 4, rows: ["1011", "0100", "1110", "0000"] };
export const QR_FIXTURE_DARK_MODULES = 7;

export const MFA_SETUP: MfaSetup = {
  secret: "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
  otpauth_uri: "otpauth://totp/Arkray%20CRM:anita%40example.test?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Arkray%20CRM",
  qr: QR_FIXTURE,
};

export const RECOVERY_CODES = Array.from({ length: 10 }, (_, i) => `AAAA-BBBB-CCCC-${String(i).padStart(4, "0")}`);

export function makeMfaStatus(overrides: Partial<MfaStatus> = {}): MfaStatus {
  return { enabled: false, confirmed_at: null, recovery_codes_remaining: 0, required: false, can_disable: true, lock: null, ...overrides };
}

/** The dark modules a drawn QR path covers: the sum of its runs ("M x y h n v1 h-n z"). */
export function darkModules(svg: Element): number {
  const d = svg.querySelector("path")?.getAttribute("d") ?? "";
  return [...d.matchAll(/h(\d+)v1/g)].reduce((sum, match) => sum + Number(match[1]), 0);
}
