# einkgen iPhone shortcuts

Drive einkgen from your phone with one Siri tap. Two paths — pick whichever
matches your deploy:

| Path | Best when | Effort | Trip ends at |
| --- | --- | --- | --- |
| **A. Email** | Inbound email is set up ([QUICKSTART §3.10](../QUICKSTART.md#310-optional-email-submission-channel)) | 2 actions, ~2 min | Mail's `Send` action |
| **B. HTTP** | No email setup; admin password works | 4–8 actions, ~5 min | Admin API queue endpoint |

Both end the same way:

> *"Hey Siri, einkgen."* → dictate a prompt → tap once → the next time the
> Inkplate wakes (default: 1 hour, or press the **WAKE** button on the
> device), it shows the new image.

### Skipping the buffer with a NOW trigger

Since 0.6.1.0, both paths support a "render this now" override that
bypasses the ~10-deep pre-rendered buffer (otherwise an email or HTTP
submission waits behind everything else cron has queued up):

- **Path A (email):** prefix the subject with `NOW `, `NOW:`, or
  `[NOW]` (case-insensitive). The trigger word is stripped from the
  prompt before generation.
- **Path B (HTTP):** add `"at": "now"` to the JSON body of the
  `/admin/queue/prompt` request.

Without the trigger, submissions land at the bottom of the queue and
render on the next cron tick after the buffer drains — usually fine for
"set it and forget it" but slow if you wanted to see it on the panel
right away.

These instructions describe the **iOS 17+ Shortcuts app**. Action names
shift slightly between iOS versions; if the exact label is gone, the
capability still exists — search the action picker for the keyword in
**bold**.

---

## Path A — Email shortcut (recommended)

A two-action shortcut: dictate a prompt, send it as email. The inbound
Lambda parses the subject/body, enqueues a `kind=prompt` queue item, and
emails you back a confirmation with the queue id.

### Prerequisites

1. **Inbound email is deployed.** See
   [QUICKSTART §3.10](../QUICKSTART.md#310-optional-email-submission-channel)
   — without this, mail bounces or vanishes.
2. **Your phone's sending address is on the allowlist.** Note which
   address your iPhone Mail app sends *from* (iCloud, Gmail, work) — the
   allowlist matches that exact address, case-insensitively:
   ```sh
   EINKGEN_BUCKET=einkgen-dev AWS_PROFILE=einkgen \
     einkgen allowlist add you@icloud.com
   ```
   You can confirm with `einkgen allowlist ls`.

### Build the shortcut

In the **Shortcuts** app:

1. Tap **+** to create a new shortcut. Tap the title bar → rename it to
   **einkgen**. (That's also the phrase Siri will listen for.)
2. **Action 1 — Dictate Text.**
   - Tap **Add Action** → search **dictate** → choose **Dictate Text**.
   - Tap the chevron to expand it. Set **Stop Listening** to **On Tap**
     so you can take pauses while composing.
3. **Action 2 — Send Email.**
   - **Add Action** → search **mail** → choose **Send Email**.
   - **To:** any local-part `@<your-inbound-domain>` works (the receipt
     rule catches every address). For example: `prompt@einkgen.link`.
   - **From:** the address you added to the allowlist.
   - **Subject:** leave blank, or pin a fixed prefix like `einkgen`. To
     always skip the buffer, pin the subject to `NOW` — the trigger
     gets stripped before the prompt reaches gpt-image-2 (see
     "Skipping the buffer" above). Build a second shortcut named
     **einkgen now** with the same actions but a `NOW` subject if you
     want one-tap urgent submissions while keeping the default path
     for batch submissions.
   - **Body:** tap the **Email** field → select **Dictated Text** from
     the variable picker.
   - Expand the action and switch **Show When Run** → **off**, so Siri
     sends silently without popping the compose sheet.
4. Tap **Done**.

### Trigger it

- *"Hey Siri, einkgen."*
- Speak the prompt. Tap when finished.
- Within a few seconds you get a confirmation email back with the queue
  id (assuming SES is out of sandbox — see
  [QUICKSTART §3.11.2 step 2](../QUICKSTART.md#3112-deploy-ses-inbound-with-cdk)).
- The generator Lambda picks up the queue item within ~30 s.
- The Inkplate redraws on its next poll, or immediately if you press the
  **WAKE** button on the back.

### Variant: photo + restyle prompt

Build a second shortcut named **einkgen photo**:

1. **Select Photo** → choose **From:** Photos.
2. **Dictate Text** → same settings as above.
3. **Send Email** → set the **Attachments** field to the selected photo,
   the **Email** body to the dictated text, recipient and from same as
   Path A. The inbound Lambda treats this as `kind=image` with the prompt
   as a restyle hint (subject + body concatenation rules in
   [ARCHITECTURE §3](../ARCHITECTURE.md)).

---

## Path B — HTTP shortcut

Calls the admin API directly. The shortcut performs two HTTP requests in
sequence: `POST /admin/login` (with the password) to obtain a session
cookie, then `POST /admin/queue/prompt` (with the cookie + the prompt).

The admin password lives inside the shortcut on your device. That's the
same trust model as keeping it in your password manager — treat the
shortcut like any other place that holds the password.

### Prerequisites

- The CloudFront domain for your deploy (e.g. `einkgen.link` or
  `dxxxxxxxxx.cloudfront.net`).
- Your admin password (set in
  [QUICKSTART §3.5](../QUICKSTART.md#35-populate-the-secrets)).

### Sanity-check the endpoints

Before building the shortcut, confirm both calls work from a terminal.
This isolates "did I configure the shortcut wrong" from "is the deploy
broken":

```sh
DOMAIN=einkgen.link
PASSWORD='paste-it-here'

# 1. Login — captures the Set-Cookie into cookies.txt.
curl -sS -c cookies.txt -X POST "https://${DOMAIN}/admin/login" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"password": sys.argv[1]}))' "$PASSWORD")" \
  -o /dev/null -w 'login: %{http_code}\n'
# Expect: login: 204

# 2. Submit a prompt with the cookie.
curl -sS -b cookies.txt -X POST "https://${DOMAIN}/admin/queue/prompt" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "watercolor of a mountain at dawn"}'
# Expect: {"id":"20...","kind":"prompt"}

rm cookies.txt  # don't leave the cookie lying around
```

If both succeed, the shortcut will too. If `login` returns `503
not_configured`, the admin password secret still holds the placeholder —
re-run QUICKSTART §3.5.

### Build the shortcut

In the **Shortcuts** app:

1. **+** → rename to **einkgen**.
2. **Text** action — content: your CloudFront domain (e.g.
   `einkgen.link`). Tap the variable chip at the bottom → rename to
   **Domain**.
3. **Text** action — content: your admin password. Rename the variable
   to **Password**. (This text lives in the shortcut on your phone; it
   never leaves your iCloud-encrypted shortcut storage.)
4. **Dictionary** action — build a one-key dictionary with key
   `password` and value the **Password** variable. Rename the output to
   **LoginBody**.
5. **Get Contents of URL** action:
   - URL: `https://` followed by the **Domain** variable, then
     `/admin/login`.
   - Tap **Show More**:
     - **Method:** POST
     - **Request Body:** JSON → set to the **LoginBody** variable
     - **Headers:** leave the default — `Content-Type: application/json`
       is set automatically when Request Body is JSON.
6. **Get Dictionary Value** action — get the value for key `Set-Cookie`
   from the **Headers** of the previous response. (Some iOS versions
   expose the response headers as a separate output on the
   **Get Contents of URL** action; others need a dedicated
   **Get Headers of URL** action after step 5. Either path produces the
   same dictionary.) Rename the output to **SetCookieRaw**.
7. **Match Text** action — pattern `einkgen_admin=[^;]+` against
   **SetCookieRaw**. (The `Set-Cookie` header includes flags like
   `; Path=/admin; HttpOnly; Secure; SameSite=Lax` which the next request
   shouldn't echo back. The match isolates just the `name=value`
   pair.) Rename the matches output → take the first match → name it
   **Cookie**.
8. **Dictate Text** action — **Stop Listening: On Tap**.
9. **Dictionary** action — key `prompt`, value the **Dictated Text**
   variable. (Add a second key `at` with value `now` if you want this
   shortcut to always bypass the buffer — see "Skipping the buffer"
   above. Without the `at` key, items land at the bottom of the queue
   and render on the next cron tick after the buffer drains.) Rename
   the output to **PromptBody**.
10. **Get Contents of URL** action:
    - URL: `https://<Domain>/admin/queue/prompt`
    - **Method:** POST
    - **Headers:** add one — `Cookie` → the **Cookie** variable from
      step 7.
    - **Request Body:** JSON → **PromptBody**.
11. **Show Result** action — content: the response from step 10. Useful
    for seeing the queue id (or the error, if something went wrong).

Tap **Done**.

> **Shortcut shape note.** Some iOS releases (17.4+) auto-share cookies
> between successive **Get Contents of URL** actions targeting the same
> host within a single shortcut run. If that turns out to be true on your
> device, steps 6 and 7 are dead weight and step 10 won't need the
> `Cookie` header. The version above is the "manual, always works"
> shape — leave it in place, no harm done.

### Trigger it

- *"Hey Siri, einkgen."*
- Speak the prompt → tap when done.
- The Show Result sheet displays `{"id":"...","kind":"prompt"}` on
  success. The generator picks it up within ~30 s.

### Rotating the password

If you rotate `einkgen/admin_password` in Secrets Manager (see
[CLAUDE.md "Rotate the admin password"](../CLAUDE.md)), update the
**Password** action in the shortcut. Unlike the SPA — which holds a
90-day signed cookie that survives rotations until it expires — the
shortcut logs in fresh on every run, so the new password takes effect on
the next invocation.

---

## Sharing the shortcut

Long-press the shortcut tile → **Share** → **iCloud Link** lets you send
the shortcut to anyone. The dictated-prompt actions are portable, but
the **Domain** and **Password** text fields go along too — so only share
the email-path shortcut, not the HTTP-path one (which embeds a secret).

---

## Troubleshooting

- **"This shortcut requires permission to send email."** First run only
  — tap **Always Allow**.
- **Email shortcut: no confirmation reply arrives.** Either SES is still
  in sandbox (QUICKSTART §3.11.2 step 2 — inbound enqueue still works,
  the reply is just suppressed) or the sender address isn't on the
  allowlist (which we never reveal in a reply — check the **Queue** tab
  on the SPA, or tail the inbound Lambda log).
- **HTTP shortcut returns 401.** Either the password is wrong (step 3)
  or the cookie didn't make it from step 7 to step 10. Run the curl
  check above to isolate. The `Set-Cookie` key is case-sensitive in
  iOS Shortcuts' Get Dictionary Value.
- **HTTP shortcut returns 503 `not_configured`.** The admin password
  secret is still `REPLACE_ME_POST_DEPLOY`. Re-run QUICKSTART §3.5 and
  retry.
- **Both paths: queue item lands but the Inkplate doesn't update.** The
  generator may still be running (check the **Queue** tab for the item;
  it disappears once published, then appears in **History**). The
  device only polls on its sleep cadence — press the WAKE button to
  force an immediate refresh.

See also [ARCHITECTURE.md](../ARCHITECTURE.md) for the full data flow.
