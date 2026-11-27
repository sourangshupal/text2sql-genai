# text2sql-genai

Text-to-SQL over **Neon serverless Postgres**, taught two ways: a **LangChain**
agent notebook and a **LlamaIndex** query-engine notebook. Both run against the
same 10-table, ~200k-row synthetic e-commerce dataset and the same eval set.

Audience: advanced learners. The notebooks explain the *why*, not just the *how* —
schema grounding, semantic layers, retrieval over many tables, deterministic
SQL guards, and why the database role — never the prompt — is the security boundary.

## Layout

```
scripts/schema.sql          10-table schema + COMMENT metadata + read-only role
scripts/generate_data.py    Faker-based generator (~20k rows/table, seeded, idempotent)
evals/questions.jsonl       25 question -> gold-SQL pairs used to score both notebooks
notebooks/01_langchain_sql_agent.ipynb
notebooks/02_llamaindex_text2sql.ipynb
```

## Setup

```bash
# 1. Install everything (uv handles the venv)
uv sync

# 2. Configure credentials
cp .env.example .env       # then fill in DATABASE_URL and OPENAI_API_KEY

# 3. Create the schema and load ~335k rows of dummy data
#    (Tip: create a branch in the Neon console first and point .env at it,
#     so AI-generated SQL is never tested against production data.)
psql "$DATABASE_URL" -f scripts/schema.sql
uv run python scripts/generate_data.py

# 4. Launch the notebooks with the project environment
uv run jupyter lab notebooks/
```

## The two paths, in one paragraph each

- **LangChain** (`01`): the current documented pattern — a `create_agent` agent
  with four custom tools (list tables, get schema + sample rows, LLM query
  checker, guarded query executor). The executor validates every generated
  query with sqlglot (single statement, SELECT-only, LIMIT enforced) before it
  touches the database, and `HumanInTheLoopMiddleware` can pause execution for
  human approval.
- **LlamaIndex** (`02`): the `SQLDatabase` abstraction plus the progression
  `NLSQLTableQueryEngine` -> `SQLTableRetrieverQueryEngine` (schema retrieval
  for many tables, with `context_str` semantic hints) -> row/column value
  retrievers for entity grounding -> `NLSQLRetriever` composed by hand.

Both notebooks finish by scoring themselves against `evals/questions.jsonl`, and both are
traced with **Langfuse** (see below).

## Observability (Langfuse)

Every run in both notebooks is traced to Langfuse:

- **Notebook 01 (LangChain)** uses Langfuse's native `CallbackHandler`
  (`langfuse.langchain`). Demo, adversarial, and eval runs each get their own trace,
  grouped into sessions (`lc-demo`, `lc-adversarial`, `lc-eval-*`), tagged for
  filtering, and every eval question is scored with an `execution_accuracy` score.
- **Notebook 02 (LlamaIndex)** uses `openinference-instrumentation-llama-index`:
  LlamaIndex operations emit OpenTelemetry spans that are routed to Langfuse's OTLP
  endpoint (`/api/public/otel`) with the auth header derived from your keys.

Filter by `session_id` starting with `lc-eval` / `li-eval` to compare the two
frameworks on the same question set.
