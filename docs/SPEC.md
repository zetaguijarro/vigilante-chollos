# SPEC: qué cambia en el vigilante tras el análisis de Instinct

Versión 2.2 · 16/11/2026

Une dos cosas: el análisis del bot de Instinct (chat, base de datos, 159 casos y 5.627 hilos de Chollometro) y lo que este repositorio ya hace. **No sustituye a `docs/ESTADO.md`.** Si algo de aquí choca con "Decisiones de Zeta", manda `ESTADO.md` hasta que Zeta decida (apartado 4).

Nada de lo propuesto en el apartado 5 está hecho ni probado. Cada cambio lleva su prueba en `tests/`.

## 1. Conclusión

El vigilante ya resuelve casi todo lo que hundía a Instinct: lee por código, decide sin IA, no acepta el "antes" de la tienda como prueba, respeta robots.txt y tiene copia local de Chollometro. El reglamento que se escribió para Instinct **no se copia aquí**: sería un paso atrás.

Lo que sí aporta el análisis:

1. **Datos nuevos de qué triunfa** (apartado 3), más finos que el recuento de hilos calientes: tasa de éxito por tienda, categoría, marca y descuento.
2. **Tres huecos reales del vigilante**: no sabe qué pasa con lo que Zeta publica, Amazon sigue casi sin leerse, y no aprovecha las recopilaciones.
3. **Cinco decisiones de Zeta que los datos ponen en duda** (apartado 4).

## 2. Lo que ya está hecho aquí

| Idea del análisis | Dónde está en el código |
|---|---|
| Leer con programas, IA solo para juzgar y redactar | `ronda`, `ronda_grupo`; Hermes solo en `dossier` y `entregar` |
| El PVP tachado no es prueba | `exigir_precio_mercado`, `descuento_real` |
| Prueba por historial | `historico_comparador` (PCComTracker), `historial_propio_ok` (14 días) |
| Prueba por otras tiendas | `precioreal_red`, `mismo_modelo`, `min_bajo_mercado` |
| Prueba por antecedente en Chollometro | `demanda_probada_ok`, `mismo_producto_hot` |
| Duplicados sin abrir la búsqueda | tabla `cm`, `estado_chollometro`, `ya_publicado_igual_o_mas_barato` |
| Tiendas elegidas por lo que publican | `tiendas_con_demanda`, `filtrar_por_demanda` |
| Caché de comparadores | tabla `idealo`, `CACHE_IDEALO_H` |
| Precio absurdo = error de lectura | `precio_sospechoso` |
| Formato español de precios | `a_numero`, `RE_PRECIO_PARTIDO` |
| Bloqueo de tienda con salida y plazo | `registrar_fallos`, `no_se_lee:` 24 h |
| Tope de avisos | `freno_telegram` |
| Horario y silencio | `horario` en `tiendas.json` |
| Sin comprar, sin proxies, sin saltar antibot | reglas del README |

## 3. Qué triunfa en Chollometro

Fuente: 5.627 hilos publicados del 10 al 14/11/2026, seguidos desde Nuevos. Éxito = llegar a 200°. Llegaron 1.838 (33%). Ojo: el vigilante usa 300° (`CM_HOT`) y 120 días; estas cifras no son comparables una a una con las de `ESTADO.md`.

### Por tienda (éxitos / hilos, tasa)

| Tienda | Éxitos / hilos | Tasa | Estado en el vigilante |
|---|---|---|---|
| Amazon | 806 / 2.987 | 27% | Solo /deals, 30 productos |
| MediaMarkt | 291 / 705 | 41% | 13 productos; faltan listados |
| AliExpress | 129 / 275 | 47% | Portada, 0 útiles |
| Miravia | 53 / 103 | 51% | Página de L'Oréal, 12 |
| Carrefour | 31 / 82 | 38% | 403/503 |
| Privé by Zalando | 29 / 78 | 37% | Necesita `login` |
| El Corte Inglés | 22 / 53 | 42% | 3 productos |
| PcComponentes | 22 / 92 | 24% | Por PCComTracker |
| Showroomprivé | 20 / 54 | 37% | Necesita `login` |
| Lidl | 17 / 43 | 40% | Con navegador |
| adidas | 16 / 20 | 80% | Con navegador |
| Privalia | 16 / 42 | 38% | Necesita `login` |
| AEG | 11 / 20 | 55% | Sin revisar |
| Foot Locker | 10 / 16 | 62% | Con navegador |
| Private Sport Shop | 10 / 20 | 50% | No está |
| Coolmod | 10 / 24 | 42% | Bien, 25/25 |
| Decathlon | 9 / 34 | 26% | Antibot, apartada |
| NBA Store | 8 / 13 | 62% | No está |
| SharkNinja | 8 / 22 | 36% | Pedida, falta |
| Toys "R" Us | 8 / 25 | 32% | Sin revisar |
| Juegos digitales (Steam, Humble, Epic, eShop, PS Store, Xbox) | 20 / 32 | 63% | Xbox excluido |

Las cuatro primeras son el 70% de los éxitos y son justo las que peor lee el vigilante. Peores tasas: Veepee 11%, Deporte-Outlet 19%, Action 20%, Zalando 22%, Primor 9%.

### Por categoría

Videojuegos y consolas 54% · calzado 47% · informática 41% · móviles 41% · viajes 41% · juguetes y LEGO 34% · TV y audio 31% · moda 30% · alimentación 29% · electrodomésticos 23% · droguería 22% · hogar 19%.

### Otros resultados

- **Descuento impreso:** por debajo del 20%, solo el 15% llega a 200°. Entre 20% y 60% apenas cambia. Por encima del 60% sube al 43%.
- **El precio no importa:** de 0 a 5 EUR acierta el 36%; de 10 a 20 EUR, el 30%. Coincide con "sin tope de precio".
- **Recopilaciones:** 70% llega a 200° y 25% pasa de 500° (40 casos).
- **Primera hora:** con más de 150° a la hora, el 87% llega a 200°.
- **Tallas en el título:** 43% frente a 32%.
- **Marcas que tiran:** Xbox 74%, Switch 2 66%, Nintendo 62%, New Balance 62%, ASUS 62%, LEGO 52%, adidas 51%.
- **Marcas que no:** JBL 0 de 16, Tommy Hilfiger 7%, Calvin Klein 12%, Cecotec 13%, Lacoste 14%, Roborock y Dreame 17%.

### Qué rechaza el soporte

13 de 15 rechazos en 10 días fueron por "la gráfica muestra el precio habitual". Miran PrecioReal, su extensión, el buscador de Chollometro, Idealo, Google Shopping y gg.deals. Los otros dos: tienda sin garantías y viaje sin fechas en el título.

### Límites

Cuatro días y medio con el 11.11 en medio (infla AliExpress, Miravia y MediaMarkt). Un tercio de los hilos sin categoría. Tiendas con menos de 30 hilos: orientativas. El descuento es el impreso, no una bajada comprobada.

## 4. Conflictos: decide Zeta

| # | Lo que hay en el repo | Lo que dicen los datos o Zeta después | Recomendación |
|---|---|---|---|
| 1 | Xbox excluido (`excluir`) | Xbox es la marca con mejor tasa: 74% | Quitar la exclusión |
| 2 | `solo_tiendas_con_demanda`: ~117 tiendas no se leen | 15/11: "no quiero que se queden tiendas paradas" | Leerlas cada 3 h y solo ofertas, en vez de nunca. Más peticiones, cero tokens |
| 3 | Verificación a mano: botón "Verificar #N" | 15/11: "que no me pregunte si sacar una oferta" | Mantener el botón. Verificar solo lo que pase con prueba fuerte cuesta tokens de Haiku por cada candidato |
| 4 | Etiquetas BUENA, MUY BUENA, OJO OJITO, EXCEPCIONAL | Con Instinct usaba SUBIENDO, DESTACADOS, EXCEPCIONAL | **Resuelto el 16/11:** Zeta eligió las de Instinct con el Modo Clásico; están en `SKILL.md` |
| 5 | Caliente = 300° (`CM_HOT`) | Chollometro marca popular hacia 200° | Dejar 300° para "ya fue chollo" y medir resultados propios con 200° |
| 6 | Viajes apagados | Viajes 41%; Skyscanner y BuscoUnChollo 59% | Dejarlos apagados: 11 avisos, todos dudosos, y el soporte exige fechas |
| 7 | Moda sin mercado: 70% de descuento | En Chollometro, moda con 30 a 40% acierta igual que con 40 a 50% | Dejar el 70%: el dato es del descuento impreso, y aquí ya se vio que el "antes" está inflado |
| 8 | Histórico propio de 14 días | Propuesta: bajada de 14 días como máximo y precio anterior de 7 días | No aplicable todavía: `vistos` guarda mínimo y `desde`, no cuándo cambió el precio (ver 5.4) |

Sin conflicto, gana el repo porque es más estricto o está mejor medido: horario hasta las 00:45, formato de 5 mensajes, no abrir la búsqueda de Chollometro, no tocar una página con antibot.

## 5. Cambios propuestos, por orden de rendimiento

Ninguno se hace sin autorización. Todos son código y no gastan tokens, salvo que se diga.

### 5.1 Saber qué pasa con lo publicado

Hoy `titulos_perfil` solo guarda títulos para evitar repetidos. Falta el resultado.

- Tabla nueva `publicadas`: id del aviso (`lista.id`), id del hilo, hora, grados a la hora y a las 24 horas, caducado.
- Emparejar los hilos del perfil con los avisos por tienda y `mismo_post`.
- Los grados salen de la tabla `cm`, que ya se actualiza en cada ronda rápida.
- `informe`: por tienda y categoría, enviados, verificados, publicados y con 200° o más.

Es lo primero porque sin esto no se puede evaluar nada de lo demás. Estaba en "pendiente cuando haya datos".

### 5.2 Demanda por tasa, no solo por recuento

`tiendas_con_demanda` cuenta hilos calientes. La tabla `cm` ya tiene también los que no se calentaron, así que puede dar la tasa.

- Devolver por tienda: publicados, calientes y tasa.
- Orden de candidatos (`score`): subir tienda y categoría con buena tasa, bajar las malas.
- Marcas con tasa mala medida: bajan en el orden, no se excluyen.
- Se recalcula solo, como ahora. No hay lista fija de tiendas que mantener.

### 5.3 Cobertura de las cuatro grandes

Es donde está el 70% de los éxitos. Ya figuraba como "lo que más rinde".

- **Amazon:** fuente nueva en `radar` con las bajadas de CamelCamelCamel para Amazon España. **Antes hay que mirar su robots.txt con `permitido`**; no se ha podido comprobar desde fuera. Si lo prohíbe, queda la API de Keepa, que es de pago y con precio por confirmar. Prioridad dentro de Amazon: videojuegos, calzado, informática y audio.
- **MediaMarkt:** listados de categoría en `urls_catalogo.json`, ya que las campañas cargan casi vacías.
- **AliExpress y Miravia:** buscar con `descubrir` la página de ofertas con cupón; la portada no sirve.
- **Ventas privadas:** `login` de Zeta en Privé, Showroomprivé y Privalia. Veepee no merece el esfuerzo: 11%.

### 5.4 Cambios de precio con fecha

Para poder decir "este precio empezó hace 3 días y el anterior duró un mes", que es lo que mira el soporte.

- Tabla `cambios` (clave, fecha, precio): una fila solo cuando el precio cambia.
- `historial_propio_ok` puede entonces exigir que el precio anterior durase al menos 7 días.
- No cambia la decisión de Zeta de los 14 días de seguimiento.

### 5.5 Recopilaciones

- Si en una ronda pasan 5 o más candidatos de la misma tienda y categoría, enviar además un aviso agrupado.
- Respeta `max_por_tienda_y_ronda` para los avisos sueltos.
- Chollometro solo admite recopilaciones de productos relacionados por tipo, promoción o categoría.

### 5.6 Reglas de Chollometro en el dossier

- Tallas: `variantes_de` ya las lee. Falta la regla: ropa con menos de 2 tallas con stock o calzado con menos de 3 deja `validada` en falso.
- `SKILL.md`: en ropa y calzado, las tallas van en el título.
- Duplicados: el hilo de la misma tienda con menos de un mes solo se supera con más de un 5% y más de 1 EUR de bajada. Comprobar que `ya_publicado_igual_o_mas_barato` lo aplica así.
- Enlace limpio: quitar parámetros de afiliado y de seguimiento antes de entregar.

### 5.7 Partir `src/vigilante.py`

Sigue en pie, después de 5.1 y 5.2, por fases y ajustando las sustituciones de las pruebas.

## 6. Lo que se descarta del plan de Instinct

- El esquema nuevo de base de datos. `vistos.db` ya cumple; solo se añaden `publicadas` y `cambios`.
- La lista fija de 30 tiendas en grupos por frecuencia. Aquí la demanda se recalcula sola y las rondas ya son cada 5 y 15 minutos.
- Buscar en Chollometro por modelo. Su robots.txt lo prohíbe; se usa la copia local.
- Los estados ACEPTADA, DESCARTADA, DUPLICADA y BLOQUEADA. Aquí un candidato pasa los filtros o no, y el veredicto es buena, dudosa o caducada.
- Todo lo de WhatsApp, topes de mensajes, status por hora y "pregunta a Zeta".
- Los 4 minutos y 3 consultas por candidata: la ronda no usa IA y `SKILL.md` ya limita las consultas.
