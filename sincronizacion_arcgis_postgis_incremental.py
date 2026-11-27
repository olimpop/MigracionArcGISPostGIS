
# -*- coding: utf-8 -*-
"""Sincronizacion general ArcGIS PostgreSQL -> PostgreSQL/PostGIS."""

import csv
import hashlib
import json
import os
import re
import sys
import time
import base64
from datetime import date, datetime, time as datetime_time
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from configuracion import ORIGEN, DESTINO

SOURCE = ORIGEN
DEST = DESTINO

SCHEMAS = ("gis", "hist")
STATE_SCHEMA = "etl"
STATE_TABLE = "arcgis_postgis_sync_state"
LOG_FILE = "sincronizacion_arcgis_postgis_resultado.csv"
BATCH_SIZE = 1000


def connect(cfg, label):
    params = dict(cfg)
    params["connect_timeout"] = 15
    params["application_name"] = (
        "arcgis_postgis_full_sync_" + label.lower()
    )
    print("Conectando a %s..." % label, flush=True)
    return psycopg.connect(**params)


def ident(name):
    return sql.Identifier(name)


def json_safe(v):
    if isinstance(v, memoryview):
        v = v.tobytes()
    if isinstance(v, bytes):
        return {"__bytes__": base64.b64encode(v).decode("ascii")}
    if isinstance(v, (datetime, date, datetime_time)):
        return {"__date__": v.isoformat()}
    if isinstance(v, Decimal):
        return {"__decimal__": str(v)}
    if isinstance(v, (list, tuple)):
        return [json_safe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): json_safe(x) for k, x in v.items()}
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return {"__repr__": str(v), "__type__": type(v).__name__}


def digest_row(row):
    data = json.dumps(
        [json_safe(x) for x in row],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def log_result(schema, table, status, rows, inserted, updated,
               seconds, message=""):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow([
                "fecha", "esquema", "tabla", "estado", "filas_origen",
                "insertados", "actualizados", "segundos", "mensaje"
            ])
        w.writerow([
            datetime.now().isoformat(timespec="seconds"),
            schema, table, status, rows, inserted, updated,
            round(seconds, 2), message
        ])


def discover_tables(conn):
    q = """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY(%s)
          AND table_type = 'BASE TABLE'
          AND table_name !~ '^i[0-9]+$'
        ORDER BY table_schema, table_name
    """
    with conn.cursor() as cur:
        cur.execute(q, (list(SCHEMAS),))
        return cur.fetchall()


def get_columns(conn, schema, table):
    q = """
        SELECT c.column_name, c.ordinal_position,
               c.data_type, c.udt_name
        FROM information_schema.columns c
        WHERE c.table_schema = %s AND c.table_name = %s
        ORDER BY c.ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(q, (schema, table))
        return cur.fetchall()


def get_safe_key(conn, schema, table):
    # Solo PK o indice UNIQUE valido, no parcial y de una columna.
    q = """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a
          ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
        WHERE n.nspname = %s
          AND t.relname = %s
          AND i.indisunique
          AND i.indisvalid
          AND i.indpred IS NULL
          AND i.indexprs IS NULL
          AND i.indnkeyatts = 1
          AND i.indnatts = 1
        ORDER BY i.indisprimary DESC
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(q, (schema, table))
        r = cur.fetchone()
        return r[0] if r else None


def get_geometry_info(conn, schema, table):
    # ArcGIS ST_Geometry metadata. If no geometry is listed,
    # the table is treated as a regular attribute table.
    q = """
        SELECT column_name, srid
        FROM sde.st_geometry_columns
        WHERE schema_name = %s AND table_name = %s
    """
    with conn.cursor() as cur:
        cur.execute(q, (schema, table))
        return {r[0]: int(r[1] or 0) for r in cur.fetchall()}


def ensure_state_table(dst):
    with dst.transaction():
        with dst.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    ident(STATE_SCHEMA)
                )
            )
            cur.execute(sql.SQL("""
                CREATE TABLE IF NOT EXISTS {}.{} (
                    source_schema text NOT NULL,
                    source_table text NOT NULL,
                    key_value jsonb NOT NULL,
                    row_hash text NOT NULL,
                    synced_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (source_schema, source_table, key_value)
                )
            """).format(ident(STATE_SCHEMA), ident(STATE_TABLE)))


def target_exists(dst, schema, table):
    with dst.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s)",
            ("%s.%s" % (schema, table),)
        )
        return cur.fetchone()[0] is not None


def read_state(dst, schema, table):
    q = sql.SQL("""
        SELECT key_value, row_hash
        FROM {}.{}
        WHERE source_schema = %s AND source_table = %s
    """).format(ident(STATE_SCHEMA), ident(STATE_TABLE))
    with dst.cursor() as cur:
        cur.execute(q, (schema, table))
        return {r[0]: r[1] for r in cur.fetchall()}


def state_upsert(dst, schema, table, key_value, row_hash):
    q = sql.SQL("""
        INSERT INTO {}.{}
          (source_schema, source_table, key_value, row_hash, synced_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (source_schema, source_table, key_value)
        DO UPDATE SET row_hash = EXCLUDED.row_hash,
                      synced_at = now()
    """).format(ident(STATE_SCHEMA), ident(STATE_TABLE))
    with dst.cursor() as cur:
        cur.execute(q, (schema, table, Jsonb(key_value), row_hash))


def build_select(schema, table, columns, geometries):
    expressions = []
    for name, _, _, _ in columns:
        if name in geometries:
            expressions.append(
                sql.SQL("sde.st_asbinary({})").format(ident(name))
            )
        else:
            expressions.append(ident(name))
    return sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(expressions), ident(schema), ident(table)
    )


def build_upsert(schema, table, columns, geometries, key):
    names = [c[0] for c in columns]
    values = []
    for name in names:
        if name in geometries:
            values.append(
                sql.SQL("ST_GeomFromWKB(%s, {})").format(
                    sql.Literal(geometries[name])
                )
            )
        else:
            values.append(sql.SQL("%s"))

    updates = [n for n in names if n != key]
    if updates:
        action = sql.SQL("DO UPDATE SET {}").format(
            sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(ident(n), ident(n))
                for n in updates
            )
        )
    else:
        action = sql.SQL("DO NOTHING")

    return sql.SQL("""
        INSERT INTO {}.{} ({})
        VALUES ({})
        ON CONFLICT ({}) {}
    """).format(
        ident(schema), ident(table),
        sql.SQL(", ").join(ident(n) for n in names),
        sql.SQL(", ").join(values),
        ident(key), action
    )


def sync_table(src, dst, schema, table, batch_size):
    started = time.time()

    if not target_exists(dst, schema, table):
        raise RuntimeError("No existe la tabla destino")

    source_cols = get_columns(src, schema, table)
    dest_cols = get_columns(dst, schema, table)
    if not source_cols:
        raise RuntimeError("Tabla origen sin columnas")

    source_names = [c[0] for c in source_cols]
    dest_names = {c[0] for c in dest_cols}
    missing = [n for n in source_names if n not in dest_names]
    if missing:
        raise RuntimeError(
            "Columnas ausentes en destino: " + ", ".join(missing)
        )

    source_key = get_safe_key(src, schema, table)
    dest_key = get_safe_key(dst, schema, table)
    if not source_key or source_key != dest_key:
        raise RuntimeError(
            "PENDIENTE_CLAVE: se requiere PK o UNIQUE de una columna "
            "con el mismo nombre en origen y destino"
        )

    geometries = get_geometry_info(src, schema, table)
    previous = read_state(dst, schema, table)
    key_index = source_names.index(source_key)

    select_sql = build_select(schema, table, source_cols, geometries)
    upsert_sql = build_upsert(
        schema, table, source_cols, geometries, source_key
    )

    scanned = inserted = updated = 0
    seen = set()

    print(
        "  Clave=%s | geometria=%s | estado previo=%s" %
        (source_key, len(geometries), len(previous)),
        flush=True
    )

    # Cada tabla tiene una transaccion destino independiente.
    with dst.transaction():
        cursor_name = ("sync_%s_%s" % (schema, table))[:60]
        with src.cursor(name=cursor_name) as read_cur:
            read_cur.itersize = batch_size
            read_cur.execute(select_sql)

            while True:
                batch = read_cur.fetchmany(batch_size)
                if not batch:
                    break

                for row in batch:
                    scanned += 1
                    key_value = row[key_index]

                    if key_value is None:
                        raise RuntimeError(
                            "Clave nula en origen, fila %s" % scanned
                        )
                    if key_value in seen:
                        raise RuntimeError(
                            "Clave duplicada en origen: %r" % key_value
                        )
                    seen.add(key_value)

                    current_hash = digest_row(row)
                    old_hash = previous.get(key_value)

                    if current_hash != old_hash:
                        with dst.cursor() as write_cur:
                            write_cur.execute(upsert_sql, row)

                        if old_hash is None:
                            inserted += 1
                        else:
                            updated += 1

                        state_upsert(
                            dst, schema, table, key_value, current_hash
                        )

    # No se eliminan filas del destino en esta primera version.
    src.rollback()

    return {
        "rows": scanned,
        "inserted": inserted,
        "updated": updated,
        "seconds": time.time() - started
    }


def main():
    global LOG_FILE
    import argparse

    parser = argparse.ArgumentParser(
        description="Sincronizacion general ArcGIS -> PostGIS"
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--only", nargs="*", default=None,
                        help="Ejecutar solo tablas indicadas por nombre")
    parser.add_argument("--log", default=LOG_FILE)
    args = parser.parse_args()
    LOG_FILE = args.log

    if args.batch_size < 1:
        parser.error("--batch-size debe ser mayor que cero")

    src = dst = None
    errors = 0
    total_start = time.time()

    try:
        src = connect(SOURCE, "ORIGEN")
        dst = connect(DEST, "DESTINO")
        dst.autocommit = True

        ensure_state_table(dst)
        tables = discover_tables(src)

        if args.only:
            wanted = set(args.only)
            tables = [t for t in tables if t[1] in wanted]

        print("\nTablas detectadas: %s" % len(tables), flush=True)
        print("Esquemas: gis, hist", flush=True)
        print("Eliminacion de registros destino: DESACTIVADA", flush=True)

        for schema, table in tables:
            print("\n=== %s.%s ===" % (schema, table), flush=True)
            started = time.time()
            try:
                result = sync_table(
                    src, dst, schema, table, args.batch_size
                )
                print(
                    "OK | origen=%s | insertados=%s | actualizados=%s "
                    "| %.1f segundos" %
                    (result["rows"], result["inserted"],
                     result["updated"], result["seconds"]),
                    flush=True
                )
                log_result(
                    schema, table, "OK", result["rows"],
                    result["inserted"], result["updated"],
                    result["seconds"]
                )
            except Exception as exc:
                errors += 1
                try:
                    src.rollback()
                except Exception:
                    pass
                try:
                    dst.rollback()
                except Exception:
                    pass

                message = "%s: %s" % (type(exc).__name__, exc)
                print("PENDIENTE/ERROR: " + message, flush=True)
                log_result(
                    schema, table, "ERROR", 0, 0, 0,
                    time.time() - started, message
                )

        elapsed = time.time() - total_start
        print("\n========================================")
        print("PROCESO FINALIZADO")
        print("Tablas procesadas: %s" % len(tables))
        print("Tablas con error/pendientes: %s" % errors)
        print("Tiempo total: %.2f minutos" % (elapsed / 60))
        print("Archivo de resultados: %s" % os.path.abspath(LOG_FILE))
        print("========================================", flush=True)

        if errors:
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nProceso interrumpido.", flush=True)
        sys.exit(130)
    finally:
        if src:
            src.close()
        if dst:
            dst.close()


if __name__ == "__main__":
    main()
