# Retail Analytics Agent

A conversational analytics assistant for retail executives. It investigates business questions using BigQuery data and curated analyst knowledge, supports follow-up exploration, and produces reports with evidence and action items.

## Project status

Implementation has not started. The intended application uses a CLI connected to an HTTP backend, Pydantic AI for the agent, Temporal for durable execution, and PostgreSQL for application state. BigQuery provides read-only retail analysis; model and database credentials stay on the backend.

Runnable setup instructions, public architecture documentation and evaluation results will be added as their implementations are verified.

Do not commit credentials, raw query results or private conversation data.
