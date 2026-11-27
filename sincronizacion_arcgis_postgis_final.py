# -*- coding: utf-8 -*-
"""Sincronización ArcGIS PostgreSQL -> PostgreSQL/PostGIS, resiliente y reanudable.

- Descubre tablas base de gis e hist; excluye tablas técnicas iNNN.
- Tablas con PK/UNIQUE simple igual en origen/destino: upsert por clave y hash.
- Tablas sin clave segura: DELETE + INSERT completo dentro de una transacción.
- No elimina tablas físicas, no inventa claves, no descarta filas.
- Geometrías SDE: sde.st_astext -> ST_GeomFromText (evita WKB ArcGIS especial).
- Reabre ambas conexiones y reintenta la tabla completa hasta 3 veces.
- Omite tablas cuyo último resultado del CSV sea OK; --reprocesar fuerza todo.
- No propaga eliminaciones de origen en tablas incrementales (modo conservador).
"""

import argparse, csv, hashlib, json, os, re, sys, time
from datetime import date, datetime, time as dtime
from decimal import Decimal
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from configuracion import ORIGEN, DESTINO

SOURCE = ORIGEN
DEST = DESTINO
SCHEMAS = ("gis", "hist")
STATE_SCHEMA, STATE_TABLE = "etl", "arcgis_postgis_sync_state"
LOG_FILE = "sincronizacion_arcgis_postgis_resultado.csv"
BATCH, RETRIES, WAIT = 500, 3, 20
EXCLUDE = re.compile(r"^i[0-9]+$", re.I)
def connect(cfg):
    params = dict(cfg)
    params["connect_timeout"] = 20
    params["application_name"] = (
        "arcgis_postgis_sync_" + cfg["dbname"]
    )
    return psycopg.connect(**params)
def qname(schema, table):
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def columns(conn, schema, table):
    q = """
    SELECT c.column_name,c.data_type,c.udt_name,c.ordinal_position,
           pg_catalog.format_type(a.atttypid,a.atttypmod)
    FROM information_schema.columns c
    JOIN pg_catalog.pg_namespace n ON n.nspname=c.table_schema
    JOIN pg_catalog.pg_class cl ON cl.relnamespace=n.oid AND cl.relname=c.table_name
    JOIN pg_catalog.pg_attribute a ON a.attrelid=cl.oid AND a.attname=c.column_name
    WHERE c.table_schema=%s AND c.table_name=%s
      AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(q, (schema, table)); return cur.fetchall()


def discover():
    c = connect(SOURCE)
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT table_schema,table_name
                           FROM information_schema.tables
                           WHERE table_type='BASE TABLE' AND table_schema=ANY(%s)
                           ORDER BY table_schema,table_name""", (list(SCHEMAS),))
            return [(s,t) for s,t in cur.fetchall() if not EXCLUDE.match(t)]
    finally:
        c.close()


def geom_info(src, schema, table):
    with src.cursor() as cur:
        cur.execute("""SELECT column_name,srid FROM sde.st_geometry_columns
                       WHERE schema_name=%s AND table_name=%s""", (schema,table))
        return {n:int(s or 0) for n,s in cur.fetchall()}


def simple_key(conn, schema, table):
    q = """
    SELECT a.attname
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class t ON t.oid=i.indrelid
    JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[0]
    WHERE n.nspname=%s AND t.relname=%s AND i.indisunique
      AND i.indisvalid AND i.indisready AND i.indpred IS NULL
      AND i.indexprs IS NULL AND i.indnatts=1 AND i.indnkeyatts=1
    ORDER BY i.indisprimary DESC LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(q,(schema,table)); r=cur.fetchone()
        return r[0] if r else None


def ensure_state(dst):
    with dst.transaction():
        with dst.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(STATE_SCHEMA)))
            cur.execute(sql.SQL("""
              CREATE TABLE IF NOT EXISTS {}.{}(
                source_schema text NOT NULL, source_table text NOT NULL,
                key_value jsonb NOT NULL, row_hash text NOT NULL,
                synced_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY(source_schema,source_table,key_value))
            """).format(sql.Identifier(STATE_SCHEMA),sql.Identifier(STATE_TABLE)))


def state_read(dst,schema,table):
    with dst.cursor() as cur:
        cur.execute(sql.SQL("""SELECT key_value,row_hash FROM {}.{}
          WHERE source_schema=%s AND source_table=%s""").format(
          sql.Identifier(STATE_SCHEMA),sql.Identifier(STATE_TABLE)),(schema,table))
        return dict(cur.fetchall())


def safe(v):
    if isinstance(v,memoryview): v=v.tobytes()
    if isinstance(v,bytes):
        import base64
        return {"bytes":base64.b64encode(v).decode("ascii")}
    if isinstance(v,(datetime,date,dtime)): return {"datetime":v.isoformat()}
    if isinstance(v,Decimal): return {"decimal":str(v)}
    if isinstance(v,float) and (v!=v or abs(v)==float("inf")): return {"float":str(v)}
    if isinstance(v,(list,tuple)): return [safe(x) for x in v]
    if isinstance(v,dict): return {str(k):safe(x) for k,x in v.items()}
    if v is None or isinstance(v,(str,int,float,bool)): return v
    return {"repr":str(v),"type":type(v).__name__}


def row_hash(row):
    b=json.dumps([safe(x) for x in row],sort_keys=True,ensure_ascii=False,
                 separators=(",",":")).encode("utf-8")
    return hashlib.sha256(b).hexdigest()


def source_select(schema,table,cols,geoms):
    expr=[]
    for c in cols:
        n=c[0]
        expr.append(sql.SQL("sde.st_astext({}) AS {}").format(
            sql.Identifier(n),sql.Identifier(n)) if n in geoms else sql.Identifier(n))
    return sql.SQL("SELECT {} FROM {}").format(sql.SQL(",").join(expr),qname(schema,table))


def insert_stmt(schema,table,cols,geoms,key=None):
    names=[c[0] for c in cols]; vals=[]
    for n in names:
        vals.append(sql.SQL("CASE WHEN %s::text IS NULL THEN NULL ELSE ST_GeomFromText(%s::text,{}) END").format(
            sql.Literal(geoms[n])) if n in geoms else sql.SQL("%s"))
    stmt=sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        qname(schema,table),sql.SQL(",").join(map(sql.Identifier,names)),sql.SQL(",").join(vals))
    if key:
        upd=[n for n in names if n!=key]
        act=(sql.SQL("DO UPDATE SET {}").format(sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(n),sql.Identifier(n)) for n in upd))
             if upd else sql.SQL("DO NOTHING"))
        stmt+=sql.SQL(" ON CONFLICT ({}) {}").format(sql.Identifier(key),act)
    return stmt


def params_for(row,cols,geoms):
    out=[]
    for i,v in enumerate(row):
        if cols[i][0] in geoms: out.extend((v,v))
        else: out.append(v)
    return tuple(out)


def target_exists(dst,schema,table):
    with dst.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)",("%s.%s"%(schema,table),))
        return cur.fetchone()[0] is not None


def validate(src_cols,dst_cols,schema,table):
    a=[x[0] for x in src_cols]; b=[x[0] for x in dst_cols]
    missing=[x for x in a if x not in b]
    extra=[x for x in b if x not in a]
    if missing or extra:
        raise RuntimeError("Estructura distinta. Faltan destino=%s; sobran destino=%s"%
                           (missing,extra))


def sync_one(schema,table,batch_size):
    src=connect(SOURCE)
    try: dst=connect(DEST)
    except Exception: src.close(); raise
    rows=ins=upd=0; started=time.time()
    try:
        sc=columns(src,schema,table); dc=columns(dst,schema,table)
        if not sc: raise RuntimeError("Tabla sin columnas en origen")
        if not target_exists(dst,schema,table): raise RuntimeError("No existe tabla destino")
        validate(sc,dc,schema,table)
        geoms=geom_info(src,schema,table)
        names=[c[0] for c in sc]
        si=simple_key(src,schema,table); di=simple_key(dst,schema,table)
        key=si if si and si==di and si in names else None
        sel=source_select(schema,table,sc,geoms)
        ins_sql=insert_stmt(schema,table,sc,geoms,key)
        geom_idx={i for i,n in enumerate(names) if n in geoms}

        if key:
            old=state_read(dst,schema,table); seen=set(); ki=names.index(key)
            with dst.transaction():
                with src.cursor(name=("arcgis_%s_%s"%(schema,table))[:60]) as rd:
                    rd.itersize=batch_size; rd.execute(sel)
                    while True:
                        batch=rd.fetchmany(batch_size)
                        if not batch: break
                        for row in batch:
                            rows+=1; k=row[ki]
                            if k is None: raise RuntimeError("Clave nula en origen, fila %s"%rows)
                            if k in seen: raise RuntimeError("Clave duplicada en origen: %r"%k)
                            seen.add(k); h=row_hash(row); prev=old.get(k)
                            if h!=prev:
                                with dst.cursor() as wr: wr.execute(ins_sql,params_for(row,sc,geoms))
                                if prev is None: ins+=1
                                else: upd+=1
                                with dst.cursor() as st:
                                    st.execute(sql.SQL("""INSERT INTO {}.{}
                                      (source_schema,source_table,key_value,row_hash,synced_at)
                                      VALUES(%s,%s,%s,%s,now())
                                      ON CONFLICT(source_schema,source_table,key_value)
                                      DO UPDATE SET row_hash=EXCLUDED.row_hash,synced_at=now()""").format(
                                      sql.Identifier(STATE_SCHEMA),sql.Identifier(STATE_TABLE)),
                                      (schema,table,Jsonb(k),h))
            src.commit(); mode="INCREMENTAL"
        else:
            # Reemplazo de contenido atómico; nunca DROP/TRUNCATE de tabla.
            with dst.transaction():
                with dst.cursor() as cur: cur.execute(sql.SQL("DELETE FROM {}").format(qname(schema,table)))
                with src.cursor(name=("arcgis_full_%s_%s"%(schema,table))[:60]) as rd:
                    rd.itersize=batch_size; rd.execute(sel)
                    while True:
                        batch=rd.fetchmany(batch_size)
                        if not batch: break
                        with dst.cursor() as wr:
                            for row in batch: wr.execute(ins_sql,params_for(row,sc,geoms))
                        rows+=len(batch); ins+=len(batch)
            src.commit(); mode="CARGA_COMPLETA_SIN_CLAVE"
        return rows,ins,upd,time.time()-started,mode
    finally:
        try: src.close()
        except Exception: pass
        try: dst.close()
        except Exception: pass


def latest_ok(path):
    ok=set()
    if not os.path.exists(path): return ok
    with open(path,"r",newline="",encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k=(r.get("esquema"),r.get("tabla"))
            if r.get("estado")=="OK": ok.add(k)
            elif r.get("estado")=="ERROR": ok.discard(k)
    return ok


def log(path,schema,table,status,rows,ins,upd,secs,msg=""):
    exists=os.path.exists(path) and os.path.getsize(path)>0
    with open(path,"a",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        if not exists:
            w.writerow(["fecha","esquema","tabla","estado","filas_origen",
                        "insertados","actualizados","eliminados","segundos","mensaje"])
        w.writerow([datetime.now().isoformat(timespec="seconds"),schema,table,status,
                    rows,ins,upd,0,round(secs,2),msg])


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--batch-size",type=int,default=BATCH)
    p.add_argument("--log",default=LOG_FILE)
    p.add_argument("--reprocesar",action="store_true")
    p.add_argument("--solo",nargs="*",default=None)
    a=p.parse_args()
    if a.batch_size<1: p.error("--batch-size debe ser positivo")
    # Crear tabla de estado sin mantener conexiones abiertas durante el ETL.
    d=connect(DEST)
    try: ensure_state(d)
    finally: d.close()
    tables=discover()
    if a.solo:
        wanted=set(a.solo); tables=[x for x in tables if x[1] in wanted]
        absent=wanted-{t for _,t in tables}
        if absent: print("No encontradas: "+", ".join(sorted(absent)))
    done=set() if a.reprocesar else latest_ok(a.log)
    pending=[t for t in tables if t not in done]
    print("Detectadas=%d | omitidas OK=%d | pendientes=%d"%(len(tables),len(tables)-len(pending),len(pending)))
    errors=0
    for i,(s,t) in enumerate(pending,1):
        print("\n[%d/%d] %s.%s"%(i,len(pending),s,t),flush=True)
        result=None; err=None
        for attempt in range(1,RETRIES+1):
            try:
                result=sync_one(s,t,a.batch_size); break
            except Exception as e:
                err=e
                print("  intento %d/%d: %s: %s"%(attempt,RETRIES,type(e).__name__,e),flush=True)
                if attempt<RETRIES: time.sleep(WAIT)
        if result:
            nr,ni,nu,secs,mode=result
            print("  OK %s | origen=%d | insertados=%d | actualizados=%d | %.1fs"%
                  (mode,nr,ni,nu,secs),flush=True)
            log(a.log,s,t,"OK",nr,ni,nu,secs,mode)
        else:
            errors+=1; msg="%s: %s"%(type(err).__name__,err)
            log(a.log,s,t,"ERROR",0,0,0,0,msg)
            print("  ERROR DEFINITIVO: "+msg,flush=True)
    print("\nFINALIZADO | procesadas=%d | errores=%d | log=%s"%
          (len(pending),errors,os.path.abspath(a.log)))
    if errors: sys.exit(1)


if __name__=="__main__": main()
