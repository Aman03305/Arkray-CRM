/* Browser acceptance: the second-factor step-up on reset and delete (ADR-0036, ADR-0033).
 *
 *   ACCEPT_MFA=true scripts/browser/acceptance-stack.sh up
 *   (cd backend && SECRET_FILE=/tmp/mira-secret.txt DATABASE_URL=postgres://arkray:arkray_dev_only_password@127.0.0.1:55532/arkray \
 *      DJANGO_SETTINGS_MODULE=config.settings.local uv run python ../scripts/browser/seed_account_lifecycle_mfa.py)
 *   (cd backend && SECRET_FILE=/tmp/mira-secret.txt DATABASE_URL=... DJANGO_SETTINGS_MODULE=config.settings.local \
 *      node ../scripts/browser/account-lifecycle-mfa.cjs)
 *
 * The backend runs with MFA_REQUIRED_FOR_ADMINS=true. Mira is a Super admin with an enrolled
 * authenticator. After she signs in with her code, the session's last verification is aged 20
 * minutes (tamper_mfa_session.py: the step-up window is 15), so the server answers 403
 * mfa_required to the sensitive action and the app asks for a fresh code, then repeats the
 * action. Run from backend/ (the aging script needs its settings and the database URL).
 */
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const BASE = process.env.BASE_URL || "http://localhost:3100";
const BACKEND = process.env.BACKEND_DIR || path.join(__dirname, "..", "..", "backend");
const SECRET = fs.readFileSync(process.env.SECRET_FILE, "utf8").trim();
const SHOTS = process.env.SHOTS || path.join(__dirname, "shots");
const PASSWORD = "correct-horse-battery-staple";
fs.mkdirSync(SHOTS, { recursive: true });

function base32(secret) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of secret.toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
function hotp(key, counter) {
  const buf = Buffer.alloc(8);
  buf.writeBigUInt64BE(BigInt(counter));
  const mac = crypto.createHmac("sha1", key).update(buf).digest();
  const offset = mac[mac.length - 1] & 0xf;
  const code = ((mac[offset] & 0x7f) << 24) | (mac[offset + 1] << 16) | (mac[offset + 2] << 8) | mac[offset + 3];
  return String(code % 1000000).padStart(6, "0");
}
let lastStep = -1;
/** A code for a step nobody used yet (the server refuses a repeat), within its one step of drift. */
async function nextCode() {
  for (;;) {
    const current = Math.floor(Date.now() / 30000);
    const step = Math.max(current, lastStep + 1);
    if (step <= current + 1) {
      lastStep = step;
      return hotp(base32(SECRET), step);
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

const results = [];
const problems = [];
async function step(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`PASS  ${name}`);
  } catch (error) {
    results.push({ name, ok: false, error: String(error.message || error).split("\n")[0] });
    console.log(`FAIL  ${name}\n      ${String(error.message || error).split("\n").slice(0, 5).join("\n      ")}`);
  }
}
const expect = (c, m) => {
  if (!c) throw new Error(m);
};

function ageTheVerification(sessionKey) {
  const out = execFileSync("uv", ["run", "--frozen", "python", path.join(__dirname, "tamper_mfa_session.py"), sessionKey], {
    cwd: BACKEND,
    encoding: "utf8",
    env: process.env,
  });
  expect(out.includes("aged"), `could not age the session: ${out}`);
}
const alertWithText = (scope, text) => scope.locator('[role="alert"]').filter({ hasText: text }).first();

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !/status of 4\d\d/.test(m.text())) problems.push(`console: ${m.text().slice(0, 160)}`);
  });

  await step("Mira signs in with her password and her authenticator code", async () => {
    await page.goto(`${BASE}/login`);
    await page.locator('input[name="email"]').fill("mira@acme.test");
    await page.locator('input[name="password"]').fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await page.locator('input[name="code"]').waitFor({ timeout: 30000 });
    await page.locator('input[name="code"]').fill(await nextCode());
    await page.getByRole("button", { name: /Verify|Sign in|Continue/ }).first().click();
    await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 30000 });
  });

  const sessionKey = async () => (await context.cookies()).find((c) => c.name === "arkray_session")?.value;

  await page.goto(`${BASE}/admin/users`);
  await page.getByRole("table").waitFor();
  const openMenu = async (name) => {
    const trigger = page.getByRole("button", { name: `Actions for ${name}`, exact: true });
    await trigger.scrollIntoViewIfNeeded();
    await page.waitForTimeout(250);
    await trigger.click();
    await page.getByRole("menu").waitFor();
  };

  await step("Right after signing in, a reset link needs no new code", async () => {
    await openMenu("Ravi Second");
    await page.getByRole("menuitem", { name: "Reset password" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Send reset link" }).click();
    await page.getByText("Reset link sent to ravi@acme.test.").waitFor({ timeout: 20000 });
    expect((await page.getByText("Confirm it's you").count()) === 0, "asked for a code right after a verified sign-in");
  });

  await step("An old verification: reset password asks for a code, a wrong code is refused, the right one repeats the action", async () => {
    ageTheVerification(await sessionKey());
    await openMenu("Quinn Target");
    await page.getByRole("menuitem", { name: "Reset password" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Send reset link" }).click();
    const prompt = page.getByRole("dialog", { name: "Confirm it's you" });
    await prompt.waitFor({ timeout: 20000 });
    await page.screenshot({ path: path.join(SHOTS, "07-step-up.png") });
    await prompt.locator('input[name="code"]').fill("000000");
    await prompt.getByRole("button", { name: "Verify" }).click();
    await prompt.getByText(/That code didn.t work/).waitFor({ timeout: 20000 });
    await page.screenshot({ path: path.join(SHOTS, "08-wrong-code.png") });
    expect((await page.getByText("Reset link sent to quinn@acme.test.").count()) === 0, "the action ran without a valid code");
    await prompt.locator('input[name="code"]').fill(await nextCode());
    await prompt.getByRole("button", { name: "Verify" }).click();
    await page.getByText("Reset link sent to quinn@acme.test.").waitFor({ timeout: 30000 });
  });

  await step("Cancelling the code prompt leaves the account untouched (delete)", async () => {
    ageTheVerification(await sessionKey());
    await openMenu("Quinn Target");
    await page.getByRole("menuitem", { name: "Delete account" }).click();
    const dialog = page.getByRole("alertdialog");
    await dialog.getByLabel("Type their email to confirm").waitFor({ timeout: 20000 });
    await dialog.getByLabel("Type their email to confirm").fill("quinn@acme.test");
    await dialog.getByRole("button", { name: "Delete account" }).click();
    const prompt = page.getByRole("dialog", { name: "Confirm it's you" });
    await prompt.waitFor({ timeout: 20000 });
    await prompt.getByRole("button", { name: "Cancel" }).click();
    expect((await page.getByText("Quinn Target was deleted.").count()) === 0, "deleted without the code");
    const list = await page.evaluate(async () => (await (await fetch("/api/v1/admin/users?q=quinn")).json()).results.map((u) => u.email));
    expect(list.includes("quinn@acme.test"), `Quinn is gone: ${list}`);
  });

  await step("Delete asks for a code and, once verified, finishes", async () => {
    // The dialog is still open after the cancelled prompt (nothing was sent): submit again.
    const dialog = page.getByRole("alertdialog");
    if (!(await dialog.count())) {
      await openMenu("Quinn Target");
      await page.getByRole("menuitem", { name: "Delete account" }).click();
      await page.getByRole("alertdialog").getByLabel("Type their email to confirm").fill("quinn@acme.test");
    }
    await page.getByRole("alertdialog").getByRole("button", { name: "Delete account" }).click();
    const prompt = page.getByRole("dialog", { name: "Confirm it's you" });
    await prompt.waitFor({ timeout: 20000 });
    await prompt.locator('input[name="code"]').fill(await nextCode());
    await prompt.getByRole("button", { name: "Verify" }).click();
    await page.getByText("Quinn Target was deleted.").waitFor({ timeout: 60000 });
  });

  await step("No page errors or unexpected console errors", async () => {
    expect(problems.length === 0, problems.slice(0, 5).join(" | "));
  });

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} step-up checks passed`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => {
  console.error("harness failure:", e);
  process.exit(2);
});
