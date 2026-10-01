"""Vuelve a leer el esquema de las bases y actualiza lo que el bot sabe de ellas.

    .venv\\Scripts\\python.exe actualizar_catalogo.py            # compara y no escribe
    .venv\\Scripts\\python.exe actualizar_catalogo.py --escribir # compara y escribe

Antes `catalogo.json` se armaba a mano, desde un documento HTML del esquema, y
el script que lo hizo nunca se guardo: cuando la base cambiaba -- una columna
nueva, una vista rota -- no habia como ponerse al dia. Este lee directo de
INFORMATION_SCHEMA de cada base, con el usuario de solo lectura del bot.

Escribe dos archivos:

- `catalogo.json`: bases, objetos, columnas con su tipo y filas de cada
  tabla. Las DESCRIPCIONES de cada base se conservan: son texto escrito a
  mano y el servidor no las sabe. Cada vista se prueba con un SELECT TOP 0:
  una vista que no resuelve (como ETL_Consolidado_B2B el 30 de septiembre de
  2026, con sus vistas de origen desparejadas) queda marcada con su error.
- `valores.json`: los valores de las columnas de baja cardinalidad que el
  bot consulta seguido (retailers, marcas, carriers...). Las mismas columnas
  que ya tenia; si la consolidada esta rota, se sacan de sus vistas.

Lo que NO hace: tocar las notas de catalogo.py. Esas son conocimiento de
negocio y se revisan como codigo; este script dice que cambio para que
alguien decida si una nota quedo vieja.
"""

import argparse
import json
import os
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
CATALOGO = AQUI / "catalogo.json"
VALORES = AQUI / "valores.json"

# Las vistas que une la consolidada, por si hay que sacar valores sin ella.
VISTAS_DE_LA_CONSOLIDADA = (
    "vw_ETL_ATT_Postpaid", "vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT",
    "vw_ETL_Elektra", "vw_ETL_Liverpool", "vw_ETL_Liverpool_IOT",
    "vw_ETL_Movistar_CAC", "vw_ETL_Sears", "vw_ETL_TELCEL_B2B",
)
MAX_VALORES = 400


def _tipo(fila) -> str:
    """'nvarchar(255)', 'varchar(max)', 'decimal(18,2)', 'int'..."""
    tipo, largo, precision, escala = fila
    if tipo in ("varchar", "nvarchar", "char", "nchar", "varbinary", "binary"):
        return f"{tipo}({'max' if largo == -1 else largo})"
    if tipo in ("decimal", "numeric"):
        return f"{tipo}({precision},{escala})"
    return tipo


def leer_base(conexion, base: str) -> list[dict]:
    """Objetos de dbo con sus columnas, filas (tablas) y si resuelven (vistas)."""
    objetos = []
    filas = conexion.exec_driver_sql(
        f"SELECT TABLE_NAME, TABLE_TYPE FROM [{base}].INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = 'dbo' ORDER BY TABLE_NAME"
    ).fetchall()
    for nombre, tipo in filas:
        # Los respaldos no son datos que consultar: son copias que alguien
        # hizo antes de tocar una tabla (bkp_LIVERPOOL_B2B_20260930 aparecio
        # el mismo dia que cambiaron las vistas de Liverpool). Dejarlos en el
        # catalogo es invitar al modelo a sumar una copia vieja.
        if nombre.lower().startswith(("bkp_", "backup_")):
            continue
        columnas = [
            {"nombre": c[0], "tipo": _tipo(c[1:])}
            for c in conexion.exec_driver_sql(
                f"SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
                f"NUMERIC_PRECISION, NUMERIC_SCALE FROM [{base}].INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                (nombre,),
            ).fetchall()
        ]
        objeto = {
            "nombre": nombre,
            "tipo": "vista" if tipo == "VIEW" else "tabla",
            "filas": None,
            "columnas": columnas,
        }
        if objeto["tipo"] == "tabla":
            objeto["filas"] = conexion.exec_driver_sql(
                f"SELECT SUM(p.rows) FROM [{base}].sys.partitions p "
                f"JOIN [{base}].sys.objects o ON o.object_id = p.object_id "
                "WHERE o.name = ? AND p.index_id IN (0, 1)",
                (nombre,),
            ).scalar()
        else:
            try:
                conexion.exec_driver_sql(f"SELECT TOP 0 * FROM [{base}].dbo.[{nombre}]")
            except Exception as error:  # noqa: BLE001
                objeto["rota"] = " ".join(str(getattr(error, "orig", error)).split())[:300]
        objetos.append(objeto)
    return objetos


def comparar(viejo: dict, nuevo: dict) -> list[str]:
    """Lo que cambio, en lineas legibles."""
    cambios = []
    bases_v = {b["nombre"]: b for b in viejo.get("bases", [])}
    for base in nuevo["bases"]:
        antes = {o["nombre"]: o for o in bases_v.get(base["nombre"], {}).get("objetos", [])}
        ahora = {o["nombre"]: o for o in base["objetos"]}
        for nombre in sorted(set(ahora) - set(antes)):
            o = ahora[nombre]
            cambios.append(f"[{base['nombre']}] NUEVO {o['tipo']} {nombre} ({len(o['columnas'])} columnas)")
        for nombre in sorted(set(antes) - set(ahora)):
            cambios.append(f"[{base['nombre']}] YA NO EXISTE {nombre}")
        for nombre in sorted(set(antes) & set(ahora)):
            col_v = {c["nombre"]: c["tipo"] for c in antes[nombre]["columnas"]}
            col_n = {c["nombre"]: c["tipo"] for c in ahora[nombre]["columnas"]}
            for c in col_n.keys() - col_v.keys():
                cambios.append(f"[{base['nombre']}] {nombre}: columna nueva [{c}] {col_n[c]}")
            for c in col_v.keys() - col_n.keys():
                cambios.append(f"[{base['nombre']}] {nombre}: ya no tiene [{c}]")
            for c in col_v.keys() & col_n.keys():
                if col_v[c] != col_n[c]:
                    cambios.append(f"[{base['nombre']}] {nombre}: [{c}] cambio de {col_v[c]} a {col_n[c]}")
            if ahora[nombre].get("rota"):
                cambios.append(f"[{base['nombre']}] {nombre}: NO RESUELVE -> {ahora[nombre]['rota'][:160]}")
    return cambios


def _como_texto(valor) -> str:
    """El valor como lo escribiria el modelo en un filtro.

    Un float entero va sin decimales: HCReport.[YEAR] es float, y '2025.0'
    en la lista de valores invita al modelo a escribir `[YEAR] = '2025.0'`.
    """
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


INTENTOS_POR_COLUMNA = 3


def _distintos(motor, sql: str) -> list:
    """Corre el DISTINCT con su propia conexion, reintentando si la red se cae.

    Cada DISTINCT sobre la consolidada recorre millones de filas y tarda
    minutos; el 1 de octubre de 2026 una de esas se corto a la mitad
    ('Communication link failure') y se llevo por delante todo lo que ya se
    habia leido. Con una conexion por consulta, un corte solo cuesta repetir
    esa consulta.
    """
    for intento in range(1, INTENTOS_POR_COLUMNA + 1):
        try:
            with motor.connect() as conexion:
                conexion.connection.dbapi_connection.timeout = 900
                return conexion.exec_driver_sql(sql).fetchall()
        except Exception as error:  # noqa: BLE001
            if intento == INTENTOS_POR_COLUMNA:
                raise
            print(f"  la conexion fallo ({str(error)[:80]}...); reintento {intento + 1}", flush=True)
    return []


def leer_valores(motor, catalogo: dict, columnas: dict, anteriores: dict | None = None) -> dict:
    """Los DISTINCT de las columnas que ya estaban en valores.json.

    Si una columna no se puede leer ni reintentando, se queda con sus valores
    anteriores y se avisa: perder la lista de retailers por un corte de red
    seria peor que tenerla de hace unos dias.
    """
    anteriores = anteriores or {}
    ubicacion = {
        o["nombre"]: (b["nombre"], o)
        for b in catalogo["bases"] for o in b["objetos"]
    }
    resultado = {}
    for objeto, cols in columnas.items():
        base, info = ubicacion.get(objeto, (None, None))
        if base is None:
            print(f"  aviso: {objeto} ya no existe; sus valores se descartan")
            continue
        origen = f"[{base}].dbo.[{objeto}]"
        if info.get("rota") and objeto == "ETL_Consolidado_B2B":
            # Rota en el servidor: los mismos valores salen de sus vistas.
            partes = " UNION ALL ".join(
                f"SELECT [Retail],[Carrier],[Brand],[Year],[Month] FROM [RAW_DATA_B2B].dbo.[{v}]"
                for v in VISTAS_DE_LA_CONSOLIDADA
            )
            origen = f"({partes}) t"
        resultado[objeto] = {}
        for columna in cols:
            print(f"  {objeto}.{columna}...", flush=True)
            try:
                filas = _distintos(
                    motor,
                    f"SELECT DISTINCT TOP {MAX_VALORES} [{columna}] FROM {origen} "
                    f"WHERE [{columna}] IS NOT NULL ORDER BY 1",
                )
                resultado[objeto][columna] = [_como_texto(f[0]) for f in filas]
            except Exception as error:  # noqa: BLE001
                previos = anteriores.get(objeto, {}).get(columna)
                print(f"  AVISO: no se pudo leer {objeto}.{columna} ({str(error)[:80]}); "
                      f"{'se conservan los valores anteriores' if previos else 'queda sin valores'}")
                if previos:
                    resultado[objeto][columna] = previos
    return resultado


def main() -> int:
    opciones = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    opciones.add_argument("--escribir", action="store_true",
                          help="escribe catalogo.json y valores.json (si no, solo compara)")
    args = opciones.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(AQUI))
    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    load_dotenv(AQUI / ".env")
    motor = create_engine(os.environ["DB_URI"])
    viejo = json.loads(CATALOGO.read_text(encoding="utf-8"))
    valores_viejos = json.loads(VALORES.read_text(encoding="utf-8")) if VALORES.exists() else {}

    nuevo = {"bases": []}
    with motor.connect() as conexion:
        conexion.connection.dbapi_connection.timeout = 900
        for base in viejo["bases"]:
            print(f"leyendo {base['nombre']}...", flush=True)
            nuevo["bases"].append({
                "nombre": base["nombre"],
                "descripcion": base.get("descripcion", ""),
                "objetos": leer_base(conexion, base["nombre"]),
            })
    print("leyendo valores...", flush=True)
    columnas = {obj: list(cols) for obj, cols in valores_viejos.items()}
    valores = leer_valores(motor, nuevo, columnas, valores_viejos)

    print()
    cambios = comparar(viejo, nuevo)
    print(f"{len(cambios)} cambios en el esquema:" if cambios else "El esquema no cambio.")
    for cambio in cambios:
        print("  - " + cambio)
    for objeto, cols in valores.items():
        for columna, lista in cols.items():
            antes = set(valores_viejos.get(objeto, {}).get(columna, []))
            nuevos, idos = sorted(set(lista) - antes), sorted(antes - set(lista))
            if nuevos or idos:
                print(f"  - valores {objeto}.{columna}: +{nuevos[:12]} -{idos[:12]}")

    if args.escribir:
        CATALOGO.write_text(json.dumps(nuevo, ensure_ascii=False, indent=1), encoding="utf-8")
        VALORES.write_text(json.dumps(valores, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nEscritos {CATALOGO.name} y {VALORES.name}.")
    else:
        print("\nNo se escribio nada (usa --escribir).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
