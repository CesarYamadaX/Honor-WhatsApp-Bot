# Cómo se consultan las tablas

Fecha: 2026-09-21
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
| **A** | tabla **cruda** del retailer | `u.[DESCRIPCION]` + `u.[Retail]` | **100.0%** Elektra (167 845 de 167 845), **97.8%** Coppel (472 876 de 483 430) | la ruta por defecto |
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

Tres detalles que se descubrieron midiendo:

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

## Lo que sigue sin resolverse

- **Telcel y Sears no cruzan con `CustomerStores` por nombre.** Mientras siga
  así, no hay forma de poner región ni promotoría junto a sus ventas.
- **El número de tienda de Elektra es una convención, no una columna.** Se
  extrae del texto; si algún día una tienda no empieza con su número, ese corte
  falla en silencio.
- **`UNICOS$` tiene 155 170 filas con `[DESCRIPCION]` vacía** (el 84%). No
  estorban al cruce, pero el catálogo pesa seis veces lo que usa.
