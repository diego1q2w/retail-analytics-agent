# Google access setup (BigQuery and Gemini)

This guide gets a developer machine ready for the live BigQuery dataset and Gemini. Nothing here needs production infrastructure, service-account keys or broad IAM roles.

## What you do yourself

| Step | Who | Notes |
| --- | --- | --- |
| Create or choose a Google Cloud project | you | A dedicated project keeps query jobs and any future costs separate. |
| Enable the BigQuery API on it | you | Console, or `gcloud services enable bigquery.googleapis.com --project <project>`. |
| Log in for Application Default Credentials | you | Browser consent; see below. |
| Create a Gemini API key in Google AI Studio | you | Browser login; put the key only in your ignored `.env`. |
| Optional: OpenAI fallback key | you | Separate account with prepaid API credit. Not needed for the checks. |

Never paste keys or tokens into chat, issues, logs or commits.

## Public dataset versus your project

The data lives in the public dataset `bigquery-public-data.thelook_ecommerce` (tables `orders`, `order_items`, `products`, `users`). Google owns and maintains it; you do not copy or recreate the tables. What you need is your own project to run query jobs in. Setting `GOOGLE_CLOUD_PROJECT` selects that project: it is the query (and billing) project, not the dataset owner.

## Authentication

```sh
gcloud auth application-default login
gcloud auth application-default set-quota-project <project>
```

- Credentials are stored by `gcloud` in your user profile, outside the repository.
- Owning your own project is enough. Do not grant Owner or Editor to anyone for this, and do not download service-account keys. In a shared project, the narrow roles are BigQuery Job User on the project (to create query jobs); reading public data needs no role.
- Expired or revoked credentials show as "credentials are expired or revoked"; rerun the login command.

## Billing and cost controls

- Without a billing account, a project runs in the BigQuery sandbox: no billing, a limited free query quota, and tables expire by default. That is enough here since we only read public tables. Check Google's current sandbox and pricing pages for the exact allowances; this guide does not promise a number.
- Billing and budget alerts are an optional upgrade. Budget alerts only notify; they are not spending caps.
- The checks and application bound cost per query with `maximum_bytes_billed` (100 MB in the credential check).

## Gemini

Create a key in Google AI Studio and set it in `.env` (copied from `.env.example`):

```text
GOOGLE_CLOUD_PROJECT=<your project id>
GEMINI_API_KEY=<your key>
GEMINI_MODEL=gemini-3-flash-preview
```

The model setting is a configurable default for the check only; choosing models for the agent is a separate decision. Availability and rate limits depend on your key and tier: a model can be listed by the models endpoint yet answer 404 on `generateContent`. Confirm in Google's current model and rate-limit documentation.

## Verify

```sh
retail-analytics-check-credentials
# or: python -m retail_analytics.bootstrap.check_credentials
```

It checks, in order: Application Default Credentials, metadata (row and column counts) for the four tables, a dry run that must scan under 100 MB, and a minimal Gemini request. Each failure prints a one-line cause and a fix. The output contains no keys or tokens. Exit status: 0 all passed, 1 a check failed, 2 invalid configuration.

The same checks run as tests marked `live`: `python -m pytest -m live`. They read `.env` in the repository root and skip when the project, key or credentials are absent.

## Common failures

| Message | Fix |
| --- | --- |
| no application default credentials | `gcloud auth application-default login` |
| credentials are expired or revoked | log in again |
| permission denied (HTTP 403) on BigQuery | enable the BigQuery API on your project; check you can create query jobs there |
| API key rejected (Gemini) | create a new key in AI Studio and update `.env` |
| model not available to this key (404) | set `GEMINI_MODEL` to a model your key supports |
| rate limit or quota exceeded (429) | wait, or check quota in AI Studio |
