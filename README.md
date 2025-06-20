# InventoryAPI

> **Status:** Release Candidate – central inventory micro‑service shared across the Culvana platform.

`InventoryAPI` offers **real‑time stock tracking** and **cost history** for every ingredient, beverage, and supply item in a restaurant operation. Upstream ingest pipelines (InvoiceAPI, Manual Count App, POS integration) continuously push delta updates; downstream consumers (RecipeAPI, analytics dashboards) query current levels and historical trends.

---

## ⚙️ Key Capabilities

| Function                | Route                                                                                           | Description                                            |
| ----------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| **Search items**        | `GET /inventory/items?q=romaine&mode=hybrid`                                                    | Hybrid keyword + embedding search.                     |
| **Snapshot counts**     | `POST /inventory/counts`                                                                        | Upload nightly count sheet; auto‑reconciles shrinkage. |
| **Stream updates**      | Server‑Sent Events at `/inventory/stream` emit stock level diffs for WebSocket‑limited clients. |                                                        |
| **Vendor cost history** | `GET /inventory/items/{id}/costs`                                                               | Returns time‑series for forecasting.                   |
| **Bulk import**         | `POST /inventory/import` accepts CSV/XLSX defined by `schemas/inventory_import.schema.json`.    |                                                        |

---

## 🏗️ Architecture

* **DB** – PostgreSQL (TimescaleDB) storing current `on_hand_qty` + `cost_history` hypertable.
* **Cache** – Redis for hot lookups and SSE fan‑out.
* **Queue** – Azure Service Bus to decouple heavy reconciliations.
* **API** – FastAPI served by Uvicorn / Gunicorn.

Diagram available in `/docs/architecture.png`.

---

## 📂 Repository Layout

```
InventoryAPI/
├── app/
│   ├── main.py            # FastAPI instance
│   ├── models.py          # SQLAlchemy
│   ├── routers/
│   │   ├── items.py
│   │   └── counts.py
│   ├── services/
│   │   ├── search.py      # pgvector queries
│   │   ├── cost.py        # cost history utils
│   │   └── cache.py       # Redis helpers
│   └── core/config.py
├── alembic/
├── requirements.txt
└── Dockerfile
```

---

## 🛠 Environment Variables

| Name              | Purpose                                 |
| ----------------- | --------------------------------------- |
| `DATABASE_URL`    | Postgres / Timescale connection string. |
| `REDIS_URL`       | Redis cache.                            |
| `BUS_CONN_STR`    | Azure Service Bus for background jobs.  |
| `ALLOWED_ORIGINS` | CORS.                                   |
| `OPENAI_API_KEY`  | Used only when `mode=semantic` search.  |

---

## 🚀 Running Locally

```bash
docker compose -f docker-compose.dev.yml up --build
```

This spins up PG, Redis, and the API on `localhost:8000`.

---

## 🧪 Test Suite

```
pytest -m "not integration"  # unit only
pytest -m integration         # requires docker compose running
```

---

## 🌐 Deployment

* **Azure Container Apps** recommended for auto‑scaling SSE.
* Apply migrations via `alembic upgrade head` on startup.

---

## 📝 License

MIT © Culvana 2025
