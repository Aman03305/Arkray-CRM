/* Browser acceptance for ADR-0036 (delete account, reset password) against the isolated stack.
 *
 *   scripts/browser/acceptance-stack.sh up
 *   (cd backend && DATABASE_URL=postgres://arkray:arkray_dev_only_password@127.0.0.1:55532/arkray \
 *      DJANGO_SETTINGS_MODULE=config.settings.local uv run python ../scripts/browser/seed_account_lifecycle.py)
 *   node scripts/browser/account-lifecycle.cjs
 *   scripts/browser/acceptance-stack.sh down
 *
 * Real Chromium, the real Next.js app and the real API; accounts come from the seed script. The
 * stack must have been started with the second factor off (the default). Environment:
 *   BASE_URL (default http://localhost:3100)  MAILPIT_URL (default http://127.0.0.1:58225)
 *   PLAYWRIGHT_MODULE  the playwright package to require (default: `playwright`)
 *   SHOTS  directory for screenshots (default: ./shots next to this file)
 */
const fs = require("node:fs");
const path = require("node:path");

const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const BASE = process.env.BASE_URL || "http://localhost:3100";
const MAIL = process.env.MAILPIT_URL || "http://127.0.0.1:58225";
const SHOTS = process.env.SHOTS || path.join(__dirname, "shots");
const PASSWORD = "correct-horse-battery-staple";
const FRESH_PASSWORD = "Lantern-Orchard-Quiet-Harbour-52";
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
const problems = []; // page errors and unexpected console errors, with where they happened
let current = "";

async function step(name, fn) {
  current = name;
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`PASS  ${name}`);
  } catch (error) {
    results.push({ name, ok: false, error: String(error.message || error).split("\n").slice(0, 6).join(" | ") });
    console.log(`FAIL  ${name}\n      ${String(error.message || error).split("\n").slice(0, 6).join("\n      ")}`);
  }
}

function watch(page, label) {
  page.on("pageerror", (error) => problems.push(`[${label}] pageerror during "${current}": ${error.message}`));
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // The negative checks below make the browser log 4xx responses: those are expected.
    if (/Failed to load resource: the server responded with a status of 4\d\d/.test(text)) return;
    problems.push(`[${label}] console.error during "${current}": ${text.slice(0, 200)}`);
  });
}

async function openContext(browser, label, viewport = { width: 1280, height: 900 }) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  watch(page, label);
  return { context, page };
}

async function signIn(browser, email, password = PASSWORD, viewport) {
  const { context, page } = await openContext(browser, email, viewport);
  await page.goto(`${BASE}/login`);
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 30000 });
  return { context, page };
}

async function openUsers(page) {
  await page.goto(`${BASE}/admin/users`);
  await page.getByRole("table").waitFor({ timeout: 30000 });
  await page.getByRole("button", { name: /^Actions for / }).first().waitFor({ timeout: 30000 });
}

const rowMenu = (page, name) => page.getByRole("button", { name: `Actions for ${name}`, exact: true });

/** Opens a row's menu like a person: scrolled into view first (the menu closes on any scroll). */
async function openMenu(page, name) {
  const trigger = rowMenu(page, name);
  await trigger.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  await trigger.click();
  await page.getByRole("menu").waitFor({ timeout: 10000 });
}

/** An alert with something in it (Next's empty route announcer is a role=alert too). */
const alertWithText = (scope, text = /\S/) => scope.locator('[role="alert"]').filter({ hasText: text }).first();

async function menuItems(page, name) {
  await openMenu(page, name);
  const items = await page.getByRole("menuitem").allInnerTexts();
  await page.keyboard.press("Escape");
  return items.map((s) => s.trim());
}

async function shot(page, name) {
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: false });
}

function expect(condition, message) {
  if (!condition) throw new Error(message);
}

/** The newest mail to `address` (waiting for the outbox worker), with its text. */
async function mailTo(address, { after = 0, timeoutMs = 60000, containing } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = await (await fetch(`${MAIL}/api/v1/search?query=${encodeURIComponent(`to:${address}`)}`)).json();
    const messages = (found.messages || []).filter((m) => new Date(m.Created).getTime() >= after);
    for (const m of messages) {
      const full = await (await fetch(`${MAIL}/api/v1/message/${m.ID}`)).json();
      if (!containing || containing.test(full.Text || "")) return { subject: full.Subject, text: full.Text || "" };
    }
    if (Date.now() > deadline) throw new Error(`no mail to ${address}${containing ? ` matching ${containing}` : ""}`);
    await new Promise((r) => setTimeout(r, 1000));
  }
}

/** A same-origin API call from the signed-in page (cookies and the CSRF header as the app sends them). */
async function api(page, method, url, body) {
  return page.evaluate(
    async ({ method, url, body }) => {
      const read = () =>
        ["__Host-arkray_csrftoken", "arkray_csrftoken"]
          .map((name) => document.cookie.split("; ").find((c) => c.startsWith(`${name}=`))?.split("=")[1])
          .find(Boolean) ?? "";
      if (method !== "GET" && !read()) await fetch("/api/v1/auth/csrf", { credentials: "same-origin" });
      const csrf = read();
      if (method !== "GET" && !csrf) throw new Error("no CSRF cookie to echo");
      const response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      let json = null;
      try {
        json = await response.json();
      } catch {
        /* no body */
      }
      return { status: response.status, json, code: json && json.error ? json.error.code : null };
    },
    { method, url, body },
  );
}

async function directory(page) {
  const out = await api(page, "GET", "/api/v1/admin/users?page_size=50");
  expect(out.status === 200, `user list answered ${out.status}`);
  return Object.fromEntries(out.json.results.map((u) => [u.email, u]));
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const started = Date.now();
  const hasMailpit = await fetch(`${MAIL}/api/v1/messages`).then((r) => r.ok).catch(() => false);
  expect(hasMailpit, "Mailpit is not reachable");

  // ------------------------------------------------------------------ Super admin: what is offered
  const anita = await signIn(browser, "anita@acme.test");
  await openUsers(anita.page);
  let users = await directory(anita.page);

  await step("Super admin sees every account and the right actions on each row", async () => {
    for (const email of ["tara@acme.test", "omar@acme.test", "rahul@acme.test", "priya@acme.test", "neha@acme.test", "vikram@acme.test"]) {
      const items = await menuItems(anita.page, users[email].full_name);
      expect(items.includes("Reset password"), `${email}: no Reset password in ${items}`);
      expect(items.includes("Delete account"), `${email}: no Delete account in ${items}`);
    }
    const invited = await menuItems(anita.page, "Sana Invited");
    expect(!invited.includes("Reset password"), "an invited account is offered a reset link");
    expect(invited.includes("Delete account"), "an invited account can't be deleted");
    // Never a Super admin's account, and never one's own.
    for (const name of ["Bina Boss", "Anita Admin"]) {
      const items = await menuItems(anita.page, name);
      expect(!items.includes("Reset password") && !items.includes("Delete account"), `${name}: ${items}`);
    }
  });

  await step("Super admin's drawer offers Reset password and Delete account for an Admin", async () => {
    await anita.page.getByRole("button", { name: "Tara Lead", exact: true }).click();
    const drawer = anita.page.getByRole("dialog");
    await drawer.getByRole("button", { name: "Reset password" }).waitFor();
    await drawer.getByRole("button", { name: "Delete account" }).waitFor();
    expect(
      (await drawer.innerText()).includes("Temporary password and support access: Not available for administrators."),
      "the drawer doesn't say why a temporary password isn't offered for an Admin",
    );
    await shot(anita.page, "01-drawer-admin");
    await anita.page.keyboard.press("Escape");
  });

  // ------------------------------------------------------------------ Reset password (Super admin -> User)
  const rahulOld = await signIn(browser, "rahul@acme.test");
  await step("Rahul is signed in before the reset", async () => {
    await rahulOld.page.goto(`${BASE}/pipeline`);
    await rahulOld.page.waitForLoadState("networkidle");
    expect(!rahulOld.page.url().includes("/login"), "Rahul wasn't signed in");
  });

  let resetLink = "";
  await step("Super admin resets Rahul's password: dialog, success notice, email to Rahul's own address", async () => {
    const sentAt = Date.now() - 2000;
    await openMenu(anita.page, "Rahul Sharma");
    await anita.page.getByRole("menuitem", { name: "Reset password" }).click();
    const dialog = anita.page.getByRole("alertdialog");
    await dialog.waitFor();
    const text = await dialog.innerText();
    expect(/Rahul Sharma/.test(text) && /rahul@acme\.test/.test(text) && /User/.test(text), `dialog lacks name/email/role: ${text}`);
    expect(/signed out everywhere/.test(text) && /never see or choose/.test(text), "dialog doesn't say what happens");
    await shot(anita.page, "02-reset-dialog");
    await dialog.getByRole("button", { name: "Send reset link" }).click();
    await anita.page.getByText("Reset link sent to rahul@acme.test.").waitFor({ timeout: 20000 });
    const mail = await mailTo("rahul@acme.test", { after: sentAt, containing: /reset-password\// });
    expect(/An administrator reset the password/.test(mail.text), `the email doesn't say who: ${mail.text}`);
    expect(!/you can ignore this email/.test(mail.text), "the email advises ignoring it");
    const match = mail.text.match(/(https?:\/\/[^\s]+\/reset-password\/[A-Za-z0-9_-]{43})/);
    expect(match, `no link in: ${mail.text}`);
    resetLink = match[1];
  });

  await step("Rahul's existing session ended and his old password no longer works", async () => {
    await rahulOld.page.goto(`${BASE}/pipeline`);
    await rahulOld.page.waitForURL(/\/login/, { timeout: 30000 });
    const attempt = await openContext(browser, "rahul-old-password");
    await attempt.page.goto(`${BASE}/login`);
    await attempt.page.locator('input[name="email"]').fill("rahul@acme.test");
    await attempt.page.locator('input[name="password"]').fill(PASSWORD);
    await attempt.page.getByRole("button", { name: "Sign in", exact: true }).click();
    await alertWithText(attempt.page).waitFor({ timeout: 20000 });
    expect(attempt.page.url().includes("/login"), "the old password still signed Rahul in");
    await attempt.context.close();
  });

  await step("Rahul chooses a new password through the single-use link, which then stops working", async () => {
    const fresh = await openContext(browser, "rahul-reset");
    await fresh.page.goto(resetLink.replace(/^https?:\/\/[^/]+/, BASE));
    await fresh.page.locator('input[name="new-password"]').fill(FRESH_PASSWORD);
    await fresh.page.locator('input[name="confirm-password"]').fill(FRESH_PASSWORD);
    await fresh.page.getByRole("button", { name: "Change password" }).click();
    await fresh.page.waitForURL(/\/login/, { timeout: 30000 });
    await fresh.context.close();
    const again = await openContext(browser, "rahul-reused-link");
    await again.page.goto(resetLink.replace(/^https?:\/\/[^/]+/, BASE));
    await again.page.locator('input[name="new-password"]').fill("Another-Phrase-Not-Used-Before-88");
    await again.page.locator('input[name="confirm-password"]').fill("Another-Phrase-Not-Used-Before-88");
    await again.page.getByRole("button", { name: "Change password" }).click();
    await alertWithText(again.page, /invalid|expired/i).waitFor({ timeout: 20000 });
    await again.context.close();
    const rahulNew = await signIn(browser, "rahul@acme.test", FRESH_PASSWORD);
    await rahulNew.context.close();
  });

  // ------------------------------------------------------------------ Reset an Admin's password (Super admin only)
  await step("Super admin resets an Admin's password; the link goes to the Admin's own address", async () => {
    const sentAt = Date.now() - 2000;
    await openMenu(anita.page, "Omar Khan");
    await anita.page.getByRole("menuitem", { name: "Reset password" }).click();
    const dialog = anita.page.getByRole("alertdialog");
    expect(/Admin/.test(await dialog.innerText()), "the dialog doesn't show the Admin role");
    await dialog.getByRole("button", { name: "Send reset link" }).click();
    await anita.page.getByText("Reset link sent to omar@acme.test.").waitFor({ timeout: 20000 });
    await mailTo("omar@acme.test", { after: sentAt, containing: /reset-password\// });
  });

  // ------------------------------------------------------------------ Admin: reach, reset and the API refusing the rest
  const tara = await signIn(browser, "tara@acme.test");
  await openUsers(tara.page);
  const taraSees = await tara.page.locator("tbody tr").evaluateAll((rows) => rows.map((r) => r.querySelector("td")?.innerText.trim()));

  await step("Admin sees only herself and her team", async () => {
    const wanted = ["Tara Lead", "Rahul Sharma", "Priya Patel", "Mohan Das", "Sana Invited"];
    for (const name of wanted) expect(taraSees.some((n) => n?.startsWith(name)), `${name} missing from ${taraSees}`);
    for (const name of ["Omar Khan", "Neha Rao", "Vikram Free", "Anita Admin", "Bina Boss"]) {
      expect(!taraSees.some((n) => n?.startsWith(name)), `${name} is visible to an Admin: ${taraSees}`);
    }
  });

  await step("Admin gets Reset password and Delete account for her users, nothing for herself", async () => {
    for (const name of ["Rahul Sharma", "Priya Patel", "Mohan Das"]) {
      const items = await menuItems(tara.page, name);
      expect(items.includes("Reset password") && items.includes("Delete account"), `${name}: ${items}`);
    }
    const mine = await menuItems(tara.page, "Tara Lead");
    expect(!mine.includes("Reset password") && !mine.includes("Delete account"), `own row: ${mine}`);
  });

  await step("Admin resets her user's password from the drawer", async () => {
    const sentAt = Date.now() - 2000;
    await tara.page.getByRole("button", { name: "Mohan Das", exact: true }).click();
    const drawer = tara.page.getByRole("dialog");
    await drawer.getByRole("button", { name: "Reset password" }).click();
    const dialog = tara.page.getByRole("alertdialog");
    await dialog.getByRole("button", { name: "Send reset link" }).click();
    await tara.page.getByText("Reset link sent to mohan@acme.test.").first().waitFor({ timeout: 20000 });
    await mailTo("mohan@acme.test", { after: sentAt, containing: /reset-password\// });
    await tara.page.keyboard.press("Escape");
  });

  await step("The API refuses an Admin everything outside her authority, identically to a missing account", async () => {
    const missing = "00000000-0000-4000-8000-000000000001";
    const probe = async (method, url, body) => {
      const out = await api(tara.page, method, url, body);
      expect(out.code !== "csrf_failed", `${method} ${url} was refused for its CSRF token, not for authorization`);
      return out.status;
    };
    const omar = users["omar@acme.test"], anitaU = users["anita@acme.test"], neha = users["neha@acme.test"];
    const self = users["tara@acme.test"];
    const body = { version: 1, confirm_email: "x@acme.test" };
    for (const target of [omar, anitaU, neha]) {
      expect((await probe("POST", `/api/v1/admin/users/${target.id}/reset-password`)) === 404, `reset ${target.email}`);
      expect((await probe("POST", `/api/v1/admin/users/${target.id}/delete`, { ...body, version: target.version, confirm_email: target.email })) === 404, `delete ${target.email}`);
      expect((await probe("GET", `/api/v1/admin/users/${target.id}/deletion-preview`)) === 404, `preview ${target.email}`);
    }
    expect((await probe("POST", `/api/v1/admin/users/${missing}/delete`, body)) === 404, "a missing id differs");
    expect((await probe("POST", `/api/v1/admin/users/${self.id}/reset-password`)) === 422, "own reset");
    expect((await probe("POST", `/api/v1/admin/users/${self.id}/delete`, { ...body, version: self.version, confirm_email: self.email })) === 422, "own delete");
    expect((await probe("PATCH", `/api/v1/admin/users/${users["rahul@acme.test"].id}`, { version: users["rahul@acme.test"].version, role: "admin" })) === 403, "role escalation");
  });

  // ------------------------------------------------------------------ Admin deletes a user with work (batched hand-over)
  const priyaOld = await signIn(browser, "priya@acme.test");
  await step("Admin deletes Priya: hands her work to herself, types the email, sees the result", async () => {
    await openMenu(tara.page, "Priya Patel");
    await tara.page.getByRole("menuitem", { name: "Delete account" }).click();
    const dialog = tara.page.getByRole("alertdialog");
    await dialog.getByText("Checking what this account owns…").waitFor({ state: "detached", timeout: 20000 }).catch(() => {});
    const text = await dialog.innerText();
    expect(/Priya Patel/.test(text) && /priya@acme\.test/.test(text), "the dialog lacks name/email");
    expect(/1 lead/.test(text) && /1 open deal/.test(text), `the counts are missing: ${text}`);
    const submit = dialog.getByRole("button", { name: "Delete account" });
    expect(await submit.isDisabled(), "Delete is enabled before anything is chosen");
    await dialog.getByLabel("Hand their work to").selectOption({ label: "Tara Lead (tara@acme.test)" });
    expect(await submit.isDisabled(), "Delete is enabled before the email is typed");
    await dialog.getByLabel("Type their email to confirm").fill("priya@acme.test");
    await shot(tara.page, "03-delete-dialog");
    await submit.click();
    await tara.page.getByText("Priya Patel was deleted.").waitFor({ timeout: 60000 });
    await tara.page.getByText("Former user").first().waitFor();
  });

  await step("The deleted account shows as Deleted with no actions; its work is now Tara's; Priya's session ended", async () => {
    const row = tara.page.locator("tbody tr", { hasText: "priya@acme.test" });
    expect((await row.count()) === 0, "the deleted account still shows its email");
    const former = tara.page.locator("tbody tr", { hasText: "Former user" });
    expect((await former.first().innerText()).includes("Deleted"), "the row doesn't read Deleted");
    expect((await former.first().getByRole("button", { name: /^Actions for / }).count()) === 0, "a deleted account still has actions");
    const leads = await api(tara.page, "GET", "/api/v1/workspaces/me/leads?page_size=50");
    expect(leads.status === 200 && JSON.stringify(leads.json).includes("PriyaLead"), `Tara doesn't have Priya's lead: ${leads.status}`);
    await priyaOld.page.goto(`${BASE}/pipeline`);
    await priyaOld.page.waitForURL(/\/login/, { timeout: 30000 });
    const attempt = await openContext(browser, "priya-after-delete");
    await attempt.page.goto(`${BASE}/login`);
    await attempt.page.locator('input[name="email"]').fill("priya@acme.test");
    await attempt.page.locator('input[name="password"]').fill(PASSWORD);
    await attempt.page.getByRole("button", { name: "Sign in", exact: true }).click();
    await alertWithText(attempt.page).waitFor({ timeout: 20000 });
    expect(attempt.page.url().includes("/login"), "a deleted account signed in");
    await attempt.context.close();
  });

  await step("Admin deletes Mohan, who owns nothing: no hand-over asked for", async () => {
    await openMenu(tara.page, "Mohan Das");
    await tara.page.getByRole("menuitem", { name: "Delete account" }).click();
    const dialog = tara.page.getByRole("alertdialog");
    await dialog.getByText(/nothing needs handing over/).waitFor({ timeout: 20000 });
    expect((await dialog.getByLabel("Hand their work to").count()) === 0, "an owner was asked for");
    await dialog.getByLabel("Type their email to confirm").fill("mohan@acme.test");
    await dialog.getByRole("button", { name: "Delete account" }).click();
    await tara.page.getByText("Mohan Das was deleted.").waitFor({ timeout: 60000 });
  });

  // ------------------------------------------------------------------ Super admin deletes an Admin
  await step("Super admin deletes Tara (an Admin): her team is released, her work handed over", async () => {
    await anita.page.reload();
    await anita.page.getByRole("table").waitFor();
    await openMenu(anita.page, "Tara Lead");
    await anita.page.getByRole("menuitem", { name: "Delete account" }).click();
    const dialog = anita.page.getByRole("alertdialog");
    await dialog.getByText(/release their team/).waitFor({ timeout: 20000 });
    expect(/cancel the invitations they sent/.test(await dialog.innerText()), "the Admin consequences aren't listed");
    await dialog.getByLabel("Hand their work to").selectOption({ label: "Bina Boss (bina@acme.test)" });
    await dialog.getByLabel("Type their email to confirm").fill("tara@acme.test");
    await dialog.getByRole("button", { name: "Delete account" }).click();
    await anita.page.getByText("Tara Lead was deleted.").waitFor({ timeout: 60000 });
    const after = await directory(anita.page);
    const rahul = after["rahul@acme.test"];
    expect(rahul.manager === null, `Rahul still has an Admin: ${JSON.stringify(rahul.manager)}`);
    const sana = after["sana@acme.test"];
    expect(sana.manager === null, "the invited member kept the deleted Admin");
  });

  await step("Tara's session ended with her account", async () => {
    await tara.page.goto(`${BASE}/admin/users`);
    await tara.page.waitForURL(/\/login/, { timeout: 30000 });
  });

  // ------------------------------------------------------------------ An ordinary user
  const neha = await signIn(browser, "neha@acme.test");
  await step("An ordinary user has no Users page and the API refuses every new route", async () => {
    await neha.page.goto(`${BASE}/admin/users`);
    await neha.page.waitForLoadState("networkidle");
    expect(!(await neha.page.getByRole("table").count()), "an ordinary user sees the users table");
    const target = users["vikram@acme.test"];
    for (const [method, route, body] of [
      ["POST", "reset-password"],
      ["GET", "deletion-preview"],
      ["POST", "delete", { version: target.version, confirm_email: target.email }],
    ]) {
      const out = await api(neha.page, method, `/api/v1/admin/users/${target.id}/${route}`, body);
      expect(out.status === 403 && out.code === "permission_denied", `${route} answered ${out.status} ${out.code} to an ordinary user`);
    }
  });

  // ------------------------------------------------------------------ Phone-sized screen
  await step("The dialogs fit a 375 px phone with no horizontal scroll", async () => {
    const phone = await signIn(browser, "bina@acme.test", PASSWORD, { width: 375, height: 812 });
    await openUsers(phone.page);
    const overflow = () => phone.page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect((await overflow()) <= 0, `the page scrolls sideways by ${await overflow()}px`);
    await openMenu(phone.page, "Vikram Free");
    await phone.page.getByRole("menuitem", { name: "Reset password" }).click();
    const reset = phone.page.getByRole("alertdialog");
    await reset.waitFor();
    const box = await reset.boundingBox();
    expect(box.x >= 0 && box.x + box.width <= 375.5, `the reset dialog leaves the screen: ${JSON.stringify(box)}`);
    expect((await overflow()) <= 0, "the reset dialog makes the page scroll sideways");
    await shot(phone.page, "04-phone-reset");
    await reset.getByRole("button", { name: "Cancel" }).click();
    await openMenu(phone.page, "Neha Rao");
    await phone.page.getByRole("menuitem", { name: "Delete account" }).click();
    const del = phone.page.getByRole("alertdialog");
    await del.getByLabel("Type their email to confirm").waitFor({ timeout: 20000 });
    const delBox = await del.boundingBox();
    expect(delBox.x >= 0 && delBox.x + delBox.width <= 375.5, `the delete dialog leaves the screen: ${JSON.stringify(delBox)}`);
    expect((await overflow()) <= 0, "the delete dialog makes the page scroll sideways");
    await shot(phone.page, "05-phone-delete");
    await del.getByRole("button", { name: "Cancel" }).click();
    await phone.context.close();
  });

  await step("Security events list the reset and the deletions", async () => {
    await anita.page.goto(`${BASE}/admin`);
    await anita.page.getByText("sent a password reset link to").first().waitFor({ timeout: 30000 });
    await anita.page.getByText(/deleted the account of/).first().waitFor({ timeout: 30000 });
    await shot(anita.page, "06-security-activity");
  });

  await step("No page errors or unexpected console errors anywhere", async () => {
    expect(problems.length === 0, problems.slice(0, 6).join(" | "));
  });

  await browser.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} browser checks passed in ${Math.round((Date.now() - started) / 1000)}s`);
  fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify({ results, problems }, null, 2));
  process.exit(failed.length ? 1 : 0);
})().catch((error) => {
  console.error("harness failure:", error);
  process.exit(2);
});
