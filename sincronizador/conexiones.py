# -*- coding: utf-8 -*-

"""
Compatibilidad del paquete sincronizador con el modulo central
de conexiones PostgreSQL.
"""

from conexiones import (
    conectar_origen,
    conectar_destino,
    probar_conexiones,
)

__all__ = [
    "conectar_origen",
    "conectar_destino",
    "probar_conexiones",
]
