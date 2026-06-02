"""Idempotent demo-data loader for the three bundled databases.

Run as a one-shot (the `seeder` service in docker-compose does this):
    python -m dblab_agent.seed

It waits for each database to accept connections, then creates a small
e-commerce schema (customers / products / orders + a view) with a handful of
rows — enough to ask the agent interesting questions out of the box. Safe to
re-run: tables are created IF NOT EXISTS and rows inserted only when empty.
"""
import time

from .connections import CONNECTIONS, open_conn

# Dialect-specific DDL. openGauss uses the same DDL as PostgreSQL.
_DDL = {
    "pg": [
        """CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            email VARCHAR(120) UNIQUE,
            country VARCHAR(40),
            created_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            category VARCHAR(40),
            price NUMERIC(10,2) NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_id INT REFERENCES customers(id),
            product_id INT REFERENCES products(id),
            quantity INT NOT NULL DEFAULT 1,
            status VARCHAR(20) DEFAULT 'pending',
            ordered_at TIMESTAMP DEFAULT now()
        )""",
        """CREATE OR REPLACE VIEW order_summary AS
            SELECT o.id AS order_id, c.name AS customer, p.name AS product,
                   o.quantity, (o.quantity * p.price) AS amount, o.status
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            JOIN products p ON p.id = o.product_id""",
    ],
    "mysql": [
        """CREATE TABLE IF NOT EXISTS customers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            email VARCHAR(120) UNIQUE,
            country VARCHAR(40),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS products (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(80) NOT NULL,
            category VARCHAR(40),
            price DECIMAL(10,2) NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS orders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            customer_id INT,
            product_id INT,
            quantity INT NOT NULL DEFAULT 1,
            status VARCHAR(20) DEFAULT 'pending',
            ordered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE OR REPLACE VIEW order_summary AS
            SELECT o.id AS order_id, c.name AS customer, p.name AS product,
                   o.quantity, (o.quantity * p.price) AS amount, o.status
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            JOIN products p ON p.id = o.product_id""",
    ],
}

_CUSTOMERS = [
    ("Alice Chen", "alice@example.com", "China"),
    ("Bruno Costa", "bruno@example.com", "Brazil"),
    ("Carla Diaz", "carla@example.com", "Spain"),
    ("Deepak Rao", "deepak@example.com", "India"),
    ("Erika Novak", "erika@example.com", "Czechia"),
]
_PRODUCTS = [
    ("Mechanical Keyboard", "peripherals", 79.90),
    ("27\" Monitor", "displays", 219.00),
    ("USB-C Hub", "accessories", 34.50),
    ("Noise-Cancel Headset", "audio", 129.00),
    ("Ergonomic Mouse", "peripherals", 45.00),
]
_ORDERS = [
    (1, 1, 2, "shipped"), (1, 3, 1, "shipped"), (2, 2, 1, "pending"),
    (3, 4, 1, "delivered"), (3, 5, 3, "pending"), (4, 1, 1, "cancelled"),
    (5, 2, 2, "delivered"), (5, 3, 4, "pending"),
]


def _wait_for(conn_id: str, timeout: int = 150):
    cfg = CONNECTIONS[conn_id]
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            conn = open_conn(cfg)
            conn.close()
            return True
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(3)
    print(f"[seed] {conn_id}: not reachable within {timeout}s ({last})", flush=True)
    return False


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cur.fetchone()[0])


def seed_one(conn_id: str) -> None:
    cfg = CONNECTIONS[conn_id]
    driver = cfg["driver"]
    ddl = _DDL["mysql" if driver == "mysql" else "pg"]
    ph = "%s"  # both psycopg2 and pymysql use %s placeholders
    if not _wait_for(conn_id):
        return
    conn = open_conn(cfg)
    try:
        cur = conn.cursor()
        for stmt in ddl:
            cur.execute(stmt)
        if driver == "pg":
            conn.commit()
        if _count(cur, "customers") == 0:
            cur.executemany(
                f"INSERT INTO customers (name, email, country) VALUES ({ph},{ph},{ph})",
                _CUSTOMERS,
            )
            cur.executemany(
                f"INSERT INTO products (name, category, price) VALUES ({ph},{ph},{ph})",
                _PRODUCTS,
            )
            cur.executemany(
                "INSERT INTO orders (customer_id, product_id, quantity, status) "
                f"VALUES ({ph},{ph},{ph},{ph})",
                _ORDERS,
            )
            if driver == "pg":
                conn.commit()
        cur.close()
        print(f"[seed] {conn_id}: ok", flush=True)
    except Exception as e:
        print(f"[seed] {conn_id}: FAILED — {type(e).__name__}: {e}", flush=True)
    finally:
        conn.close()


def main() -> None:
    for conn_id in ("postgres", "mysql", "opengauss"):
        if conn_id in CONNECTIONS:
            seed_one(conn_id)


if __name__ == "__main__":
    main()
