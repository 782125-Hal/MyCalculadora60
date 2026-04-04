"""
migrar_datos.py — Migra datos de SQLite local a PostgreSQL (Railway)

Uso:
    DATABASE_URL=postgresql://user:pass@host:5432/dbname python migrar_datos.py

El script es SOLO LECTURA sobre SQLite. No modifica ni borra datos locales.
Los registros que ya existen en PostgreSQL se omiten sin error.
"""

import os
import sqlite3
import sys
from decimal import Decimal

# ── Verificar DATABASE_URL ────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    print("ERROR: Define la variable de entorno DATABASE_URL antes de correr el script.")
    print("Ejemplo:")
    print("  DATABASE_URL=postgresql://user:pass@host:5432/dbname python migrar_datos.py")
    sys.exit(1)

# Railway a veces entrega URLs con prefijo 'postgres://' en lugar de 'postgresql://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ── Conexión a SQLite ─────────────────────────────────────────────────────────
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")

if not os.path.exists(SQLITE_PATH):
    print(f"ERROR: No se encontró la base de datos SQLite en: {SQLITE_PATH}")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 no está instalado. Corre: pip install psycopg2-binary")
    sys.exit(1)


def conectar_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def conectar_postgres():
    return psycopg2.connect(DATABASE_URL)


def leer_tabla(sqlite_conn, tabla):
    cur = sqlite_conn.cursor()
    cur.execute(f'SELECT * FROM "{tabla}"')
    return cur.fetchall()


def migrar_clientes(sqlite_conn, pg_conn):
    """prestamos_cliente — sin dependencias externas."""
    filas = leer_tabla(sqlite_conn, "prestamos_cliente")
    if not filas:
        print("  prestamos_cliente: sin registros.")
        return 0

    migrados = 0
    cur = pg_conn.cursor()
    for f in filas:
        cur.execute(
            """
            INSERT INTO prestamos_cliente (id, nombre, telefono)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (f["id"], f["nombre"], f["telefono"]),
        )
        if cur.rowcount:
            migrados += 1

    pg_conn.commit()
    print(f"  prestamos_cliente: {migrados} de {len(filas)} registros migrados.")
    return migrados


def migrar_prestamos(sqlite_conn, pg_conn):
    """prestamos_prestamo — depende de prestamos_cliente."""
    filas = leer_tabla(sqlite_conn, "prestamos_prestamo")
    if not filas:
        print("  prestamos_prestamo: sin registros.")
        return 0

    migrados = 0
    cur = pg_conn.cursor()
    for f in filas:
        cur.execute(
            """
            INSERT INTO prestamos_prestamo (
                id, cliente_id, nombre_cliente, telefono,
                monto_original, tasa_interes_anual, tipo_pago,
                fecha_inicio, saldo_actual, pago_mensual,
                plazo_meses, activo, ultimo_pago, modo
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                f["id"],
                f["cliente_id"],
                f["nombre_cliente"],
                f["telefono"],
                f["monto_original"],
                f["tasa_interes_anual"],
                f["tipo_pago"],
                f["fecha_inicio"],
                f["saldo_actual"],
                f["pago_mensual"],
                f["plazo_meses"],
                bool(f["activo"]),
                f["ultimo_pago"],
                f["modo"],
            ),
        )
        if cur.rowcount:
            migrados += 1

    pg_conn.commit()
    print(f"  prestamos_prestamo: {migrados} de {len(filas)} registros migrados.")
    return migrados


def migrar_movimientos(sqlite_conn, pg_conn):
    """prestamos_movimiento — depende de prestamos_prestamo."""
    filas = leer_tabla(sqlite_conn, "prestamos_movimiento")
    if not filas:
        print("  prestamos_movimiento: sin registros.")
        return 0

    migrados = 0
    cur = pg_conn.cursor()
    for f in filas:
        cur.execute(
            """
            INSERT INTO prestamos_movimiento (id, prestamo_id, fecha, monto, tipo, descripcion)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                f["id"],
                f["prestamo_id"],
                f["fecha"],
                f["monto"],
                f["tipo"],
                f["descripcion"],
            ),
        )
        if cur.rowcount:
            migrados += 1

    pg_conn.commit()
    print(f"  prestamos_movimiento: {migrados} de {len(filas)} registros migrados.")
    return migrados


def migrar_pagos(sqlite_conn, pg_conn):
    """prestamos_pago — depende de prestamos_prestamo."""
    filas = leer_tabla(sqlite_conn, "prestamos_pago")
    if not filas:
        print("  prestamos_pago: sin registros.")
        return 0

    migrados = 0
    cur = pg_conn.cursor()
    for f in filas:
        cur.execute(
            """
            INSERT INTO prestamos_pago (id, prestamo_id, fecha, monto)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (f["id"], f["prestamo_id"], f["fecha"], f["monto"]),
        )
        if cur.rowcount:
            migrados += 1

    pg_conn.commit()
    print(f"  prestamos_pago: {migrados} de {len(filas)} registros migrados.")
    return migrados


def sincronizar_secuencias(pg_conn):
    """
    Después de insertar con IDs manuales, PostgreSQL no actualiza las secuencias
    automáticamente. Esto las sincroniza para que el próximo INSERT no colisione.
    """
    cur = pg_conn.cursor()
    tablas = [
        "prestamos_cliente",
        "prestamos_prestamo",
        "prestamos_movimiento",
        "prestamos_pago",
    ]
    for tabla in tablas:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{tabla}', 'id'), "
            f"COALESCE(MAX(id), 1)) FROM {tabla}"
        )
    pg_conn.commit()
    print("  Secuencias PostgreSQL sincronizadas.")


def main():
    print("=" * 55)
    print("  Migración SQLite → PostgreSQL")
    print("=" * 55)
    print(f"\nOrigen : {SQLITE_PATH}")
    print(f"Destino : {DATABASE_URL[:40]}...\n")

    sqlite_conn = conectar_sqlite()

    try:
        pg_conn = conectar_postgres()
    except Exception as e:
        print(f"ERROR al conectar a PostgreSQL: {e}")
        sqlite_conn.close()
        sys.exit(1)

    print("Conexiones establecidas. Iniciando migración...\n")

    total = 0
    try:
        # Orden correcto: padres antes que hijos
        total += migrar_clientes(sqlite_conn, pg_conn)
        total += migrar_prestamos(sqlite_conn, pg_conn)
        total += migrar_movimientos(sqlite_conn, pg_conn)
        total += migrar_pagos(sqlite_conn, pg_conn)

        print("\nSincronizando secuencias...")
        sincronizar_secuencias(pg_conn)

    except Exception as e:
        pg_conn.rollback()
        print(f"\nERROR durante la migración: {e}")
        sqlite_conn.close()
        pg_conn.close()
        sys.exit(1)

    sqlite_conn.close()
    pg_conn.close()

    print(f"\n{'=' * 55}")
    print(f"  Migración completada: {total} registros migrados")
    print(f"{'=' * 55}")
    print("\nLos datos originales en SQLite NO fueron modificados.")


if __name__ == "__main__":
    main()
