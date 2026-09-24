# Cómo se consultan las tablas

Fecha: 2026-09-21, ampliado 2026-09-23 (Raw_Data y PIVOT)
Estado: revisado contra la base, cada número de aquí está medido

Este documento es el mapa que el bot ya lleva dentro (`sql_agent/catalogo.py`),
escrito para leerse de un vistazo. Salvo donde se diga lo contrario, cada cifra
sale de correr esa consulta contra `DESKTOP-GOE584A` el día de la fecha, sobre
**agosto de 2026**; ninguna es una estimación.

## El mapa

```
VENTA                                    CATÁLOGOS                 PROMOTORÍA
─────                                    ─────────                 ──────────

[RAW_DATA_B2B]                           [GTM BI Database]         [GTM BI Database]

 9 tablas crudas          ──(A)──►  UNICOS$                         HCReport
 COPPEL_B2B  [Modelo]               (homologación de modelo)        1 fila por
 EKT_B2B     [Desc Sku]             [DESCRIPCION]         ◄─(A)     tienda y SEMANA
 TELCEL_B2B  [Descripción]          [MATERIAL DESCRIPTION           [ID] [Week] [YEAR]
 LIVERPOOL_B2B$ [Material            FINAL]               ◄─(B)         │
              Description]          [HOMOLOGACION MODELO]              │ [ID]
 ATT_B2B / MVS_B2B /                                                   ▼
 SEARS_B2B   [Model]                                            CustomerStores
     │ insumo                                                   [ID] [Coppel Name]
     ▼                                                          [Region] [RetailName]
 9 vistas vw_ETL_*        ──(B)──►                                     ▲
 (una por retailer)                                                    │ por NOMBRE
     │ unión                                                           │ (parcial)
     ▼                            ─────────────────────────────────────┘
 ETL_Consolidado_B2B
 [Retail][Store][Brand]                                   V_HCReport_Informes
 [Material_Description]                                   = HCReport ⋈ CustomerStores,
 [Carrier][Date][Year]                                      una semana por mes
 [Month][Day][QTY]                                        [Coppel Name][Region]
                                                          [status_of_promoter][Period]
```

```
PRECIOS  [Scrap]

 Complete_Scrap  (2.3 M filas, 2025 → hoy, 8 retailers)
 [model] = codigo del sitio (BVL-N49)  ← NUNCA se agrupa por aqui
 [approved_name] = HONOR MAGIC 6 PRO 512GB  ← la homologacion de esta base
        │
        └── mismo vocabulario que UNICOS$.[HOMOLOGACION MODELO]
            (156 de los 183 modelos que Elektra vendio en agosto 2026)
```

## Las tres rutas de homologación

El modelo de teléfono se agrupa SIEMPRE por `UNICOS$.[HOMOLOGACION MODELO]`;
cada cadena escribe el mismo aparato a su manera. Hay dos formas de llegar ahí
y una que parece funcionar y no funciona.

| | Desde | Con | Casa | Veredicto |
|---|---|---|---|---|
| **A** | tabla **cruda** del retailer | `u.[DESCRIPCION]` + `u.[Retail]` | 100% Elektra, Telcel, Liverpool y Sears; **97.8%** Coppel; **89.0%** AT&T | la ruta por defecto |
| **B** | `vw_ETL_COPPEL` / `_IOT` | `u.[MATERIAL DESCRIPTION FINAL]` | **100.0%** (483 284 de 483 430 unidades, Coppel agosto 2026) | válida solo para esas dos vistas |
| **C** | cualquier `vw_ETL_*` | `u.[DESCRIPCION]` | **4.1%** (19 867 de 483 430) | **prohibida**: devuelve un top que parece razonable y es falso |

La ruta C también se cuela por la puerta de atrás: `ETL_Consolidado_B2B` es la
unión de las nueve vistas, así que unirla con `[DESCRIPCION]` es el mismo error.
Medido en agosto de 2026, casa **165 941 de 2 030 760 unidades, el 8.2%**. El
motivo es que ahí `[Material_Description]` es SKU + retailer + descripción:

```
N.I15128N AT&T Cacs APPLE IPHONE 15 128GB 4G   <- consolidada
```

Y hay un segundo daño, menos visible: `UNICOS$` tiene 184 546 filas pero solo
27 006 descripciones distintas (155 170 filas la traen vacía), y **1 249
descripciones están repetidas**, algunas apuntando a 25 homologaciones
distintas. Sin `u.[Retail]` en el `ON`, una misma venta se multiplica y puede
quedar contada bajo el modelo equivocado. Por eso la regla A lleva el retailer
pegado a la condición, no en el `WHERE`.

## El nombre del modelo

El modelo **nunca** se agrupa por el texto que escribe el retailer: cada cadena
escribe el mismo aparato a su manera y agrupar por ahi parte un teléfono en
varias filas, sin que nada falle. Cada base tiene su columna homologada:

| Mundo | Columna | Ejemplo |
|---|---|---|
| venta | `UNICOS$.[HOMOLOGACION MODELO]` | `APPLE IPHONE 15 128GB 4G` |
| precios | `Complete_Scrap.[approved_name]` | `HONOR MAGIC 6 PRO 512GB` |

Las dos hablan el mismo idioma, así que son la llave entre precio y venta: de
los **183** modelos que Elektra vendió en agosto de 2026, **156** tienen precio
en Scrap. Quitar el sufijo de red (`4G`/`5G`) solo recupera 2 más, así que la
diferencia no es de formato: son modelos que nadie publicó online.

Y cuidado con las columnas que dicen "homologado" y no lo son:
`EKT_B2B.[Modelo Homologado]` es la homologación **interna de Elektra**
(`Oppo A6`, `Samsung Galaxy A37`) y contra la oficial casa el **2.5%** del
volumen (4 250 de 167 845 unidades). En Scrap, `[model]` es el código del sitio
del retailer (`BVL-N49`, `CLK-NX3`): hay 1 045 códigos para 3 561 nombres
aprobados, o sea que el código ni siquiera identifica el producto.

Scrap trae además dos retailers que **no existen en las tablas de venta**:
Suburbia y Mercado Libre. Y dos trampas: `[price]` trae ceros (hay que filtrar
`> 0`) y hay varias filas del mismo modelo, tienda y fecha.

### Cuál de las dos columnas de `UNICOS$`, y cómo se arma la llave

`[MATERIAL DESCRIPTION FINAL]` **es una concatenación**, y ahí está la trampa:
comparada contra la descripción pelada de la venta no casa con nada, pero
armando la misma concatenación del lado de la venta casa mejor que
`[DESCRIPCION]`. Cada cadena la arma a su manera:

| Cadena | Llave del lado de la venta | Columna de `UNICOS$` | Casa |
|---|---|---|---|
| Coppel | `CONCAT([Marca], ' ', [Modelo])` | `MATERIAL DESCRIPTION FINAL` | **100.0%** (97.8% por `[DESCRIPCION]`) |
| Sears | `CONCAT('Sears ', [Model])` | `MATERIAL DESCRIPTION FINAL` | 100.0% (igual por `[DESCRIPCION]`) |
| Elektra | `[Desc Sku]` | `DESCRIPCION` | 100.0% |
| Telcel | `[Descripción]` | `DESCRIPCION` | 100.0% |
| Liverpool | `[Material Description]` | `DESCRIPCION` | 100.0% |
| AT&T | `[Model]` | `DESCRIPCION` | 89.0% — ahí la final casa **0%** |

### Por qué no la final en todas

Porque `[MATERIAL DESCRIPTION FINAL]` no es un formato: es **prefijo +
`[DESCRIPCION]`**, y el prefijo cambia por cadena. Filas del catálogo donde las
dos columnas son idénticas:

| Cadena | Iguales | El prefijo es |
|---|---|---|
| Telcel | 8 947 de 8 947 | nada — son la misma columna |
| Elektra | 3 364 de 3 393 | nada, salvo 29 filas |
| Liverpool | 6 227 de 12 489 | el código de artículo (`1200272087 ABS Nebula…`) |
| Coppel | 11 de 3 076 | la **marca** (`ACER ANV15-41-R4WJ`) |
| Sears | 0 de 455 | la palabra **Sears** (`Sears APPLE IPHONE 11 64GB`) |
| AT&T | 11 de 454 | el **nombre del retail** (`AT&T CACS APPLE IPHONE 17…`) |

De ahí la regla: donde el prefijo existe y se puede reconstruir desde la venta
(Coppel, Sears), la final **es** la llave y casa mejor. Donde las dos columnas
son casi la misma (Telcel, Elektra), da igual cuál se use. En Liverpool
`[DESCRIPCION]` además cubre la mitad del catálogo que lleva código delante.

AT&T es el caso que no compensa: el prefijo es el nombre del retail, y la tabla
cruda mezcla CACS y DEALERS sin una columna que diga cuál es, así que habría que
adivinarlo. Medido: con `'AT&T CACS '` casa 89.3%, con `'AT&T DEALERS '` 86.2%,
y por `[DESCRIPCION]` 89.0%. Se queda con la descripción.

La final es también la columna que casa al 100% contra `vw_ETL_COPPEL` y
`vw_ETL_COPPEL_IOT`, que arman su `[Material_Description]` como
`CONCAT(marca, modelo)` — la misma idea del otro lado.

Por `[MATERIAL DESCRIPTION FINAL]` no hay ningún texto ambiguo en Coppel ni en
Sears (0 con dos homologaciones); por `[DESCRIPCION]` sí hay 137 en Liverpool y
6 en Coppel, y por eso la llave se colapsa con `MIN` antes de unir.

## La tienda

No hay una llave de tienda entre venta y catálogo: la venta trae `[Store]`, que
es texto, y el catálogo trae `[ID]`. Cruzarlos por nombre funciona en unos
retailers y en otros no. Tiendas distintas de 2026 contra `[Coppel Name]`:

| Retailer | Casan | |
|---|---|---|
| AT&T | 1732 de 1732 | 100% |
| Coppel | 1792 de 1800 | 99.6% |
| Liverpool | 193 de 204 | 95% |
| Elektra | 1253 de 2307 | 54% |
| Telcel | 0 de 675 | no hay cruce |
| Sears | 0 de 91 | no hay cruce |

Y el nombre tampoco es estable dentro de la propia venta. En Elektra el número
9797 aparece como `ELEKTRA.MX`, `ELEKTRA.COM` y `ELETRA.MX`; el **73% del
volumen de 2026** (927 913 de 1 268 566 unidades) está en números de tienda con
más de un nombre. Agrupar por `[Store]` parte una tienda en varias: la 6169 da
670 unidades como `EKT MEGA ZUMPANGO` y 908 como `MEGA ZUMPANGO`. Para CAPA o
para cualquier ranking por tienda de Elektra, el corte va por el número:
`LEFT([Store], CHARINDEX(' ', [Store]+' ')-1)`. En Coppel, AT&T, Movistar y
Sears el nombre es estable (0.2% o menos).

## Promotoría

`HCReport` es semanal y se une a `CustomerStores` **por `[ID]`**, nunca por
nombre: su `[NAME]` es el nombre del operador y no coincide con el del catálogo.

`V_HCReport_Informes` es la vista mensual, y su definición dice cómo lo hace el
negocio: toma **una sola semana por mes**, la última completa, con el mapa
escrito a mano dentro de la vista.

| Mes | 2025 | 2026 |
|---|---|---|
| ene / feb / mar / abr | W04 W08 W13 W17 | W05 W09 W13 W17 |
| may / jun / jul / ago | W22 W26 W30 W35 | W22 W26 W30 W35 |
| sep / oct / nov / dic | W39 W43 W48 W52 | W39 W44 W48 W52 |

Dos límites que la vista no anuncia:

1. Es un `INNER JOIN` con `CustomerStores` y se queda con la mitad: de las
   **8 326** tiendas de `HCReport` en W35 de 2026, solo **4 529** están en el
   catálogo. Sirve para cortes por tienda o región; no para un total de
   promotores del país.
2. El mapa cubre **solo 2025 y 2026**. De 2027 en adelante `[Period]` sale
   `NULL`. Hay que actualizar la vista, no calcular la semana por fuera.

`[Period]` es texto con el mes en inglés (`'2026-August'`), no una fecha.

## Lo que se arregló en esta revisión

La hoja "Ventas por tienda" del Excel (`sql_agent/contexto.py`) filtraba los
modelos del reporte uniendo la consolidada con `UNICOS$` por `[DESCRIPCION]`:
la ruta C, la prohibida. Perdía el 92% del volumen y duplicaba parte del resto.
Ahora la hoja va por **periodo**, sin pasar por la homologación: unidades por
retailer y tienda en el rango que cubre el reporte. Es correcta, y además más
rápida (16 s un mes, 39 s medio año, contra 23 s de una consulta equivocada).

## La hoja por tienda del Excel

La hoja "Ventas por tienda" lleva el **modelo** cuando el reporte es de **una
sola cadena**, y entonces sale por la ruta A: la tabla cruda de esa cadena
unida a `UNICOS$` por su columna, con el retailer pegado al `ON`. Medido, las
seis consultan en 3 segundos o menos.

| Cadena | Tabla cruda | Modelo | Tienda (clave / etiqueta) | Fecha | Unidades |
|---|---|---|---|---|---|
| Coppel | `COPPEL_B2B` | `[Modelo]` | `[Tienda]` | `[Día]` | `[Unidades]` |
| Elektra | `EKT_B2B` | `[Desc Sku]` | `[Tienda #]` / `[POS]` | `[Day]` | `[QTY]` |
| Telcel | `TELCEL_B2B` | `[Descripción]` | `[Venta]` | `[Fecha]` | `[Cantidad]` |
| Liverpool | `LIVERPOOL_B2B$` | `[Material Description]` | `[Centro]` / `[STORE]` | `[Día/Periodo]` | `[Vtas# U Per 1]` |
| AT&T | `ATT_B2B` | `[Model]` | `[Codigo de Organización]` / `[Nombre de la Organización]` | `[Fecha de Transacción]` | `[Cantidad]` |
| Sears | `SEARS_B2B` | `[Model]` | `[#POS]` / `[POS]` | `[Fecha]` | `[Total]` |

Y lleva la **región** de cada tienda, que no está en la venta: sale de
`CustomerStores`, y el enlace también es distinto por cadena. Tiendas de agosto
de 2026 que llegan a una región:

| Cadena | Se une | Casan |
|---|---|---|
| Coppel | `[Tienda]` → `[Coppel Name]` | 1728 / 1735 (99.6%) |
| Telcel | `[Venta]` → `[Coppel Name]` | 649 / 653 (99.4%) |
| AT&T | `[Nombre de la Organización]` → `[Coppel Name]` | 1674 / 1682 (99.5%) |
| Liverpool | `[STORE]` → `[Coppel Name]` | 189 / 200 (94.5%) |
| Elektra | `[Tienda #]` → número dentro de `[HONOR NAME]` | 1258 / 1269 (99.1%) |
| Sears | `[#POS]` → número dentro de `[HONOR NAME]` | 90 / 91 (98.9%) |

Elektra y Sears **no casan por nombre**: sus tiendas se llaman distinto en la
venta y en el catálogo. El número está escondido entre paréntesis al final de
`[HONOR NAME]` (`ELEKTRA MERIDA 2 (8649)`, `SEARS SATELITE (104)`). Y esto es
otra razón para ir por la tabla cruda: desde la consolidada, Telcel y Sears no
casan **nunca** y Elektra casa la mitad.

Tres detalles más que se descubrieron midiendo:

- **Se agrupa por la clave y se muestra la etiqueta.** El número de tienda es
  estable; el nombre no. En Elektra la 6169 es `EKT MEGA ZUMPANGO` y también
  `MEGA ZUMPANGO`: agrupar por el nombre la parte en dos.
- **La homologación se colapsa antes de unir** (`GROUP BY [DESCRIPCION]` +
  `MIN([HOMOLOGACION MODELO])`). En Liverpool hay **137** descripciones que
  apuntan a más de una homologación, 6 en Coppel y 1 entre los dos AT&T; sin
  colapsarlas, esas ventas se unirían dos veces y las unidades saldrían
  infladas.
- **AT&T necesita sus dos `[Retail]`.** Con `AT&T DEALERS` solo casa el 80.7%
  del mes; con `DEALERS` y `CACS` juntos, el 88.9%.

Cuando el reporte **no** es de una sola cadena — porque compara varias, o
porque no hay columna de modelo, o porque es de Movistar, cuya tabla no tiene
columna de tienda — la hoja sale por **periodo**: unidades por retailer y
tienda en el rango del reporte, sin pasar por la homologación. Siempre
correcta, aunque sin modelo.

## Las hojas del archivo

| Hoja | Qué lleva |
|---|---|
| `Datos` | el resultado completo, en orden cronológico |
| una por cadena | las filas de esa cadena, cuando el reporte trae varias |
| `Ventas por tienda` | región, tienda, modelo y unidades (una sola cadena) o retailer, tienda y unidades del periodo (varias) |
| `Raw_Data` | tienda, marca, mes y unidades de TODA la cadena en el periodo, sin acotar a los modelos del resultado |
| `PIVOT` | un PivotTable de Excel de verdad sobre `Raw_Data`: tienda › marca en filas, mes en columnas, suma y share % |
| `Tiendas`, `Regiones`, `Carriers` | la ficha de los valores que salieron |

### `Raw_Data` y `PIVOT`

Se generan solo cuando el reporte es de **una sola cadena** (la misma condición
que activa la ruta A de la homologación): sin una tabla cruda identificada no
hay de dónde sacar el detalle. `Raw_Data` sale de esa tabla cruda —no de la
vista consolidada— agrupada por tienda, marca y mes, **sin** acotar a los
modelos del resultado: "las ventas de Elektra de junio a agosto" no viene con
una lista corta de modelos que desglosar, así que esta hoja trae la cadena
entera. Se agrupa por marca y no por modelo porque a nivel modelo la misma
consulta da cuatro veces más filas (127 602 contra 28 887 en Elektra, medido);
el detalle por modelo ya lo cubre `Ventas por tienda`. Tope medido: Elektra
28 887 filas, Coppel (la tabla cruda más grande) 58 066 — el límite de
`MAX_FILAS_RAW_DATA` (80 000) deja margen sin acercarse a los 16 MB del
adjunto de WhatsApp.

`PIVOT` es un PivotTable **real** de Excel —arrastrable, refrescable, el mismo
objeto que arma alguien desde el menú Insertar—, no una tabla que solo se le
parece. openpyxl no sabe escribir pivotes: leerlos sí, pero fabricar el cache
y sus registros a mano en XML no tiene ejemplos documentados y no hay forma de
probar que Excel lo abre sin pedir reparación. La única vía confiable es dejar
que Excel mismo lo construya, así que `sql_agent/excel_pivot.py` abre Excel
por COM (`pywin32`) sobre el archivo ya guardado y arma ahí el pivot:

- Corre en un hilo aparte con plazo (90 s): una llamada COM colgada no puede
  bloquear el turno de otra pregunta.
- Un candado (`threading.Lock`) asegura que solo hay un Excel COM a la vez;
  dos reportes armando un pivot en paralelo abrirían dos procesos por
  separado, más lento y más frágil.
- Cada hilo que usa COM necesita su propio `pythoncom.CoInitialize()` — un
  apartamento COM no se comparte entre hilos, y sin esto revienta con
  "CoInitialize has not been called". Apareció solo la segunda vez que se
  probó: la primera, construir el envoltorio de la librería de Excel
  disparaba un CoInitialize de casualidad; en cuanto ese cache ya existía en
  disco, la ruta rápida se lo saltaba.
- Un workbook que se abre y no se cierra deja el proceso `EXCEL.EXE` vivo
  después de `Quit()`, invisible porque `Visible=False` le impide mostrar el
  diálogo que lo estaría reteniendo. Se cierran todos los libros abiertos
  antes de salir, y si el proceso sigue vivo un segundo después, se termina
  por PID como último recurso. Verificado con una prueba que compara los
  `EXCEL.EXE` vivos antes y después.

Si Excel no está disponible, la llamada COM falla, o el trabajo no termina a
tiempo, el reporte sale igual sin la hoja `PIVOT` — `Raw_Data` y el resto del
archivo ya están guardados antes de intentar el pivot.

Las hojas por cadena se agregan **sin quitar** `Datos`, y van ordenadas por
volumen para que las primeras pestañas sean las que pesan. No se separa
siempre: hacen falta entre 2 y 16 cadenas y al menos 10 filas por cadena en
promedio. Un reporte de 48 filas y 16 cadenas son tres filas por pestaña —
ahí separar no ayuda a comparar, estorba.

El nombre de la hoja es el de la cadena, limpiado para Excel: sin
`: \ / ? * [ ]`, cortado a 31 caracteres y sin repetirse.

## Lo que sigue sin resolverse

- **Telcel y Sears no cruzan con `CustomerStores` por nombre.** Mientras siga
  así, no hay forma de poner región ni promotoría junto a sus ventas.
- **El número de tienda de Elektra es una convención, no una columna.** Se
  extrae del texto; si algún día una tienda no empieza con su número, ese corte
  falla en silencio.
- **`UNICOS$` tiene 155 170 filas con `[DESCRIPCION]` vacía** (el 84%). No
  estorban al cruce, pero el catálogo pesa seis veces lo que usa.
