
# -*- coding: utf-8 -*-

"""
Modulo de conexiones PostgreSQL.
Origen: ArcGIS Enterprise PostgreSQL.
Destino: PostgreSQL/PostGIS.
"""

import psycopg

from configuracion import ORIGEN, DESTINO


def conectar_origen():
    """Establece conexion con la base de datos de origen."""

    config = ORIGEN.copy()

    config["connect_timeout"] = 30
    config["application_name"] = "ArcGIS_PostGIS_Sync_Origen"

    return psycopg.connect(**config)


def conectar_destino():
    """Establece conexion con la base de datos de destino."""

    config = DESTINO.copy()

    config["connect_timeout"] = 30
    config["application_name"] = "ArcGIS_PostGIS_Sync_Destino"

    return psycopg.connect(**config)


def probar_conexiones():
    """Prueba ambas conexiones y devuelve informacion basica."""

    conexiones = {
        "ORIGEN": conectar_origen,
        "DESTINO": conectar_destino
    }

    resultados = {}

    for nombre, funcion in conexiones.items():

        conexion = None

        try:
            conexion = funcion()

            with conexion.cursor() as cursor:
                cursor.execute("""
                    SELECT
                        current_database(),
                        current_user,
                        version()
                """)

                resultados[nombre] = cursor.fetchone()

        finally:
            if conexion is not None:
                conexion.close()

    return resultados
