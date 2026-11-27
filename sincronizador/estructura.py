
# -*- coding: utf-8 -*-
"""
Comparaci�n estructural de tablas GIS entre origen y destino.
Solo lectura. No modifica las bases de datos.
"""

from sincronizador.conexiones import conectar_origen, conectar_destino


def obtener_tablas(conexion):
    consulta = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'gis'
          AND table_type = 'BASE TABLE'
          AND table_name !~ '^i[0-9]+$'
        ORDER BY table_name;
    """

    with conexion.cursor() as cursor:
        cursor.execute(consulta)
        return {fila[0] for fila in cursor.fetchall()}


def obtener_columnas(conexion, tabla):
    consulta = """
        SELECT
            column_name,
            ordinal_position,
            data_type,
            udt_name,
            character_maximum_length,
            numeric_precision,
            numeric_scale,
            is_nullable,
            is_identity,
            identity_generation
        FROM information_schema.columns
        WHERE table_schema = 'gis'
          AND table_name = %s
        ORDER BY ordinal_position;
    """

    with conexion.cursor() as cursor:
        cursor.execute(consulta, (tabla,))
        return {
            fila[0]: fila[1:]
            for fila in cursor.fetchall()
        }


def comparar_estructuras():
    origen = conectar_origen()
    destino = conectar_destino()

    try:
        tablas_origen = obtener_tablas(origen)
        tablas_destino = obtener_tablas(destino)

        solo_origen = sorted(tablas_origen - tablas_destino)
        solo_destino = sorted(tablas_destino - tablas_origen)
        comunes = sorted(tablas_origen & tablas_destino)

        diferencias = []

        for tabla in comunes:
            columnas_origen = obtener_columnas(origen, tabla)
            columnas_destino = obtener_columnas(destino, tabla)

            nombres_origen = set(columnas_origen)
            nombres_destino = set(columnas_destino)

            for columna in sorted(nombres_origen - nombres_destino):
                diferencias.append({
                    "tabla": tabla,
                    "columna": columna,
                    "tipo": "FALTA_EN_DESTINO",
                    "origen": columnas_origen[columna],
                    "destino": None
                })

            for columna in sorted(nombres_destino - nombres_origen):
                diferencias.append({
                    "tabla": tabla,
                    "columna": columna,
                    "tipo": "ADICIONAL_EN_DESTINO",
                    "origen": None,
                    "destino": columnas_destino[columna]
                })

            for columna in sorted(nombres_origen & nombres_destino):
                if columnas_origen[columna] != columnas_destino[columna]:
                    diferencias.append({
                        "tabla": tabla,
                        "columna": columna,
                        "tipo": "ESTRUCTURA_DIFERENTE",
                        "origen": columnas_origen[columna],
                        "destino": columnas_destino[columna]
                    })

        return {
            "total_origen": len(tablas_origen),
            "total_destino": len(tablas_destino),
            "total_comunes": len(comunes),
            "solo_origen": solo_origen,
            "solo_destino": solo_destino,
            "diferencias": diferencias
        }

    finally:
        origen.close()
        destino.close()
