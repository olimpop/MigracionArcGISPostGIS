# -*- coding: utf-8 -*-

"""
Configuracion central de conexiones PostgreSQL.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env", encoding="utf-8-sig")


def obtener_configuracion(prefijo):

    variables = {
        "host": f"{prefijo}_HOST",
        "port": f"{prefijo}_PORT",
        "dbname": f"{prefijo}_DATABASE",
        "user": f"{prefijo}_USER",
        "password": f"{prefijo}_PASSWORD",
    }

    faltantes = [
        variable
        for variable in variables.values()
        if not os.getenv(variable)
    ]

    if faltantes:
        raise RuntimeError(
            f"Faltan variables de entorno para {prefijo}: "
            + ", ".join(faltantes)
        )

    return {
        "host": os.environ[variables["host"]],
        "port": int(os.environ[variables["port"]]),
        "dbname": os.environ[variables["dbname"]],
        "user": os.environ[variables["user"]],
        "password": os.environ[variables["password"]],
    }


ORIGEN = obtener_configuracion("PG_ORIGEN")
DESTINO = obtener_configuracion("PG_DESTINO")
