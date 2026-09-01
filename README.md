# Maids Evals

A shared workspace for running evaluation prompts over CSV rows — usually support conversations — then reviewing the model’s JSON, taking notes, and tightening the prompt.

It is meant for people who already write or maintain eval prompts (quality, policy, identifier, tool-use). You upload a sheet, pick a prompt from the shared library, run OpenAI / LangCC / Gemini / Ollama across the rows, then walk the results one row at a time.

If you do not have an access token, you cannot open the app, list prompts, see history, or download files. There is no guest mode.

---

## What you can do

1. Upload a CSV of conversations or any other rows.
2. Load a shared prompt (or write a new one) with `{column}` and `{row_json}` placeholders.
3. Run the prompt across the sheet, or test a single random row first.
4. Filter, review, compute true/false stats, and download a flattened CSV. Model answers render as a collapsible JSON tree (with a Pretty/Raw toggle and copy button), not a wall of text.
5. Leave a note on each row (`ok` / `wrong` / `unclear`) and ask the fixer to improve the prompt — either from **all** of a run's notes in one batched pass, or from a single **Test** row + note.
6. Save the accepted update as a new shared version (`agent_eval.v2`). Your run history stays private.
7. Browse the shared **Prompts** fix history: every improvement run, who ran it, the failure patterns, the diff, and whether it was accepted or discarded.

---

## Install and run

You need Python 3.10+.

```bash
cd maids-evals
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set a stable session secret and admin token in `.env` so logins survive a restart:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste those values into `.env`:

```
SESSION_SECRET=the-hex-you-just-generated
ADMIN_USERNAME=admin
ADMIN_TOKEN=the-urlsafe-token-you-just-generated
```

The env admin is accepted on every boot. You do not need `manage_users.py` for that account. Add extra people with:

```bash
python3 manage_users.py add yourname
```

Start the app:

```bash
python3 app.py
```

Or:

```bash
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). You will be sent to `/login`.

---

## Users and login

The site admin comes from `.env` (`ADMIN_USERNAME` / `ADMIN_TOKEN`). That pair is hashed and compared on every request; it is not written to `users.json` and it does not change across restarts. **The env admin is the admin**: it is the only account that can reach the Admin pages. (Extra `users.json` users may be promoted later with an `"is_admin": true` flag in the file; it defaults to false.)

Extra users still live in `users.json`. Only hashed tokens are stored there. The raw token is shown once when a user is created.

### Creating users

There are two ways to create users, both admin-controlled, and both use the same server-side code path (`auth.py`): a strong token is generated, hashed with SHA-256, and stored in `users.json`. The plaintext token is shown **once**.

**1. Admin UI (in the app).** Log in as the env admin and open **Admin** in the top nav (visible only to the admin). From `/admin/users` you can:

- **Create user** — type a username; the app generates the token and shows it once with a copy button.
- **Reset token** — regenerate a user's token; the old one stops working immediately.
- **Remove** — delete a `users.json` user (with a confirm). The env admin cannot be removed or reset here — it lives in `.env`.

Non-admin accounts cannot see the Admin nav and get `403` if they hit `/admin/*` directly. Unauthenticated requests are redirected to login (or get `401` for JSON).

**2. CLI (`manage_users.py`).** Same functions as the UI:

```bash
python3 manage_users.py add sara
python3 manage_users.py list
python3 manage_users.py reset-token sara
python3 manage_users.py revoke sara
```

`users.json` is created next to `app.py`. Do not commit it. `users.example.json` is only a shape reference for extra users.

On the login page:

| Field | What to type | Browser autocomplete |
|---|---|---|
| **Username** | `ADMIN_USERNAME` from `.env`, or a name from `manage_users.py add` | `username` |
| **Access token** | `ADMIN_TOKEN` from `.env`, or the extra-user token printed once | `current-password` |

This is the **site login**. Save it in Apple Passwords, Google Password Manager, or Bitwarden as “Maids Evals”. It is not an OpenAI/Gemini key.

After login you get an HttpOnly cookie. Logging out clears it. Revoking a user stops that token on the next request.

Without a valid session:

- Pages such as `/`, `/export`, `/review`, `/runs` redirect to login.
- JSON, progress, prompts, and file downloads return `401`.
- Run folders are only readable when `metadata.owner` is you.
- `/admin/*` requires login **and** admin: unauthenticated → login/`401`; logged-in non-admin → `403`.

---

## Save your LLM API key

You must be logged in. Keys are per user and per provider (`openai`, `langcc`, `gemini`). Ollama does not use a key.

On **New run** (and on **Review** when you improve a prompt):

| Field label | Suggested value | Autocomplete |
|---|---|---|
| **Key name** | `openai`, `langcc`, or `gemini` | `username` |
| **API key** | your provider secret | `current-password` |

Two ways to keep it:

1. **This account** — click **Save key**. It is written under `data/users/<you>/secrets.json`, encrypted with `SECRET_FERNET` or a key derived from `SESSION_SECRET`. Nobody else can load it. **Forget saved key** deletes only your copy for that provider.
2. **Your password manager** — Apple Passwords, Google Password Manager, or Bitwarden. The fields stay password-manager friendly. Saving to the account is extra, not a replacement.

The key is never stored in `users.json`, shared prompts, run metadata, or `/session/save`. Unauthenticated `GET`/`POST` `/credentials` returns `401` and no key material.

When you open New run, the password field is filled from *your* saved key for the selected provider. Switching provider loads that provider’s saved key.

---

## Walkthrough

### 1. Upload a CSV

On **New run**, step 1, choose a `.csv` and click **Upload**. Empty rows are dropped. The step stays put and shows the row count.

Optional, under **Advanced**: keep a random subset for a cheap first pass.

### 2. Pick or write a prompt

The prompt library is **shared**. Anyone logged in can load and save.

- Choose a saved prompt and click **Load**.
- Or type a name and **Save to library**.
- Click a column chip to insert `{ThatColumn}`. `{row_json}` inserts the whole row.

The prompt must contain at least one placeholder.

### 3. Choose a provider and run

Steps 3 and 4: pick a provider and model, then **Run**. Fill or **Save key** if the provider needs one. Workers and model parameters stay under **Advanced**, below the Run button.

| Provider | Key? | Model field |
|---|---|---|
| OpenAI | Yes | Type a model id, e.g. `gpt-5-mini` |
| LangCC | Yes | List is fetched with your key |
| Gemini | Yes | Known Gemini ids |
| Ollama | No | List from the configured Ollama host |

**Test one row** is the cheap check. On the test result you can also drop a note + verdict and click **Improve prompt from this test** to run the same Analyst → Editor → Critic pass on that single example, then save the result as a new shared version. **Run** fans the prompt out across the sheet (concurrency is capped by the server). When it finishes you land on **Results**.

Each finished batch is saved only under your account: input CSV, output CSV, results JSON, the prompt used, and an empty review file.

### 4. Results

Primary actions:

- **Review rows** — walk the batch.
- **Stats** — true/false rates on JSON keys, optional date window.
- **Saved CSV** — the archive written when the run finished.

The **Answers** section lists the first 200 matching rows. Each row is a disclosure that renders the model answer on demand: valid JSON becomes a collapsible key/value tree with a **Pretty / Raw** toggle, a valid/invalid badge, and a copy button; plain text is clamped with a **Show full output** control. The same viewer is used on Review and Test so answers look identical everywhere, and trees are only built when a row is opened (fast for many rows).

Under the fold:

- Filter on JSON true/false keys (AND).
- Build a new working set from selected rows.
- Download a custom CSV (picked input columns + flattened JSON keys).

### 5. Review with notes

Side by side: input columns and the model JSON.

On each row:

1. Mark **Ok**, **Wrong**, or **Unclear**.
2. Write what the prompt should have done.
3. Notes save onto that run (`review.json`). They are not shared.

The prompt used for the run is editable in the same page.

### 6. Improve the prompt

When you have a few notes:

1. Put your LLM key in the Review key card (same fields as New run).
2. Click **Improve prompt from all N notes**. This is a single batched pass: every reviewed row's note + verdict on the run is grouped into one Analyst → Editor → Critic run — not one row at a time.
3. Three agents run on your key: Analyst (clusters failures) → Editor (surgical edits only) → Critic (rejects overfitting and contradictions).
4. A diff appears. **Save proposed as shared version**, or **Discard** (which is recorded in the fix history).

That writes a new file into the shared library, e.g. `agent_eval.v2`. Other people can load it. Your notes stay on your run.

You can run the same improvement from a single **Test** row + note when you do not have a full reviewed batch (see step 3).

### 7. Prompt fix history

Every improvement run — from Review or from a Test row — is recorded in a shared, team-visible log under `prompts/history/<family>.jsonl`. The **Prompts** page lists each prompt family; open one to see, per fix:

- Who ran it and when, and whether it came from a review batch or a test row.
- The failure patterns the Analyst clustered, the editor's change summary, and the critic's notes.
- The full diff (old → new) and the complete proposed text — retrievable even if the fix was discarded.
- Status: `accepted` (with the resulting version name), `discarded`, `superseded`, or `proposed`.
- Every saved version in the family, with a **View text** button. Old versions are never deleted.

History is shared like the prompts themselves, but each record keeps the username of whoever ran the fix. It is still gated behind login.

### 8. History

**History** lists only your runs. Search by name, prompt, provider, or date. **Open** loads that batch back into Results and Review.

---

## Keyboard shortcuts (Review)

These fire when you are not typing in a text box.

| Key | Action |
|---|---|
| `→` or `J` | Next row |
| `←` or `K` | Previous row |
| `1` | Verdict: ok |
| `2` | Verdict: wrong |
| `3` | Verdict: unclear |

---

## Shared prompts vs private history

| Thing | Who sees it |
|---|---|
| Prompts in `prompts/` | Every logged-in user |
| New prompt versions from the fixer | Every logged-in user |
| Prompt fix history in `prompts/history/` | Every logged-in user (records who ran each fix) |
| Your runs, notes, usage | Only you |
| Other people’s runs | Nobody else, including you |

Old global `runs/` folders from the single-user tool are not listed. New work lives under `data/users/<you>/runs/`.

---

## File layout

```
app.py                 FastAPI routes
engine.py              Provider calls, JSON flatten, row processing
auth.py                Token hash + login cookie
storage.py             Per-user runs, usage, shared prompts, prompt-fix history
prompt_fixer.py        Analyst / editor / critic
manage_users.py        add / list / revoke / reset-token (shares auth.py with the Admin UI)
templates/             Pages (incl. prompt_history.html, _components.html macros)
static/                CSS and JS (incl. ai-output.js viewer, test.js)
prompts/               Shared prompt library (*.txt)
prompts/history/       Shared prompt-fix history (<family>.jsonl)
data/users/<name>/     Private runs, usage.jsonl, encrypted secrets.json
users.json             Hashed tokens — do not commit
.env                   SESSION_SECRET, ADMIN_USERNAME, ADMIN_TOKEN, optional SECRET_FERNET — do not commit
```

A run folder contains `input.csv`, `output.csv`, `results.json`, `metadata.json`, `prompt.txt`, and `review.json`.

Each `prompts/history/<family>.jsonl` record holds: `fix_id`, timestamp, `user`, `source_name` + resulting `new_version`, `flow` (`review` or `test`), `run_id`, the notes it was based on, the Analyst `patterns`, the editor `change_summary`, the `diff`, the full `old_prompt`/`new_prompt`, and `status` (`proposed` / `accepted` / `discarded` / `superseded`).

---

## Environment

Copy `.env.example` to `.env`.

| Variable | Required | Meaning |
|---|---|---|
| `SESSION_SECRET` | Yes, in practice | Signs login cookies. Without it, the process makes a one-off secret and warns you. Logins die on restart. Also used to derive the Fernet key for saved LLM API keys if `SECRET_FERNET` is unset. |
| `SECRET_FERNET` | No | Optional Fernet key for encrypting per-user LLM API keys at rest. If unset, a key is derived from `SESSION_SECRET`. |
| `ADMIN_USERNAME` | Yes, in practice | Stable admin login name. Default suggestion: `admin`. |
| `ADMIN_TOKEN` | Yes, in practice | Stable admin access token. Compared on every boot; not stored in `users.json`. |
| `MAX_REQUEST_WORKERS_CAP` | No | Max parallel provider calls. Default `64`. |
| `OLLAMA_API_BASE` | No | Default `https://ai.aliawdeh.com/api` |
| `LANGCC_API_BASE` | No | Default `https://langcc.maidstech.ai/v1` |

---

## Security notes

- Do not commit `users.json`, `.env`, or anything under `data/`.
- Saved LLM keys live only under `data/users/<you>/secrets.json`, encrypted. Do not commit `data/` or paste keys into Slack. Changing `SESSION_SECRET` / `SECRET_FERNET` makes previously saved keys unreadable.
- Extra-user tokens are stored as `sha256:...`. There is no way to recover a lost extra-user token — add a new user or replace the hash by creating the user again after a revoke. The env admin token is always the value in `.env`.
- Downloads and prompt APIs are session-gated. Direct URLs without a cookie return `401`.
- This is an internal tool. Put it behind HTTPS and a trusted network if more than one person can reach the host.

---

## Typical first hour

1. `pip install -r requirements.txt` and set `SESSION_SECRET`, `ADMIN_USERNAME`, and `ADMIN_TOKEN`.
2. Log in as the env admin (or `python3 manage_users.py add ali` for an extra account) and store the token in your password manager as the site login.
3. Run the app, log in, paste your LangCC or OpenAI key on New run, and click **Save key** (or leave it to your password manager).
4. Upload a small CSV, load `agent_eval` or another shared prompt, test one row, then run.
5. Review five rows, leave notes, run **Improve prompt from notes**, and save `agent_eval.v2` if the diff is honest.
