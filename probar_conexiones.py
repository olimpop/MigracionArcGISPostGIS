# -*- coding: utf-8 -*-

"""
Prueba de conexiones PostgreSQL.
Utiliza las credenciales definidas en el archivo .env.
"""

import psycopg

from configuracion import ORIGEN, DESTINO


def probar(nombre, configuracion):

    conexion_config = configuracion.copy()
    conexion_config["connect_timeout"] = 10
    conexion_config["application_name"] = "ArcGIS_PostGIS_Connection_Test"

    try:
        with psycopg.connect(**conexion_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        current_database(),
                        current_user,
                        version()
                """)

                base_datos, usuario, version = cur.fetchone()

                print(f"\n{nombre}: CONEXION EXITOSA")
                print(f"Base de datos: {base_datos}")
                print(f"Usuario: {usuario}")
                print(f"Version: {version.splitlines()[0]}")

        return True

    except Exception as error:
        print(f"\n{nombre}: ERROR DE CONEXION")
        print(f"Tipo de error: {type(error).__name__}")
        return False


def main():

    resultado_origen = probar("ORIGEN", ORIGEN)
    resultado_destino = probar("DESTINO", DESTINO)

    if resultado_origen and resultado_destino:
        print("\nTodas las conexiones funcionan correctamente.")
    else:
        print("\nHay conexiones pendientes de revisar.")


if __name__ == "__main__":
    main()
