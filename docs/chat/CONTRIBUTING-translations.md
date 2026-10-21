# Contributing Translations (i18n / l10n)

Thanks for helping translate **Touka**! The UI is fully internationalized with
[react-i18next](https://react.i18next.com/). This guide explains how to add or
improve a language — **no coding required** for translators.

## How it works

- All UI strings live in **JSON catalogs**, one folder per language:
  `ToukaChat/src/locales/<lng>/translation.json`.
- **`zh-TW` (Traditional Chinese) is the source language** — it is always
  complete and is what every other language is checked against.
- **`en` (English) is the reference language** — it must be 100% complete and is
  what most community translators translate _from_.
- The language registry is **`ToukaChat/src/locales/index.ts`**. Adding a
  language = drop a folder + add one line here.
- A CI guard (**Guard #33**, `npm run guard:i18n`) keeps every language in sync
  with the source and blocks bad PRs (see below).
- Missing keys in a community language are **fine** — they automatically fall
  back to English (then Chinese). Translate incrementally.

## Option A — Translate via the web (recommended, no Git)

We use a Translation Management System (TMS) so you can translate in your browser
and it opens a PR automatically. Ask a maintainer for the project link, then:

1. Pick your language (or request a new one).
2. Translate the strings in the web editor. Untranslated strings show the English
   reference.
3. Submit — the TMS commits to a branch and opens a Pull Request for review.

## Option B — Translate via a Pull Request (manual)

### Add a new language

1. Copy the English folder to your language code (BCP-47, e.g. `ja`, `de`,
   `fr`, `ko`, `es`):
   ```
   cp -r ToukaChat/src/locales/en ToukaChat/src/locales/ja
   ```
2. Register it in **`ToukaChat/src/locales/index.ts`** — add two lines:
   ```ts
   import ja from "./ja/translation.json";   // 1) import
   // ...
   export const LOCALES = {
     "zh-TW": zhTW,
     en,
     ja,                                      // 2) register
   };
   ```
3. Translate the **values** (never the keys) in `ja/translation.json`.
4. Run the guard (see below) and open a PR.

### Improve an existing language

Edit the values in `ToukaChat/src/locales/<lng>/translation.json` and open a PR.

## Translation rules (important)

Only translate the **value** (right side). Keep the **key** (left side) untouched.

1. **Keep placeholders exactly.** `{{count}}`, `{{model}}`, `{{name}}` etc. must
   appear verbatim in your translation — do not translate the text inside `{{ }}`.
   ```json
   "toast_backup_success": "Backed up {{count}} conversations"
   →  (ja) "{{count}} 件の会話をバックアップしました"
   ```
2. **Keep markup tags exactly.** Some strings contain tags like `<1>…</1>`,
   `<v>…</v>`, `<code>…</code>`, `<strong>…</strong>` (these wrap styled or
   clickable parts). Keep the same tags, same numbers, same positions — only
   translate the text between them.
3. **Plurals (English-style languages).** For counted nouns, provide both forms
   using the `_one` / `_other` suffix keys (the source `{{count}}` key already
   exists; add the variants for your language). i18next picks the right one.
   ```json
   "round_count_one": "{{count}} round",
   "round_count_other": "{{count}} rounds"
   ```
   Languages with more forms may use `_zero` / `_few` / `_many` as needed.
4. **Do not translate brand / technical terms:** Touka, ToukaSoul, Anthropic,
   Claude, Gemini, OpenAI, GPT, MCP, RBAC, ReAct, API, Proxy, Qdrant, Docker,
   JSON, token, Open WebUI, VS Code, Discord, Telegram, SNS, Legacy.
5. **Keep it concise.** UI space is limited — prefer short, natural phrasings for
   buttons, tabs, and labels. After translating, check the app for any text that
   overflows its button/sidebar and shorten if needed.
6. **Error messages** live under the `error.*` namespace. Their keys come from
   the backend error catalog (`ToukaProxy/config/errors.json`); just translate
   the values like any other string.

## Validate before you submit — Guard #33

Run the key-sync guard:

```
cd ToukaChat && npm run guard:i18n     # (or: python scripts/guard_all.py from repo root)
```

It checks every language and will:

- ✅ **Pass** if your language has only valid keys (missing keys are reported as a
  warning — incomplete translations are allowed and fall back to English).
- ❌ **Fail** (blocks the PR) if a language contains a **stale/extra key** that is
  not in the source, has **invalid JSON**, is **registered but missing its
  folder** (or vice-versa), or if **`en`** is incomplete.

So: never invent keys, never rename keys, keep the JSON valid.

## Maintainer setup — connecting a TMS

The catalogs are plain i18next JSON, so any major TMS works. Point it at
`ToukaChat/src/locales/`, source language **English (`en`)**, format **i18next /
nested JSON**, file mask `ToukaChat/src/locales/*/translation.json`.

### Weblate (recommended — open-source, self-hostable; matches Touka's self-host ethos)

Create a component with:

| Setting | Value |
|---|---|
| File format | `i18next JSON file` (v4) |
| File mask | `ToukaChat/src/locales/*/translation.json` |
| Monolingual base language file | `ToukaChat/src/locales/en/translation.json` |
| Source/template language | `en` |
| New translations | add as new file from base |
| Repository push | open a PR (or push to a `weblate` branch) |

### Crowdin (alternative — free for OSS)

Commit a `crowdin.yml` at the repo root:

```yaml
project_id_env: CROWDIN_PROJECT_ID
api_token_env: CROWDIN_PERSONAL_TOKEN
preserve_hierarchy: true
files:
  - source: /ToukaChat/src/locales/en/translation.json
    translation: /ToukaChat/src/locales/%two_letters_code%/translation.json
    type: i18next_json
```

After translators submit, the TMS opens a PR; Guard #33 runs in CI to validate it,
and a maintainer merges. New languages then "just work" once registered in
`src/locales/index.ts`.
