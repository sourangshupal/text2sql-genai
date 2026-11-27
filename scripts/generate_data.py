"""Generate ~20k+ rows per table of referentially-consistent dummy e-commerce data.

Run with:  uv run python scripts/generate_data.py

Design notes (also discussed in the notebooks):

- Every entity/fact table gets >= ~20k rows. Lookup-style tables (categories,
  suppliers) also get ~20k rows so the dataset uniformly matches the teaching
  brief; that is larger than a real taxonomy needs, which is itself a useful
  lesson for schema retrieval at scale.
- Referential integrity is enforced *while generating*, not by the DB after
  the fact: orders reference real customers/addresses, order_items snapshot
  the product price at sale time, timestamps respect causal ordering
  (created < order_date < shipped_at < delivered_at).
- Speed trick worth knowing: calling Faker once per row is ~50x slower than
  pre-generating value pools and sampling from them. 335k rows build in
  seconds this way; per-row Faker calls take minutes. Same distribution,
  same seedability.
- Rows are streamed with psycopg's COPY protocol, so loading is fast too.
- Seeded and idempotent: same seed -> same data; re-running truncates first.
"""

from __future__ import annotations

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv
from faker import Faker

load_dotenv()

SEED = int(os.getenv("DATA_SEED", "42"))
SCALE = float(os.getenv("DATA_SCALE", "1.0"))  # 1.0 => the sizes below

ROWS = {
    "categories": 20_000,
    "suppliers": 20_000,
    "customers": 20_000,
    "products": 20_000,
    "addresses": 40_000,
    "orders": 40_000,
    "order_items": 120_000,
    "payments": 40_000,
    "shipments": 30_000,
    "reviews": 25_000,
}
ROWS = {k: max(1, int(v * SCALE)) for k, v in ROWS.items()}

NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)  # fixed "present" for reproducibility
EPOCH = datetime(2022, 1, 1, tzinfo=timezone.utc)  # earliest possible event
COUNTRIES = ["United States", "United Kingdom", "India", "Germany",
             "Australia", "Canada", "France", "Japan"]
US_STATES = ["California", "Texas", "New York", "Florida", "Illinois", "Washington",
             "Georgia", "Ohio", "Michigan", "Colorado", "Oregon", "Arizona"]
CARRIERS = ["FedEx", "UPS", "DHL", "USPS", "BlueDart", "Aramex"]
STATUS_POOL = (["delivered"] * 70 + ["shipped"] * 10 + ["paid"] * 5
               + ["placed"] * 5 + ["cancelled"] * 10)
PAY_STATUS_POOL = (["completed"] * 82 + ["failed"] * 8 + ["pending"] * 6 + ["refunded"] * 4)
PAY_METHODS = ["card", "card", "card", "paypal", "bank_transfer", "cash_on_delivery", "gift_card"]


def dsn_from_env() -> str:
    url = os.environ["DATABASE_URL"]
    # Accept either a plain libpq URL or SQLAlchemy's postgresql+psycopg:// scheme.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def rand_dt(rng: random.Random, start: datetime, end: datetime) -> datetime:
    """Fast uniform random timestamp in [start, end) — avoids Faker's per-call cost."""
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randint(0, max(span - 1, 0)))


def make_pools(fake: Faker, rng: random.Random) -> dict[str, list]:
    """Pre-generate Faker value pools once; per-row cost becomes a list index."""
    return {
        "first_names": [fake.first_name() for _ in range(400)],
        "last_names": [fake.last_name() for _ in range(400)],
        "phones": [fake.phone_number()[:30] for _ in range(500)],
        "streets": [fake.street_address()[:200] for _ in range(3_000)],
        "cities": [fake.city()[:100] for _ in range(500)],
        "postcodes": [fake.postcode()[:20] for _ in range(500)],
        "companies": list({fake.company()[:145] for _ in range(3_000)}),
        "company_domains": [fake.domain_name() for _ in range(300)],
        "product_words": list({fake.word().capitalize() for _ in range(2_000)}),
        "sentences": [fake.sentence(nb_words=8)[:200] for _ in range(300)],
        "review_texts": [fake.sentence(nb_words=rng.randint(4, 20)) for _ in range(400)],
        "suffixes": ["3000", "Pro", "Max", "One", "X", "Air", "Lite", "Plus"],
        "cat_kinds": ["Goods", "Gear", "Essentials", "Pro", "Studio", "Lab"],
    }


def generate(rng: random.Random, fake: Faker) -> dict[str, int]:
    counts: dict[str, int] = {}
    started = time.time()
    p = make_pools(fake, rng)

    # ------------------------------------------------------------------
    # customers + addresses (addresses depend on customers)
    # ------------------------------------------------------------------
    customer_ids = list(range(1, ROWS["customers"] + 1))
    customer_created: dict[int, datetime] = {}
    customer_rows = []
    for cid in customer_ids:
        created = rand_dt(rng, EPOCH, NOW - timedelta(days=30))
        customer_created[cid] = created
        dob = rand_dt(rng, datetime(1945, 1, 1, tzinfo=timezone.utc),
                      datetime(2006, 1, 1, tzinfo=timezone.utc)).date()
        customer_rows.append((
            cid, rng.choice(p["first_names"]), rng.choice(p["last_names"]),
            f"user{cid}@example{rng.randint(1, 5)}.com",
            rng.choice(p["phones"]), rng.choice(COUNTRIES), dob, created,
        ))
    counts["customers"] = len(customer_rows)

    address_rows = []
    address_meta: list[tuple[int, int, datetime]] = []  # (address_id, customer_id, created)
    aid = 0
    for cid in customer_ids:
        for i in range(2):  # two addresses per customer
            aid += 1
            country = rng.choice(COUNTRIES)
            address_rows.append((
                aid, cid, rng.choice(p["streets"]), rng.choice(p["cities"]),
                rng.choice(US_STATES) if country == "United States" else None,
                rng.choice(p["postcodes"]), country, i == 0,
            ))
            address_meta.append((aid, cid, customer_created[cid]))
    counts["addresses"] = len(address_rows)

    # ------------------------------------------------------------------
    # categories (self-referencing hierarchy) + suppliers
    # ------------------------------------------------------------------
    n_root = ROWS["categories"] // 4
    category_rows = []
    cat_names: set[str] = set()
    for cat_id in range(1, ROWS["categories"] + 1):
        name = f"{rng.choice(p['product_words'])} {rng.choice(p['cat_kinds'])}"
        if name in cat_names:  # pool exhaustion -> deterministic disambiguation
            name = f"{name} {cat_id}"
        cat_names.add(name)
        parent = None if cat_id <= n_root else rng.randint(1, n_root)
        category_rows.append((cat_id, name, rng.choice(p["sentences"]), parent))
    counts["categories"] = len(category_rows)

    supplier_rows = [
        (sid, f"{rng.choice(p['companies'])}",
         f"contact{sid}@{rng.choice(p['company_domains'])}"[:200],
         rng.choice(COUNTRIES), round(rng.uniform(2.0, 5.0), 2))
        for sid in range(1, ROWS["suppliers"] + 1)
    ]
    counts["suppliers"] = len(supplier_rows)

    # ------------------------------------------------------------------
    # products (depend on categories + suppliers)
    # ------------------------------------------------------------------
    product_rows = []
    product_price: dict[int, float] = {}
    product_active: dict[int, bool] = {}
    for pid in range(1, ROWS["products"] + 1):
        cost = round(rng.uniform(2, 500), 2)
        price = round(cost * rng.uniform(1.2, 2.5), 2)  # margin over cost
        active = rng.random() > 0.08
        product_rows.append((
            pid, f"SKU-{pid:06d}", f"{rng.choice(p['product_words'])} {rng.choice(p['suffixes'])}",
            rng.randint(n_root + 1, ROWS["categories"]),  # leaf categories only
            rng.randint(1, ROWS["suppliers"]),
            price, cost, rng.randint(0, 500), active,
            rand_dt(rng, EPOCH, NOW),
        ))
        product_price[pid] = price
        product_active[pid] = active
    counts["products"] = len(product_rows)

    # ------------------------------------------------------------------
    # orders + order_items (depend on customers/addresses/products)
    # ------------------------------------------------------------------
    customer_addresses: dict[int, list[int]] = {}
    for a_id, c_id, _ in address_meta:
        customer_addresses.setdefault(c_id, []).append(a_id)

    order_rows = []
    order_date: dict[int, datetime] = {}
    order_status: dict[int, str] = {}
    for oid in range(1, ROWS["orders"] + 1):
        cid = rng.randint(1, ROWS["customers"])
        status = rng.choice(STATUS_POOL)
        od = rand_dt(rng, customer_created[cid] + timedelta(hours=1), NOW)
        shipped = delivered = None
        if status in ("shipped", "delivered"):
            shipped = od + timedelta(hours=rng.randint(6, 96))
        if status == "delivered":
            delivered = shipped + timedelta(days=rng.randint(1, 10))
        order_rows.append((oid, cid, rng.choice(customer_addresses[cid]), status, od, shipped, delivered))
        order_date[oid] = od
        order_status[oid] = status
    counts["orders"] = len(order_rows)

    active_products = [pid for pid, a in product_active.items() if a]
    item_rows = []
    iid = 0
    for oid in range(1, ROWS["orders"] + 1):
        n_items = rng.randint(1, 2) if order_status[oid] == "placed" else rng.randint(1, 5)
        for _ in range(n_items):
            iid += 1
            pid = active_products[rng.randint(0, len(active_products) - 1)]
            item_rows.append((iid, oid, pid, rng.randint(1, 4), product_price[pid]))
    counts["order_items"] = len(item_rows)

    # ------------------------------------------------------------------
    # payments + shipments (depend on orders)
    # ------------------------------------------------------------------
    payment_rows = []
    pid_ = 0
    for oid in range(1, ROWS["orders"] + 1):
        n_pay = 1 + (1 if rng.random() < 0.12 else 0)  # occasional retry
        for _ in range(n_pay):
            pid_ += 1
            pstatus = rng.choice(PAY_STATUS_POOL)
            paid = order_date[oid] + timedelta(minutes=rng.randint(1, 120)) if pstatus != "pending" else None
            payment_rows.append((pid_, oid, rng.choice(PAY_METHODS),
                                 round(rng.uniform(10, 900), 2), pstatus, paid))
    counts["payments"] = len(payment_rows)

    shipment_rows = []
    sid_ = 0
    for oid in range(1, ROWS["orders"] + 1):
        if order_status[oid] not in ("shipped", "delivered"):
            continue
        n_ship = 1 + (1 if rng.random() < 0.15 else 0)  # occasionally split shipment
        for _ in range(n_ship):
            sid_ += 1
            od = order_date[oid]
            shipped = od + timedelta(hours=rng.randint(6, 96))
            delivered = shipped + timedelta(days=rng.randint(1, 10)) if order_status[oid] == "delivered" else None
            shipment_rows.append((
                sid_, oid, rng.choice(CARRIERS), f"TRK{rng.randint(10**9, 10**10 - 1)}",
                shipped, delivered, round(rng.uniform(3, 25), 2),
            ))
    counts["shipments"] = len(shipment_rows)

    review_rows = [
        (rid, rng.randint(1, ROWS["products"]), rng.randint(1, ROWS["customers"]),
         rng.choices([1, 2, 3, 4, 5], weights=[5, 8, 15, 32, 40])[0],
         rng.choice(p["review_texts"]) if rng.random() > 0.2 else None,
         rand_dt(rng, EPOCH, NOW))
        for rid in range(1, ROWS["reviews"] + 1)
    ]
    counts["reviews"] = len(review_rows)

    # ------------------------------------------------------------------
    # Load everything (truncate first for idempotency)
    # ------------------------------------------------------------------
    tables_in_order = [
        ("categories", ["category_id", "name", "description", "parent_id"], category_rows),
        ("suppliers", ["supplier_id", "name", "contact_email", "country", "rating"], supplier_rows),
        ("customers", ["customer_id", "first_name", "last_name", "email", "phone", "country", "date_of_birth", "created_at"], customer_rows),
        ("addresses", ["address_id", "customer_id", "line1", "city", "state", "postal_code", "country", "is_default"], address_rows),
        ("products", ["product_id", "sku", "name", "category_id", "supplier_id", "unit_price", "cost_price", "stock_qty", "is_active", "created_at"], product_rows),
        ("orders", ["order_id", "customer_id", "address_id", "status", "order_date", "shipped_at", "delivered_at"], order_rows),
        ("order_items", ["order_item_id", "order_id", "product_id", "quantity", "unit_price"], item_rows),
        ("payments", ["payment_id", "order_id", "method", "amount", "status", "paid_at"], payment_rows),
        ("shipments", ["shipment_id", "order_id", "carrier", "tracking_no", "shipped_at", "delivered_at", "shipping_cost"], shipment_rows),
        ("reviews", ["review_id", "product_id", "customer_id", "rating", "review_text", "created_at"], review_rows),
    ]

    with psycopg.connect(dsn_from_env()) as conn, conn.cursor() as cur:
        # Truncate only what exists (e.g. a destructive agent run may have dropped a table)
        existing = {r[0] for r in cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")}
        want = ["categories", "suppliers", "customers", "addresses", "products",
                "orders", "order_items", "payments", "shipments", "reviews"]
        to_truncate = [t for t in want if t in existing]
        if to_truncate:
            cur.execute(f"TRUNCATE {', '.join(to_truncate)} RESTART IDENTITY CASCADE")
        total = 0
        for table, columns, rows in tables_in_order:
            t0 = time.time()
            col_list = ", ".join(columns)
            with cur.copy(f"COPY {table} ({col_list}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            conn.commit()
            total += len(rows)
            print(f"  {table:<14} {len(rows):>7,} rows in {time.time() - t0:5.1f}s", flush=True)
        cur.execute("ANALYZE")
        conn.commit()

    print(f"\nDone: {total:,} rows across {len(tables_in_order)} tables "
          f"in {time.time() - started:.1f}s (seed={SEED}, scale={SCALE})")
    return counts


def main() -> None:
    if "DATABASE_URL" not in os.environ:
        sys.exit("DATABASE_URL not set. Copy .env.example to .env and fill in your Neon connection string.")
    rng = random.Random(SEED)
    fake = Faker()
    fake.seed_instance(SEED)
    print(f"Generating dummy data (seed={SEED}, scale={SCALE}) ...", flush=True)
    generate(rng, fake)


if __name__ == "__main__":
    main()
