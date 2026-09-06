# Maids Evals

A shared workspace for running evaluation prompts over spreadsheet rows — usually support conversations — then reviewing the model’s JSON, taking notes, and tightening the prompt.

It is meant for people who already write or maintain eval prompts (quality, policy, identifier, tool-use). You upload a sheet, pick a prompt from the shared library, run OpenAI / LangCC / Gemini / Ollama across the rows, then walk the results one row at a time.

If you do not have an access token, you cannot open the app, list prompts, see history, or download files. There is no guest mode.

---

## What you can do

1. Upload a CSV or Excel (`.xlsx`) file of conversations or any other rows.
2. Load a shared prompt, write one, or **Build prompt** with a guided helper. Instructions and Input data are separate. Placeholders stay `{column}` (raw cell, no automatic label). `{row_json}` still works for older prompts.
3. Run the prompt across the sheet, or test a single random row first.
4. Filter, review, compute true/false stats, and download a flattened CSV. Model answers render as a collapsible JSON tree (with a Pretty/Raw toggle and copy button), not a wall of text.
5. Leave a note on each row (`ok` / `wrong` / `unclear`) and ask the fixer to improve the prompt — either from **all** of a run's notes in one batched pass, or from a single **Test** row + note.
6. Save the accepted update as a new shared version (`agent_eval.v2`). Your run history stays private.
7. Browse the shared **Prompts** fix history: every improvement run, who ran it, the failure patterns, the diff, and whether it was accepted or discarded.
8. View or edit shared **Company** notes so the prompt helper and fixer stay on maids.cc vocabulary. Volatile figures in that file are not treated as facts.

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

You must be logged in. Keys are per user. Ollama does not use a key.

**Settings** is the place to keep defaults: default provider and model, more than one named API key, which key is default, and which model each helper uses (New run, Build prompt, Fixer, Catalogue chat, Find a prompt). New run still lets you change the model or key for that batch.

On **Settings** or **New run**:

| Field label | Suggested value | Autocomplete |
|---|---|---|
| **Key name** | a label such as `Work LangCC` | `username` |
| **API key** | your provider secret | `current-password` / `new-password` |

Two ways to keep it:

1. **This account** — save the key. It is written under `data/users/<you>/secrets.json`, encrypted with `SECRET_FERNET` or a key derived from `SESSION_SECRET`. Nobody else can load it.
2. **Your password manager** — Apple Passwords, Google Password Manager, or Bitwarden. The fields stay password-manager friendly.

The key is never stored in `users.json`, shared prompts, run metadata, or `/session/save`. Unauthenticated `GET`/`POST` `/credentials` returns `401` and no key material.

When you open New run, it uses your Settings defaults so you do not re-enter a key every time.

---

## Walkthrough

### 1. Upload a spreadsheet

On **New run**, step 1, choose a `.csv` or Excel `.xlsx` and click **Upload**. Empty rows are dropped, blank cells become empty text, and column names stay in order. The step stays put and shows the row count.

If the Excel file has more than one sheet, the first sheet is used and a short note tells you so. Older `.xls` files are accepted when possible; if one fails, save it as `.xlsx` and try again. Downloads and run archives stay CSV.

Optional, under **Advanced**: keep a random subset for a cheap first pass.

### 2. Pick or write a prompt

The prompt library is **shared**. Anyone logged in can load and save.

- Choose a saved prompt and click **Load**. The shared library stores **instructions only**.
- Or type a name and **Save to library**.
- **Instructions** are the task and the JSON you want back.
- **Input data** is the conversation or other cells. It is appended at the end, after a line `===== INPUT =====`, when the model runs. Leave it blank to keep the old single-box behavior.
- Click a column chip to insert `{ThatColumn}` into the box you last clicked (chips start in Input data). Write any label yourself — substitution is the raw cell, with no `Column:` prefix.
- `{row_json}` still works for older prompts. Prefer named columns. Do not put the same column in both boxes, and do not put a long conversation in the middle of the instructions.

The prompt needs row data: a `{column}` that matches the sheet, `{row_json}`, or a mapped prompt field (the name in the prompt does not have to match the column name).

### 2b. Build an analysis prompt (guided)

If you do not already have a prompt, open **Build prompt** in the nav (or the link on New run).

1. Upload a CSV/Excel file, or keep the one already loaded.
2. Write what you want to find out about each row, in everyday language.
3. Click the column that holds the conversation or main text.
4. Optionally note what the other columns mean.
5. Pick provider and model (LangCC by default; same saved-key card as New run) and click **Study a few rows**. The helper sees 3–8 short sample rows only — not the whole sheet — plus the shared company notes so it uses maids.cc names (Enchanters, CC, MV, PTC). It will not put prices or headcounts from those notes into the prompt. If a short column is already clear (for example nationality values in the sample), it will not ask about it.
6. Answer up to three follow-up questions if they appear. Or click **Deep analysis** to keep answering rounds of questions until the helper has about 80–90% of what it needs for a detailed, multi-condition prompt.
7. **Write the prompt**. You get two parts: **instructions** (task, IF/THEN rules using short columns like `{nationality}`, and an explicit flat JSON object — saved to the shared library) and **input data** (the conversation column only, kept on this session/run). The helper will not use `{row_json}`, will not echo input ids, and will not put the conversation mid-instructions. Company notes stay with the helper; they are not pasted into the prompt unless you copy them in.
8. **Test a random row** (or pick a row / test the same row again). The answer uses the same JSON/text viewer as Review.
9. Tweak the description and rewrite if needed. When it looks right, **Use … on a full run** takes you back to New run with that prompt already selected.

### 2c. Catalogue

**Catalogue** lists prompts you own, prompts shared with you, and everyone-visible prompts. Search by name or username, page through results, or ask the helper to find one — it only sees prompts you can use.

Open a prompt to read it, edit it, clone it, or talk to the helper. The helper keeps that conversation. A clone **copies** the chat and then runs on its own — editing a copy never touches the original or anyone else's history.

If you can edit, the Instructions and Input data boxes are live: change them and **Save changes** appears. Every save keeps the previous text under **Earlier versions**, where you can preview or restore it. When the helper proposes an edit you see a diff of exactly which lines move before you **Apply** or **Reject** it — a shared prompt never changes on trust alone.

If you cannot edit, the helper says so: clone the prompt or request edit access from the owner. Only the creator can delete a prompt; deleting also removes its conversation.

Each accepted fix stores a short summary of what changed and what the prompt is for, so the next incremental fix does not undo an earlier one.

On **New run**, search the same list, or click **Last used**.

### 3. Choose a provider and run

Steps 3 and 4: pick a provider and model, then **Run**. Fill or **Save key** if the provider needs one. Workers and model parameters stay under **Advanced**, below the Run button.

| Provider | Key? | Model field |
|---|---|---|
| OpenAI | Yes | Type a model id, e.g. `gpt-5-mini` |
| LangCC | Yes | List is fetched with your key |
| Gemini | Yes | Known Gemini ids |
| Ollama | No | List from the configured Ollama host |

A run stops if a `{field}` in the prompt is not a column in your sheet and is not mapped — the message names the fields. Nothing is substituted blank silently. **Test one row** is the cheap check. On the test result you can also drop a note + verdict and click **Improve prompt from this test** to run the same Analyst → Editor → Critic pass on that single example, then save the result as a new shared version. **Run** fans the prompt out across the sheet (concurrency is capped by the server). **Stop the run** halts dispatch — rows already sent finish, the finished rows stay on the session, and nothing is archived. When a run finishes you land on **Results**.

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

1. Nothing to do if you already saved a key in Settings — the fixer picks it up. Paste one in the Review key card only for a different key.
2. Click **Improve prompt from all N notes**. This is a single batched pass: every reviewed row's note + verdict on the run is grouped into one Analyst → Editor → Critic run — not one row at a time.
3. Three agents run on your key: Analyst (clusters failures) → Editor (surgical edits only) → Critic (rejects overfitting and contradictions). A stage that answers in prose instead of JSON is asked once more; if the Critic still fails, the Editor's version is offered with a warning rather than the whole pass being thrown away. Very large batches send the most informative notes (wrong and unclear first) and tell you how many were used.
4. A diff appears. **Save proposed as shared version**, or **Discard** (which is recorded in the fix history).

That writes a new file into the version library, e.g. `agent_eval.v2`, **and** publishes the text into the catalogue so other people can actually load it:

- If you can edit the prompt the run used, its catalogue entry is updated in place and the old text is kept under Earlier versions.
- If you only have view access, you get your own catalogue copy carrying the same reach as the prompt it came from.

If the pass ends with the prompt unchanged, saving is refused rather than creating an identical `.v2`. Your notes stay on your run.

You can run the same improvement from a single **Test** row + note when you do not have a full reviewed batch (see step 3).

### 7. Prompt fix history

Every improvement run — from Review or from a Test row — is recorded in a shared, team-visible log under `prompts/history/<family>.jsonl`. The **Prompts** page lists each prompt family; open one to see, per fix:

- Who ran it and when, and whether it came from a review batch or a test row.
- The failure patterns the Analyst clustered, the editor's change summary, and the critic's notes.
- The full diff (old → new) and the complete proposed text — retrievable even if the fix was discarded.
- Status: `accepted` (with the resulting version name), `discarded`, `superseded`, or `proposed`.
- Every saved version in the family, with a **View text** button. Old versions are never deleted.

History is shared like the prompts themselves, but each record keeps the username of whoever ran the fix. It is still gated behind login.

### 8. Company info

**Company** (`/context`) is a shared page — like the prompt library — for short notes about maids.cc. The prompt builder (study rows, questions, write prompt) and the prompt fixer (Analyst, Editor, Critic) read this on every helper call so they stay on the internal vocabulary and service lines.

Anyone logged in can view and edit it. The file lives at `context/maids_cc.md`. The **Volatile facts** section is intentionally not authoritative: helpers are told never to quote or invent prices, salaries, benefit amounts, employee/client counts, ratings, branch counts, or nationality availability from it. If a figure is needed, the generated prompt should say it must be pulled from the owning source.

You can copy the notes into a prompt yourself. They are not forced into every prompt’s text.

### 9. History

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
| Company notes in `context/maids_cc.md` | Every logged-in user |
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
prompt_builder.py      Guided analysis-prompt helper (sample → plan → prompt)
company_context.py     Load/save shared maids.cc notes; prepended to helper/fixer calls
manage_users.py        add / list / revoke / reset-token (shares auth.py with the Admin UI)
templates/             Pages (incl. prompt_history.html, _components.html macros)
static/                CSS and JS (incl. ai-output.js viewer, test.js)
prompts/               Shared prompt library (*.txt)
prompts/history/       Shared prompt-fix history (<family>.jsonl)
context/maids_cc.md    Shared company notes (editable at /context)
data/users/<name>/     Private runs, usage.jsonl, encrypted secrets.json
users.json             Hashed tokens — do not commit
.env                   SESSION_SECRET, ADMIN_USERNAME, ADMIN_TOKEN, optional SECRET_FERNET — do not commit
```

A run folder contains `input.csv`, `output.csv`, `results.json`, `metadata.json`, `prompt.txt` (instructions), `input_template.txt` (optional input-data section), and `review.json`. `metadata.json` also stores `input_template` so reopening a run restores both boxes.

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
- Saved LLM keys are never rendered into a page or returned to the browser. `GET /credentials` reports only whether a key exists; every route that needs the value reads it server-side.
- This is an internal tool. Put it behind HTTPS and a trusted network if more than one person can reach the host.

---

## Typical first hour

1. `pip install -r requirements.txt` and set `SESSION_SECRET`, `ADMIN_USERNAME`, and `ADMIN_TOKEN`.
2. Log in as the env admin (or `python3 manage_users.py add ali` for an extra account) and store the token in your password manager as the site login.
3. Run the app, log in, paste your LangCC or OpenAI key on New run, and click **Save key** (or leave it to your password manager).
4. Upload a small CSV or Excel file, load `agent_eval` (or use **Build prompt**), test one row, then run.
5. Review five rows, leave notes, run **Improve prompt from notes**, and save `agent_eval.v2` if the diff is honest.
