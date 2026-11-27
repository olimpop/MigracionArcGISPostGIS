# -*- coding: utf-8 -*-
"""
Inventario de tablas entre ArcGIS PostgreSQL y PostgreSQL/PostGIS.
Solo lectura: no modifica las bases de datos.
"""

from sincronizador.conexiones import (
    conectar_origen,
    conectar_destino
)

ESQUEMAS = ("gis", "hist")


def obtener_tablas(conexion):

    consulta = """
        SELECT
            table_schema,
            table_name
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema IN (%s, %s)
        ORDER BY table_schema, table_name;
    """

    with conexion.cursor() as cursor:
        cursor.execute(consulta, ESQUEMAS)
        return set(cursor.fetchall())


def comparar_inventario():

    origen = conectar_origen()
    destino = conectar_destino()

    try:
        tablas_origen = obtener_tablas(origen)
        tablas_destino = obtener_tablas(destino)

        solo_origen = tablas_origen - tablas_destino
        solo_destino = tablas_destino - tablas_origen
        comunes = tablas_origen & tablas_destino

        return {
            "total_origen": len(tablas_origen),
            "total_destino": len(tablas_destino),
            "total_comunes": len(comunes),
            "solo_origen": sorted(solo_origen),
            "solo_destino": sorted(solo_destino),
            "comunes": sorted(comunes)
        }

    finally:
        origen.close()
        destino.close()