# Vigilante de chollos para Chollometro (contexto para Claude Code)



Proyecto personal de Zeta (desarrollador web). Publica ofertas en Chollometro con el perfil **PorLa_Jeta** y quiere adelantarse a otros usuarios. Antes usaba Instinct (asistente de IA por WhatsApp), pero agotaba la cuota. Este sistema lo sustituye con **casi todo el trabajo en código y sin tokens**.



Habla con Zeta en español, directo y con sinceridad: quiere que des tu opinión, aunque no le guste. Prioridades: **gastar los mínimos tokens posibles** sin empeorar la calidad de las ofertas.



## Entorno (Windows)

- Carpeta de trabajo: `C:\Users\Zeus\AppData\Local\hermes\scripts`. Hermes solo ejecuta scripts que estén aquí.

- Python de Hermes, sin PyYAML pero con Playwright y Chromium instalados:

  `C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe`.

  `python` no está en el PATH: usa siempre esta ruta.

- Hermes Agent (Nous Research): su configuración y su `.env` están en `C:\Users\Zeus\AppData\Local\hermes`. Modelo: `claude-haiku-5-5`. Telegram conectado; el chat_id de Zeta es `456808276`.

- Las habilidades de Hermes van en `C:\Users\Zeus\AppData\Local\hermes\skills\verifica-chollo\SKILL.md`.

- No pidas ni muestres tokens ni claves. El script lee el token de Telegram del `.env` de Hermes.



## Arquitectura

1. `vigilante.py <categoría>`: ronda **sin IA**. Hace todo esto por código:

   - Lee las tiendas: descarga normal (6 a la vez), Chromium por Playwright (2 pestañas) o fuentes propias (Steam, Epic, r/googleplaydeals, r/AppHookup, r/FreeGameFindings, RSS de viajes y canales públicos de Telegram).

   - Detecta lo nuevo o lo que baja de su mínimo visto (SQLite en `vigilante_data/vistos.db`).

   - Filtra por marca (`marcas.txt`), descuento por categoría o tienda, reseñas y exclusiones.

   - Quita duplicados entre tiendas.

   - Consulta Idealo, con caché de 6 horas.

   - Comprueba Chollometro, el perfil de Zeta y la memoria de modelos.

   - Envía los candidatos a Telegram. Coste: 0 €.

2. Zeta escribe a Hermes **"verifica N"**. La habilidad `SKILL.md`:

   - Ejecuta `vigilante.py dossier N`, que saca por código la ficha, el stock, el envío, los cupones, la vigencia, las características y el histórico.

   - La IA solo **juzga** (calidad, comparativa y veredicto) y **redacta**.

   - Escribe `vigilante_data/entrega_N.json` y ejecuta `vigilante.py entregar N`, que manda **6 mensajes** (foto, título, enlace, precio, INFO y descripción).

3. Lo programa el **Programador de tareas de Windows** (con `pythonw.exe` y `vig_<categoría>.py`), no Hermes.



## Comandos

`descubrir <cat>` · `prueba <cat>` · `volcar <tiendas...>` · `wrappers` · `login <tienda>` · `informe [días]` · `dossier N...` · `recordar N buena|dudosa|caducada motivo` · `entregar N` · `<cat>` (ronda).



`catalogo [categorías|tiendas]`: saca de la portada las categorías normales de cada tienda a `vigilante_data/urls_catalogo.json`.



Pruebas: `& $py -m unittest test_vigilante`. Son 145 y deben pasar siempre. Añade pruebas con cada cambio.



## Decisiones de Zeta (no cambiar sin preguntar)

- Sin modo Morralla y sin tope de precio. Formato de ficha: 6 mensajes (foto, título, enlace, precio, INFO ADICIONAL y descripción) según `docs/FORMATO.md` (reglas de Zeta del 17/11/2026, sustituye a los 5 mensajes). Ropa y calzado infantil y junior, fuera desde el 17/11/2026. Sin foto del producto se entrega igual y se declara en la INFO ("⚠️ Sin foto verificada del producto."): autorizado por Zeta el 17/11/2026.

- AliExpress: se admiten vendedores terceros si la tienda tiene buenas ventas y reseñas.

- Amazon: dentro, con reseñas y Keepa o CamelCamelCamel. Admite la etiqueta [BUENA].

- Viajes: FUERA las webs "piratas" (ViajerosPiratas apartada el 7/11/2026, regla de Instinct que Zeta pidió aplicar).

- **Reglas de Instinct (7/11/2026, "haz lo mismo")**: nada sin verificar; BUENA = al menos 25 % de descuento REAL (`descuento_real_min` 0.25, `descuento_real`: mercado, PCComTracker, mínimo propio o precio inicial de algo que ya fue caliente; el "antes" de la tienda solo no vale); escala EXCEPCIONAL > OJO OJITO > MUY BUENA > BUENA; bricolaje sí, jardín no; fuera motor; Back Market solo oficial, garantía y grado excelente; cupones personales se retienen; Amazon DE/FR/IT con aviso de envío (~8 €); ficha sin precio = "sin validar" (`validada` en el dossier); avisos sin emojis; el "antes" se muestra como "referencia de la tienda". Duplicados: copia local de Chollometro (30 días de /nuevos con caducados + portada) y búsqueda manual de Zeta con el enlace del aviso (`enlace_busqueda_chollometro`): ni el script ni Hermes abren /search. Manual técnico de Instinct (3 partes) aplicado: `validar_ficha` (título esperado + precio + sin muro antibot `RE_ANTIBOT` + mismo EAN), `comprobado` (hora), `busquedas_chollometro` (referencia, modelo y nombre, a mano), y en SKILL.md: vendedor tercero, variantes, total orientativo, condiciones del cupón, "vigencia no publicada", 2-4 comparables exactos (similares aparte), subir de categoría solo con pruebas, opinión marcada, retener lo no validado. No hecho: lectura de la llamada de variantes de cada web (p. ej. Cortefiel, Product-GetProductVariantData), porque es distinta en cada tienda. Instinct manda MUY BUENA y BUENA a Notion: NO hecho (haría falta un token de la API de Notion de Zeta); todo sigue yendo por Telegram.

- Juegos de Steam, Epic, Android e iOS: 80 % de descuento o más y populares.

- Exclusiones: Xbox, Shein, Temu, Wallapop y comida a domicilio.

- Silencio: de 01:00 a 08:00 no hay rondas; de 14:00 a 15:00 no se entregan ofertas (se guardan, se reverifican y se envían después).

- Un solo verificador de histórico por producto: PCComTracker, Keepa, Kimovil, perfumon, psdeals, IsThereAnyDeal o PrecioReal (y comparaloo como alternativa).

- Más reglas y lo aprendido del chat con Instinct: `LECCIONES_INSTINCT.md`.

- **robots.txt, en ninguna web (17/11/2026, "todo mi robots déjalo vacío, es un proyecto doméstico").** `robots_siempre` vacío: Chollometro deja de ser la excepción. Siguen sin saltarse antibot ni captcha.

- **Idealo con navegador visible (17/11/2026, "a partir de ahora que sea así").** A la descarga y al Chrome oculto les da 403; a una ventana visible normal le responde. Zeta autoriza consultarlo así: Chrome visible, sin perfil, sin cambiar el agente ni ocultar la automatización. Pocas consultas (`idealo_navegador_max`, 15 por ronda) y, si también da 403, se para 3 h. Excepción expresa a "no saltarse antibot": no se resuelven retos ni captchas.

- **Miravia y AliExpress con navegador visible (17/11/2026, 21:19).** Misma excepción que Idealo: Chrome visible, sin perfil disfrazado ni ocultar la automatización, pocas páginas por ronda. Si sale reto o captcha no se resuelve: pausa fija de **30 min**, siempre la misma aunque se repita (decisión de Zeta: sin pausas que se doblen). Motivo: son las de mejor tasa en Chollometro (Miravia 51 %, AliExpress 47 %, casi siempre con cupón) y el Chrome oculto del vigilante da 0 o reto, mientras Instinct, con un navegador normal, las lee.



## Estado (7/11/2026, tras la sesión de puesta en marcha de tecnología)

- **Probado de verdad solo en tecnología.** `prueba tecnologia` (productos / con precio anterior):

  - Bien: MediaMarkt 12/9, mi.com 366/166, Euronics 111/64, Tien21 80/51, PowerPlanet 24/24, PCBox 11/8,

    Beep 11/8, Back Market 152/22, Milar 112/47, Dell 13/8, Logitech 11/9, Coolmod 25/25, Samsung 14/14,

    MielElectro 107/103, LG 12/10, Sony 60/18 (las tres últimas con navegador).

  - Sin precio anterior: Electrocosto 16/0, ElectroDepot 79/1 (no lo enseña), Huawei 124/1 (su API manda comparePrice = price).

  - 0 o casi: HP (el precio no está en el HTML de la tarjeta), Lenovo, Apple, Garmin, Rebuy, Alternate, Expert, LDLC, K-tuin; Acer da timeout.

  - Antibot, no se intenta saltar: PcComponentes, Neobyte, Fnac (403) y Worten (página de bloqueo), incluso con `"visible": true, "canal": "chrome"`.

  - PcComponentes se lee por PCComTracker (`pccomtracker.com/black-friday`, su robots.txt lo permite salvo /goto/, /admin/ y /api/): `via_comparador` se queda con las filas que enlazan directo a PcComponentes, con su URL y su precio. Da 16 productos sin precio anterior; la portada (bajadas) enlaza a fichas del propio tracker y no sirve. Pasado el Black Friday, esa página puede desaparecer.

  - outlet-pc.es es un outlet de moda y marcas (GAS, Gant, Scott...): no encaja en tecnología.

- `descubrir` prefiere páginas permanentes a las de campaña (`RE_TEMPORAL`). Siguen en Black Friday por no tener otra: mi.com, Euronics, Tien21, Milar y PCComTracker. **Al acabar el Black Friday (diciembre 2026) hay que volver a pasar `descubrir tecnologia`** y revisar a mano lo que elija (a veces prefiere una ficha o una categoría estrecha con muchos "relacionados").

- Paginación: `siguiente_pagina` (rel=next, "Siguiente", número en la URL) y, si no hay enlace, prueba `?page=2` una vez (solo descarga normal). Funciona en MediaMarkt y Electrocosto.

- `prueba` lee las tiendas de navegador igual que la ronda real (`leer_con_pestanas`, 2 pestañas). `prueba tecnologia` tarda unos 3,5 minutos.

- **En marcha (7/11/2026):** tecnología programada en el Programador de tareas ("Vigilante tecnologia" cada 15 min, más 00:01 y 09:01), con Chrome invisible, permitido con batería y límite de 20 min por ronda. Una ronda tarda ~100 s.

- Coste: en modo telegram las rondas no gastan tokens; solo "verifica N". Freno de emergencia `freno_telegram`: máx. 20 avisos/hora y 60/día (`filtros_codigo`); al pasarse pausa los envíos y avisa una vez por Telegram.

- **Idealo bloquea todo acceso automático (Akamai, 403 hasta en robots.txt): no se salta.** Si falla, `precioreal_red` consulta PrecioReal (robots.txt lo permite): busca por marca + código de modelo (`consulta_comparador`) y solo acepta el mismo modelo (`mismo_modelo`: misma variante, mismos números o mismo código exacto; ignora precios < 50 % del de la tienda, que suelen ser accesorios). `exigir_precio_mercado: true`: sin precio de mercado no se envía (el "antes" de las tiendas oficiales suele estar inflado: Sony WF-C710N "antes 199 €", mercado 71,95 €).

- Con precio de mercado, el candidato tiene que estar al menos `min_bajo_mercado` (5 %) por debajo; el "antes" de la tienda ya no cuenta (LG QNED70 a 348 € con mercado 339 € y PVPR 449 € pasaba con la regla vieja del +5 %).

- **Generalistas en marcha (7/11/2026)**: "Vigilante generalistas" cada 15 min a :10/:25/:40/:55 (7 min desfasada de tecnología), más 00:01 y 09:01. Funcionan con navegador: Amazon /deals 30/30 (productos en `productSearchResponse` de un script: `priceToPay` y `basisPrice` = precio mediano de Amazon), eBay /deals 65/17, Carrefour 29/19. Flojas: Miravia (página de L'Oréal, sin anterior), AliExpress (portada, sin anterior), El Corte Inglés (3), Casa del Libro, Action, MGI, EMP. Apartadas (`categorias.generalistas.apartadas`): Groupon (servicios: nunca pasa filtros) y Flying Tiger (marca propia). Pendiente: página de ofertas de AliExpress y reseñas de Amazon (`customerReviews`), sin las que su regla de 100 reseñas no filtra.

- **Programación ACTUAL (7/11, unificada a petición de Zeta):** solo 2 tareas + sus 00:01 y 09:01. "Vigilante rapidas" cada 5 min (`vig_rapidas.py` -> `ronda_grupo("rapidas")`: todas las categorías, solo descarga normal y fuentes; ~154 s) y "Vigilante navegador" cada 15 min (`ronda_grupo("navegador")`: solo tiendas de navegador). Cada tienda mantiene las reglas de su categoría (`ronda(categoria, solo=...)`). Límite 14 min, IgnoreNew, batería, WakeToRun. Las tareas por categoría se borraron; `cada_min` de tiendas.json ya no manda.

- Endurecido tras el primer aluvión (18 juegos viejos de GOG, 14 de Forumsport al -50 %): GOG, Instant Gaming y Humble con descuento_min 0.8 (como Steam); categorías sin comparador a 0.6 (super 0.5); `max_por_tienda_y_ronda: 2`.

- (Histórico) Intervalos por categoría antes de unificar: tecnología, generalistas, videojuegos y radar 10 min; móviles, electro_marcas, hogar y salud_belleza 15; moda, deporte y supermercado 30; juguetes, mascotas y viajes 60. Más corto no conviene: un solo Chrome compartido (moda tarda ~4 min por ronda) y riesgo de que las tiendas bloqueen por exceso de peticiones.

- **TODAS las categorías programadas (7/11/2026, con autorización de Zeta).** Tareas "Vigilante <categoría>" con su `cada_min` y minuto de inicio repartido para no pisarse el navegador (script en el historial: programar.ps1). Todas: con batería, `WakeToRun` (despiertan el portátil), límite 20 min, IgnoreNew. Energía cambiada: enchufado no se suspende ni hiberna (`powercfg /change standby-timeout-ac 0`, `hibernate-timeout-ac 0`); temporizadores de reactivación activados en AC y DC. Para deshacer: `powercfg /change standby-timeout-ac 60` y `hibernate-timeout-ac 180`.

- Filtros por categoría (tiendas.json): hogar, salud_belleza, moda, juguetes_bebe, mascotas y supermercado sin filtro de marca (marcas.txt es de tecnología) y `exigir_precio_mercado: false` con `descuento_min_sin_mercado` 0.5 (super 0.4); deporte con marca y 0.5 sin mercado. Tecnología, generalistas, electro_marcas y móviles: marca + precio de mercado.

- Viajes: `leer_rss` lee RSS y el XML de BuscoUnChollo (`FEEDS`: /xml/rss.xml, `<listing>` con `<price>`); ViajerosPiratas pone el precio en la descripción ("desde solo 332€"). 26 + 107 viajes.

- Apartadas por antibot (403 también con navegador): Leroy Merlin (y Aki, que redirige a ella), Mifarma, Dosfarma, Decathlon, Vans, The North Face, Columbia, Timberland, Asics, Levi's, Uniqlo, Idealo (radar). Sin respuesta: Reebok, Springfield, Maxizoo. Con sesión: Zalando Privé, Showroomprivé, Privalia, Veepee (`login`). Pasadas a navegador por dar 403 con descarga: Bricodepot, Bauhaus, La Redoute, Notino, Yves Rocher, iHerb, Sprinter, Motocard, Sports Direct, Esdemarca, ASOS, Dia, Hipercor, Makro, LEGO.

- **Videojuegos (7/11)**: Steam 149/149, Epic 2 (gratis marcado, host corregido a store-site-backend-static.ak.epicgames.com), GOG 74/74 (precio anterior calculado del % de descuento), Instant Gaming 28/29 (etiqueta "-NN%" de la tarjeta), PS Store 12/9, Xtralife 24/11 (página de PC; la de Xbox no: Xbox está excluido), Humble 136 (full_price, sin comprobar). `sin_idealo: true` en videojuegos: Idealo/PrecioReal comparan copias físicas. Apartadas: Driffle (claves y tarjetas regalo) y las 3 fuentes de Reddit (robots.txt prohíbe todo acceso automatizado; vía legítima: app de la API oficial con la cuenta de Zeta). 0 o error: Game, Versus, Ubisoft, Nintendo, Fanatical, Gamesplanet (403).

- Comprobación real (7/11): de 40 productos con más "descuento de tienda" en tecnología, 14 tenían precio de mercado y ninguno era más barato que el mercado (el "antes" de las tiendas suele estar inflado); 26 no están en PrecioReal. Es normal que lleguen pocos avisos.

- `bloques_json` lee todos los objetos JSON de cada script (antes solo el primero); `normalizar_importes` pasa céntimos a euros (`amount` + `currencyExponent`, Groupon).

- `mismo_modelo` aprendido de fallos reales: 'Windows 11 Pro' no es variante (Book6 ≠ Book6 Pro), un código de modelo necesita 2+ cifras (BOOK6 no lo es), y el primer número del nombre es el del modelo (Buds 6 ≠ Buds 4 aunque el nombre diga 'Bluetooth 5.4'). Casos de prueba reales en `test_precioreal_si_idealo_bloquea`.

- PcComponentes: `via_comparador` guarda también la ficha de PCComTracker de cada fila (`historico_url`, campo `h` del candidato). Sin precio de mercado, la ronda compara con su historial: pasa si está a ≤ 3 % de su mínimo o ≥ 10 % por debajo de su precio habitual. `dossier` lee ese histórico (mínimo + habitual).

- Histórico del resto de tiendas: PrecioReal no trae el mínimo histórico en el HTML (solo `lowPrice` actual) y PCComTracker no tiene buscador: `historico()` da None salvo con `h`. Pendiente (idea: histórico propio con los precios que ya guarda el vigilante).

- Descripción (SKILL.md): párrafo de 4-6 frases + 6-10 características "• Nombre: valor". Si la ficha de la tienda no trae características (o no se puede leer, como PcComponentes), `dossier` las toma de la ficha de PrecioReal del mismo modelo (`specs_comparador`, `ficha.specs_fuente`).

- Extracción: JSON-LD, microdatos, JSON de la web, atributos data-* o JSON en atributos, y como último recurso tarjetas visibles (`tarjetas_html`, solo si los datos estructurados dan menos de 3 productos).

- Las tiendas de las demás categorías y las direcciones de búsqueda de Idealo y de los comparadores **están sin probar**.

- **Siguiente paso:** poner en marcha **solo tecnología, generalistas y videojuegos** unos días. Con `informe` se decide qué ampliar. No añadir más funciones hasta tener datos reales.

- **Afinado (7/11, 20:15) tras ver que Hermes descartaba casi todo** (109 candidatos el primer día: GOG viejos, Bricoking con 1 € de ahorro, ropa/bebé con "antes" inflado):

  - `ahorro_min_eur`: 5 € global y 15 € en las categorías sin comparador (`ahorro_suficiente`, frente al mercado o, si no hay, al "antes").

  - `min_bajo_mercado` por categoría: 0.15 en hogar, belleza, moda, deporte, juguetes, mascotas y súper (PrecioReal empareja genéricos); `descuento_min_sin_mercado` 0.7 (súper 0.6).

  - Juegos: `antes_min_eur` 10 en videojuegos y 15 en GOG, Instant Gaming y Humble; GOG además `min_resenas` 300.

  - Si Chollometro ya lo tiene igual o más barato (`otra:X`/`misma:X` con X ≤ precio), no se envía (`ya_publicado_igual_o_mas_barato`, también al soltar pendientes).

  - Freno subido a 40/hora y 200/día: Zeta quiere recibir todo lo que pase los filtros y decidir; el freno queda solo contra aluviones por fallo.

- **Segundo afinado (7/11, noche): los 11 que pasaron el primer afinado también fueron malos.** La única "buena" del día fue la de PcComponentes con histórico real (PCComTracker). Conclusión: el "antes" de la tienda no prueba nada; solo se envía con contraste real:

  1. Precio de mercado (PrecioReal) solo si el producto es `identificable` (EAN, código de modelo o marca de marcas.txt). Los genéricos no se buscan (estor Decoscreen emparejado con un colchón Pikolin). Las medidas (135X190, 75X45CM) ya no cuentan como código de modelo.

  2. **Histórico propio** (`vistos.desde`, `novedad` -> `_seguido_dias`, `historial_propio_ok`): sin mercado, solo pasa lo que baja un 10 % (`bajada_min_propia`) de su mínimo visto con al menos 14 días de seguimiento (`historial_propio_dias`). Vale también para juegos (`sin_idealo`). `OLVIDAR_DIAS` subido a 120 para conservar el histórico entre rebajas. Empezó a contar el 7/11/2026: **hasta el ~21/11 las categorías sin comparador (y los juegos de pago) casi no enviarán nada; es lo esperado.**

  3. Histórico de PCComTracker (PcComponentes), sin cambios.

  4. Error de precio: necesita al menos 1 día de seguimiento (`error_precio_dias_min`).

  5. Viajes: sin histórico (cada anuncio es nuevo), tope 110 € (umbral europeo del SKILL.md).

  - CheapShark tendría el mínimo histórico de juegos, pero su robots.txt prohíbe /api/1.0/: no se usa. Alternativa legítima: API de IsThereAnyDeal con clave gratuita de Zeta.

- **Revisión a fondo (7/11, noche).** Diagnóstico: se vigilaban ~190 tiendas por volumen, no por demanda; no se miraba robots.txt por código; y la comprobación en Chollometro usaba /search, que su robots.txt PROHÍBE.

  - **robots.txt obligatorio por código** (`permitido`, `reglas_robots`, `robots_permite` con * y $, caché 24 h en `vigilante_data/robots.json`): en `descargar` y antes de cada página del navegador (`BloqueadoPorRobots`). Si el robots.txt da 401/403 al programa y la tienda va por navegador, lo lee el navegador (`robots_pendiente_de_navegador`). Prohibición real encontrada: Decimas (Disallow: /), Chollometro /search, Reddit, CheapShark /api. Idealo: su robots.txt da 403 -> ni se intenta.

  - **Chollometro, copia local** (tabla `cm`): `actualizar_chollometro` lee /nuevos (hasta enlazar con lo guardado) y la portada cada ≥4 min (en cada ronda rápida y antes de evaluar candidatos). `estado_chollometro(p, copia)` compara en local (`mismo_post`); 'no' solo si la copia tiene < 30 min. Primera carga: `vigilante.py chollometro [días_nuevos] [páginas_portada]` (hecha: 3915 posts desde el 26/7, 1870 con ≥300°).

  - **Demanda probada** (`_cm_hot`, `demanda_probada_ok`): si el mismo producto ya fue chollo caliente (≥300°, post caducado) y ahora está igual o más barato, pasa aunque no haya comparador. Sube el orden (score) y sale en el aviso ("🔥 Ya fue chollo: 850° a 36 €").

  - **Solo tiendas con demanda** (`solo_tiendas_con_demanda`, `demanda_min_hot` 2 en `demanda_dias` 120; `siempre_leer` para excepciones): 117 tiendas vigiladas no tuvieron NI UN post caliente en 3,5 meses. Quedan ~40: MediaMarkt, PcComponentes, Coolmod, Samsung, mi.com, Amazon, AliExpress, Miravia, Carrefour, ECI, Lidl, Aldi, IKEA, Deporte-Outlet, Foot Locker, PS Store, Nintendo... Se recalcula solo con la copia local.

  - Datos de Chollometro (jul-nov 2026, ≥300°): Amazon 590 de 1870 (32 %), AliExpress 146, MediaMarkt 100 (de 121 publicados), Privé 69, PcComponentes 63, Miravia 59, Showroomprivé 39, Carrefour 35, Samsung 33, Lidl 26. Precio mediano de lo caliente: 36 €.

  - **Siguiente paso (lo que más rinde): cobertura de las grandes.** Amazon solo da 30 productos (/deals); MediaMarkt 13 (página de marketplace: sus campañas cargan casi vacías, probar listados de categoría); AliExpress 0 (portada); Miravia 12 (página de L'Oréal; /flashsale da 0); Carrefour 403/503. Ventas privadas (Privé, Showroomprivé, Veepee, Privalia: ~140 calientes) necesitan `login` de Zeta.

- **Botón para verificar (7/11):** cada candidato lleva un botón en línea "Verificar #N" (`boton_verificar`, callback `vg:verifica:N`). Lo atiende el plugin de usuario de Hermes `C:\Users\Zeus\AppData\Local\hermes\plugins\vigilante-botones` (API pública `ctx.register_platform_handler("telegram", ...)`, CallbackQueryHandler acotado a `^vg:`): comprueba la autorización con la misma lista que los botones de Hermes, cambia el botón a "Verificando #N..." y entrega "verifica N" a Hermes como mensaje de Zeta en respuesta al aviso. Activado con `hermes plugins enable vigilante-botones` (se recargó en caliente). El aviso ya no lleva "-> verifica N". Se probó antes un teclado de respuestas y Zeta lo descartó ("los botones tienen que estar debajo de cada oferta").

- Además: sin URL de ficha no hay candidato; tallas de niño/bebé (`VARIANTES`) no se comparan con las de adulto (Foot Locker "Air Max 90 - Bebés" contra el precio de adulto).

- Pendiente cuando haya datos: leer la temperatura de las publicaciones de Zeta para ajustar los filtros.




- **Instrucciones de lectura de tiendas (7/11, noche; las pasó Zeta).**
  - Orden de métodos: página como la ve una persona, DOM, JSON público de la página y otra configuración del navegador.
  - Pasos 1a-1c: una tienda de descarga que da 0 productos sin error 2 rondas seguidas pasa sola a Chrome 7 días (`a_navegador:` en meta, `anotar_vacias`). Se desactiva con `filtros_codigo.navegador_si_vacia`.
  - Paso 1d: si Chrome carga la página sin productos, segundo intento con imágenes y fuentes y el doble de espera y scroll (`leer(url, completo=True)`).
  - Paso 3, catálogo completo, no solo ofertas: `catalogo_turno` lee UNA categoría por tienda y ronda, por turnos, como mucho cada `catalogo_cada_min` (60). Páginas: `paginas_catalogo` {descarga 5, navegador 3}. Una categoría con robots, 404 o antibot sale de la lista; no aparta la tienda.
  - Paso 4: robots.txt, antibot o captcha cuentan como "no se lee": fuera 24 h a la primera (`no_se_lee:` en meta, lo lista `informe`). El muro antibot en un listado vacío lo detecta `comprobar_muro`.
  - Paso 5: la ficha del dossier añade `fotos` (2), `referencia` (sku/mpn) y `variantes` (tallas con stock: "hay stock", "agotada" o "seleccionable" si solo hay desplegable).
  - Paso 7: cada aviso dice "Mercado: sin comparar con otras tiendas", "Histórico: sin verificar" o "Histórico propio: mínimo X € en N días", "duplicado sin revisar" y "Sin verificar: envío, stock y tallas".


- **PcComponentes ampliado (7/11, noche).** PcComponentes sigue sin leerse directamente: hay antibot, y Zeta pidió resolver el captcha a mano para que el robot entrara; se rechazó porque es saltárselo. Se lee por PCComTracker, cuyo robots.txt prohíbe /goto/, /admin/ y /api/.
  - Sus enlaces a la tienda van por /goto/pccomponentes?url=...: `enlace_real` saca la URL de dentro SIN visitarla. Antes se perdía y el aviso llevaba la URL del comparador.
  - `RE_ARIA_TIENDA` admite texto tras el precio (", mejor precio"). Black Friday pasa de 14 a 30 productos, todos con enlace real e histórico.
  - Catálogo: la portada y 613 subcategorías en urls_catalogo.json, 6 cada 15 min (`catalogo_por_tienda`), vuelta completa en ~1 día. Solo se ven los 40 primeros de cada una: la paginación va por /api/.
  - En las subcategorías, la URL del producto es la ficha del comparador (`historico_url`). Si sale candidato, `historico_comparador` da mínimo, habitual y `tienda_url`, y el aviso lleva a PcComponentes.
  - `tarjetas_html` coge el nombre del <h2>/<h3> dentro del enlace.
  - Histórico propio: se mantiene en 14 días (decisión de Zeta).

- **Limpieza de basura (16/11), sobre los avisos reales del 15-16/11** (de 61 enviados, 30 fuera en el repaso):
  - `RE_SIN_PRECIO_OFICIAL`: las tarjetas de PCComTracker con "Sin precio oficial de PCComponentes" se descartan (la tienda no lo vende; el precio leído era basura: secadora AEG a 9 €, localizador a 1 €).
  - `precio_sospechoso` (`filtros_codigo.precio_sospechoso` 0.4): por debajo del 40 % del mercado o del mínimo de PCComTracker no se envía (fallo de lectura; no se puede releer por el antibot).
  - "Ya fue chollo": `mismo_producto_hot` (mismo código o `mismo_modelo`) para elegir el hilo caliente, y `demanda_probada_ok` exige precio ≥ 60 % del del hilo (`HOT_PRECIO_MIN`). Antes emparejaba un monitor de 41 € con un hilo de 599 €.
  - `sin_infantil_en` ["moda", "deporte"] con `RE_INFANTIL` (niño/niña/bebé/infantil/Inf/PS/TD...). Júnior/GS se queda a propósito.
  - `repetido_reciente` (`repetido_horas` 24): misma tienda, precio ±2 % y nombre parecido = repetido (3 DualSense de colores); un número o código de modelo distinto nunca es repetido; 'vuelve' está exento.
  - Viajes apagados: BuscoUnChollo a `apartadas` (petición de Zeta; 11 avisos, todos dudosos).
  - Pendiente: AirPods Max a 130 € y Oral-B Kids a 8 € (PCComTracker) siguen pasando por quedar justo por encima del 40 %.

- **Veredicto 'caducada' (8/11).** #114 (plancha Philips en Amazon a 219 €, mercado 305 €) subió a 329,99 € a los 16 min: oferta flash acabada, no producto malo. 'dudosa' lo bloqueaba 30 días; 'caducada' no: `vuelve_oferta_caducada` lo vuelve a enviar (estado 'vuelve') si baja desde un precio mayor a ≤ el precio del aviso. SKILL.md explica cuándo usar cada uno.

- **Tiendas del ranking de Chollometro (16/11, a petición de Zeta).** Ranking de 5.627 hilos del 10 al 14/11 (`docs/SPEC.md`, apartado 3).
  - Nuevas: `aeg.com.es` (electro_marcas), `runningemotion.com` y `nbastore.eu` (deporte). **Las tres sin probar**: pasar `descubrir` y `prueba` de su categoría. Los dominios de Running Emotion y NBA Store no se pudieron comprobar desde fuera.
  - `privatesportshop.es`: apartada en deporte hasta que Zeta haga `login`; añadida a `requiere_login` y `logins`.
  - `siempre_leer` con las 27 tiendas del ranking que ya se leen: el filtro de demanda (300° en 120 días) no las puede sacar.
  - Del ranking siguen sin leerse: Decathlon y ASICS (antibot), Privé, Showroomprivé y Privalia (falta `login`) y Xbox (excluido; decisión pendiente).
  - No se ha quitado ninguna tienda. Las de peor tasa (Veepee 11 %, Deporte-Outlet 19 %, Action 20 %, Zalando 22 %, Primor 9 %) siguen como estaban.
  - Solo en este proyecto: la copia en marcha de Hermes no cambia hasta pasar a producción.

- **robots.txt deja de mirarse (16/11, decisión de Zeta: uso personal, sin ánimo de lucro).** Sustituye a "robots.txt obligatorio por código" del 7/11.
  - `filtros_codigo.respetar_robots` false: `permitido` devuelve True sin pedir el robots.txt (`robots_activo`). Sin la clave, se respeta.
  - `robots_siempre` ["chollometro.com"]: ahí sí se respeta. Es donde está la cuenta PorLa_Jeta, y la copia local ya cubre los duplicados sin abrir /search. Para quitarlo, vaciar la lista.
  - **No cambia:** antibot y captcha siguen sin saltarse; una tienda con muro sigue fuera 24 h.
  - Lo que estaba apartado por robots.txt (Reddit, CheapShark, Decimas, /api/ de PCComTracker) NO se ha reactivado: sigue en `apartadas` o sin código que lo use. Reddit además responde 403.
  - Riesgo aceptado: sin robots.txt se leen rutas que la tienda pide no leer; si una tienda responde bloqueando la IP, afecta también a la navegación normal desde casa.

- **El código confirma en la ficha antes de avisar (16/11).** Hermes descartaba al verificar lo que el listado leía mal: #184 mochila adidas a 36 € en el listado de Spartoo y a 73,51 € en su ficha, con datos de otra marca; #169 robot Cecotec a 81 € en el listado y a 229 € en la ficha. Cada descarte gastaba una pulsación de Zeta y tokens.
  - `confirmar_en_ficha` (`filtros_codigo.confirmar_ficha` true, `ficha_tolerancia` 0.02): justo antes de mandar el aviso, abre la ficha por descarga. Descarta si el título es de otro producto o si la ficha marca más de un 2 % por encima del listado. Queda en el log ("ficha no confirma").
  - Si la ficha no se puede leer (antibot, error, sin precio), el aviso sale como antes: ni confirma ni desmiente. Las tiendas que solo se leen con navegador quedan así de momento.
  - Una petición más por candidato que se va a enviar, 0 tokens. Los gratis no se comprueban.
  - **Sin probar con tiendas reales.** Riesgo conocido: `analizar_ficha` toma el primer producto de la página; si es un "relacionado" con otro precio, descartaría uno bueno. Mirar el log los primeros días.

- **Más descartes de Hermes convertidos en código (16/11, noche).** Casos #161 y #163.
  - **Error de precio sin ficha confirmada no se envía.** #161: HP EliteBook de PCBox a 2.941,99 €, "posible error de precio", sin histórico ni referencia y con la ficha de un Samsung Galaxy Book6. El error de precio se salta el contraste de mercado, así que ahora exige que `confirmar_en_ficha` dé True; si la ficha no se lee, no sale.
  - **Duplicado de la misma tienda: regla de Chollometro.** #163: DualSense en Amazon a 54,90 € con hilo de Amazon a 56,99 €. `ya_publicado_igual_o_mas_barato` con `misma:X` solo deja pasar si baja más de un 5 % y más de 1 €. Antes bastaba con ser más barato. Otra tienda no cambia.
  - **Agotado en la ficha:** `confirmar_en_ficha` descarta si la disponibilidad de la ficha dice que no hay stock. "Posiblemente agotado" (texto suelto) no descarta.
  - Sin resolver: Amazon enseña dos precios en la misma ficha (54,90 € y 9,47 €) y se lee con navegador, así que la confirmación en ficha por descarga no lo cubre.

- **Filtros del Modo Clásico en la habilidad (16/11, noche; Zeta: "adapta lo que te he enviado con tus cambios").** Solo `hermes/skills/verifica-chollo/SKILL.md`.
  - Etiquetas nuevas: SUBIENDO, DESTACADOS, EXCEPCIONAL (sustituyen a BUENA, MUY BUENA, OJO OJITO). Tabla de porcentajes por categoría; moda SUBIENDO al 30 %; EXCEPCIONAL = mínimo histórico verificado y más barato que el resto.
  - Cierre obligatorio: enviado, descartado o sin validar, con el motivo en números. Sin "ventaja moderada" ni "no priorizada".
  - Histórico con tres salidas: favorable sigue; precio habitual descarta siempre; sin histórico hace falta otra tienda confirmada más cara o `hot`.
  - Habitual = 7 días o más antes de la bajada; bajada de 14 días o menos.
  - Tallas de Chollometro (2 en ropa, 3 en calzado) y tallas en el título. Portes no descartan solos.
  - **Sin tocar, y choca:** `descuento_real_min` 0.25 en `tiendas.json` corta en la ronda lo que esté entre el 20 % y el 25 %, así que el SUBIENDO del 20 % no llega a Hermes. Y moda sin precio de mercado sigue exigiendo un 70 % impreso. Decide Zeta.
  - La habilidad en marcha es la copiada en la carpeta de Hermes: esta no se usa hasta pasar a producción.

- **Descuento real mínimo de la ronda: del 25 % al 20 % (16/11, autorizado por Zeta).** `descuento_real_min` 0.2, para que mande la misma vara que el SUBIENDO de la habilidad. Llegarán más avisos del tramo 20-25 %. Moda sin precio de mercado sigue en el 70 % impreso: no se ha tocado.

- **Repaso antes de producción (16/11, noche): dos efectos secundarios corregidos.**
  - **Idealo vuelve a no pedirse.** Con robots.txt desactivado, `idealo_red` pedía una búsqueda a Idealo en cada ronda para recibir un 403 (antes su robots.txt con 403 lo cortaba una vez al día). `idealo.es` añadido a `robots_siempre`.
  - **La confirmación en ficha no insiste contra muros.** `ficha_se_intenta`: no se intenta en las tiendas de navegador ni en `ficha_no_leer` (PcComponentes). Sin esto se pedía la ficha de PcComponentes por descarga en cada candidato, contra su antibot. En esas tiendas el aviso sale como antes, también los errores de precio.
  - Consecuencia que queda: con robots.txt desactivado, las tiendas cuyo robots.txt daba 401/403 se vuelven a pedir por descarga. Si responden 403, `registrar_fallos` las pausa (30 min a los 3 fallos, doblando hasta 24 h).

- **Diario del recorrido (16/11, a petición de Zeta).** `recorrido()` escribe en `data/recorrido.txt`, aparte de `log.txt`. Se apaga con `filtros_codigo.log_recorrido` false. Rota a los 2 MB.
  - Cabecera por categoría: cuántas tiendas por descarga, con navegador, fuentes y categorías de catálogo; las que no se leen por estar en pausa o por no tener demanda.
  - Una línea por tienda: método, página leída, productos, con precio anterior, nuevos o más baratos y los que pasan los filtros de tienda; o el error.
  - `AVISO` por cada candidato enviado y un resumen: lecturas, productos, pasan filtros, con contraste real, avisos y segundos.
  - En vivo: `Get-Content data\recorrido.txt -Wait -Tail 40`.

- **Ventas privadas activadas (16/11, noche; requiere el `login` de Zeta antes).** Zalando Privé, Showroomprivé y Privalia pasan de `apartadas` a `navegador` en moda; Private Sport Shop, a `navegador` en deporte. Las cuatro en `siempre_leer`. Veepee sigue apartada (11 % de éxito). Si una da 0 productos dos rondas seguidas, `vigilar_sesiones` avisa por Telegram para repetir el `login`.
- **Primera ronda real con el diario (16/11, 23:47).** Lee bien: 639 productos en deporte, 268 en hogar, 206 en videojuegos. Lo que enseñó y lo corregido:
  - **Páginas de ofertas mal elegidas.** Spartoo leía la de niños (431 productos), Fútbol Emotion `/ninos/oferta` y Women'secret `/nuestros-favoritos/teen`; en moda y deporte lo infantil se descarta, así que esas tiendas no podían dar nada. `descubrir` ya no elige páginas de un solo público (`RE_SEGMENTO`). **Pendiente de Zeta:** volver a pasar `descubrir deporte` y `descubrir moda` y revisar a mano. También a revisar: IKEA (una categoría de armarios), Dyson (solo cuidado del aire, 3 productos), DeLonghi (categoría, no ofertas).
  - **El radar estaba muerto:** `precioreal.com` se quedaba fuera por "sin demanda" (no es una tienda de Chollometro). Añadido a `siempre_leer`.
  - **AEG:** `aeg.com.es` sin www no responde (tiempo agotado). La clave pasa a `www.aeg.com.es`.
  - **NBA Store:** 403 por descarga. Pasa a `navegador`.
  - Nike da antibot por descarga y queda en pausa: nada que hacer. Ninja y Shark dan 0 productos en portada: pasarán solas a Chrome tras dos rondas vacías.
  - Normal: 0 "nuevos o más baratos" a las 23:47; los precios no cambian de una ronda a la siguiente a esa hora.

- **Varias páginas de ofertas por tienda y prioridad por demanda (17/11, a petición de Zeta: "no pesa lo mismo una categoría de móviles que de impresoras").**
  - **Páginas adicionales.** `descubrir` guarda, además de la principal, hasta `ofertas_extra_max` (2) páginas con rebajas por tienda (campañas activas, outlet) con la clave `"tienda+"` en `urls_ofertas.json`. La ronda las lee como mucho cada `ofertas_extra_cada_min` (15) por tienda (`ofertas_extra_turno`), con el mismo mecanismo que el catálogo. Se pueden editar a mano.
  - **Refresco semanal** (`refrescar_extras`, `ofertas_refresco_dias` 7): una vez por semana y por categoría, dentro de la propia ronda, vuelve a mirar la portada de las tiendas que se leen y renueva solo las adicionales. La principal no se toca. Como mucho una categoría por ronda; esa ronda tarda más. No crea ni cambia tareas programadas.
  - **Campaña terminada** (`pagina_caida`): si una página daba 10 productos o más y ahora da menos del 30 %, lo apunta en el log y en el diario ("OJO"), una vez al día.
  - **Peso por demanda** (`FAMILIAS`, `familia_de`, `pesos_demanda`, `peso_de`): 32 familias de producto. El peso de cada una es su tasa de hilos que llegan a 200° en la copia local de Chollometro (120 días) dividida por la media, entre 0,3 y 3; con menos de 20 hilos pesa 1. Caché de 6 h. Se recalcula solo.
    - Catálogo: `orden_catalogo` da más turnos por vuelta a las categorías con más peso (hasta 4 por cada turno de la que menos), sin dejar ninguna sin leer. Sirve sobre todo para las 613 subcategorías de PCComTracker.
    - Candidatos: el peso entra en el `score` (de −0,35 a +1), así que con el mismo descuento un móvil va antes que una impresora cuando hay tope por tienda o por tanda.
  - Sin datos suficientes en la copia (menos de 500 hilos) todo pesa igual, como antes.
  - **Sin probar con tiendas reales.** Prueba `test_freno_si_llegan_muchos_de_golpe`: entre las 00:00 y la 01:00 su último tramo no se puede simular y se salta (fallaba a esa hora desde antes).

- **`descubrir moda` real (17/11, 00:09).** Zalando elegía `/rebajas/nike/` (115 productos) en vez de `/rebajas/` (114): ahora una página más estrecha tiene que superar a la general en más de un 10 %. Sin productos con navegador: Mango Outlet, Zara, Pull&Bear, Bershka, Stradivarius, Lefties y Bimba y Lola (webs que pintan el listado con JavaScript y el extractor no lo encuentra); H&M, 403. Ninguna está en el ranking de Chollometro: no se les dedica esfuerzo por ahora.

- **MediaMarkt lee poco (17/11, 00:30).** `descubrir` y `volcar`: su mejor página de campaña da 13 productos por descarga (942 KB, 47 precios visibles); "Ofertas Flash" 2 y "Ofertas" 1. El resto carga con JavaScript al hacer scroll. El aviso "posible antibot: Captcha" de `volcar` es un falso positivo (la palabra aparece en sus scripts; la página se lee). Primer paso, solo configuración: `catalogo_por_tienda` con 2 categorías cada 10 min para `mediamarkt.es`. **Hace falta que Zeta pase `catalogo mediamarkt.es`** para generar sus categorías. Pendiente de decidir con datos: pasarla a navegador (lee con scroll, pero cada 15 min y con riesgo de muro) o entrar por listados ordenados por descuento.

- **Catálogo de MediaMarkt (17/11, 00:36).** `catalogo mediamarkt.es` dio 12 categorías: dos repetidas (la misma ruta con tildes y codificada), varios índices (informática, televisión) y faltaban auriculares, consolas, tablets o monitores. Arreglos: `categorias_de_portada` ya no repite la misma ruta, y `catalogo_por_tienda` admite `niveles: 2` y `max` (MediaMarkt: 2 y 40) para entrar en cada categoría de la portada y recoger sus subcategorías. **Zeta tiene que volver a pasar `catalogo mediamarkt.es`.** El archivo `data\urls_catalogo.json` se puede editar a mano.

- **Navegador: 4 pestañas y 3 páginas (17/11).** Antes 2 y 1. Portátil de Zeta: 16 GB de RAM, i5-8265U (4 núcleos, bajo consumo), **dedicado solo al vigilante**. Con 3 páginas se lee hasta el triple de Amazon, AliExpress, Carrefour y Zalando. A vigilar en el diario: que la ronda de navegador no pase de 14 min (límite) y que no suban las lecturas vacías; si pasa, bajar primero las páginas a 2.

- **Primera mañana en producción (17/11, 08:36).** Las rondas corren solas. Del diario:
  - **Fallo corregido: direcciones con tildes.** Las categorías de MediaMarkt (`televisión-398`, `pequeños-electrodomésticos-6201`) fallaban con "'ascii' codec can't encode character": urllib no admite caracteres que no sean ASCII. `url_ascii` las codifica dentro de `_descargar`. Afectaba a cualquier tienda con tildes en la dirección.
  - MediaMarkt: principal 13 productos (las 3 páginas no añaden), adicionales 23 (Braun) y 4. Tecnología: 794 productos en 6 lecturas (PCComTracker).
  - **Radar: 1 lectura, 0 productos.** PrecioReal ya se lee, pero su página de bajadas no da productos. Pendiente de mirar con `volcar precioreal.com`.
  - Generalistas por descarga: 33 productos en 2 lecturas. Las grandes de ahí van con navegador.
  - Aviso de "sesión caducada" en ventas privadas: probablemente falso (0 productos en la portada con la sesión iniciada). Pendiente de `volcar`.
  - Hermes respondía "#187 no existe": seguía con la habilidad antigua en memoria, que llama al `vigilante.py` y a los datos de la carpeta vieja. Hay que reiniciarlo tras pasar a producción.

- **Descarte equivocado de Hermes por una regla mal escrita (17/11, 08:48).** #189, PcComponentes: ahora 30,00 € y habitual 39,99 € en PCComTracker (25 % menos). Hermes lo descartó y lo marcó dudosa (bloqueo de 30 días) citando "un precio habitual en el histórico descarta la oferta". La frase de `SKILL.md` "Hay histórico y marca precio habitual: descarta" se podía leer como "si el histórico tiene un precio habitual, descarta". Reescrita con la cuenta explícita, (habitual − ahora) / habitual, y dos ejemplos con números. El #188 sí estaba bien descartado (ahora 21,99 € = habitual; el 15,98 € del aviso ya no estaba). Para quitar el bloqueo del #189: borrar su fila de `modelos`. **Hay que volver a copiar la habilidad a la carpeta de Hermes y reiniciarlo.**
  - Otro dato: Hermes sí puede leer la ficha de PcComponentes con su propio navegador; el script recibe 403.

- **Decisión por código en "Verificar" (17/11, autorizado por Zeta: "sí, móntalo").** El último paso ya no pasa entero por la IA.
  - `decidir N` (`decidir_item`, `clase_clasico`, `CLASICO`): lee la ficha en vivo (el mismo dossier de siempre, `dossier_items`) y aplica la tabla del Modo Clásico con cuentas. Sin IA ni interpretaciones. Veredictos: `pasa` (con etiqueta y prueba), `descartado` (con el motivo en números; guarda dudosa o caducada y lo dice por Telegram) o `sin_validar` (la ficha no se pudo leer).
  - Orden de pruebas: histórico con precio habitual (baja = (habitual − ahora) / habitual, y como mucho un 15 % sobre el mínimo); si no, ventaja frente al mercado; si no, antecedente caliente en Chollometro; en moda sin comparador, 70 % impreso; por último el descuento real que calculó la ronda. Tallas de Chollometro, agotados y duplicados descartan antes.
  - Umbrales en `CLASICO`; se pueden cambiar en `filtros_codigo.clasico.<clase>`.
  - `bloques_de_codigo` deja en `data/decision_N.json` los bloques de precios, información y enlace, la foto y la etiqueta. `redaccion N` da a la IA solo nombre, tallas, características y resumen. `entregar N` completa lo que falte con la decisión y pone la etiqueta: lo calculado por código manda sobre lo que escriba la IA.
  - Habilidad nueva `redacta-chollo` (tarea corta: título y descripción, sin navegar). `verifica-chollo` queda para lo que el código no valida (PcComponentes, muro antibot: Hermes sí lo lee con su navegador) y para "verifica N" a mano.
  - Plugin `vigilante-botones`: al pulsar lanza `decidir N`. Descartado: no despierta a la IA. Pasa: "redacta N". Sin validar o cualquier fallo: "verifica N", como antes. Toma las rutas de python y del script de la habilidad instalada.
  - `entrega.redaccion`: "hermes" o "plantilla" (título y descripción por código, 0 tokens, sin Hermes).
  - Instalación: `bash scripts/instalar_en_hermes.sh` y reiniciar Hermes.
  - **Sin probar con Hermes real:** el plugin usa su API interna (`asyncio.to_thread` + `subprocess`); si algo falla, cae al camino antiguo. El #189 (30,00 € con habitual de 39,99 €) sale ahora como DESTACADOS; el #188 (21,99 € = habitual), descartado con "baja un 0 %".

- **"Comprueba N" para las fichas que el script no puede leer (17/11, autorizado por Zeta).** `decidir 189` salía `sin_validar` por el 403 de PcComponentes, y entonces el botón mandaba el "verifica N" completo: para PcComponentes el ahorro era cero.
  - Ahora el botón manda "comprueba N". Habilidad nueva `comprueba-chollo`: Hermes abre la ficha con su navegador, apunta precio, stock y vendedor, y lanza `decidir N --precio X --stock si --vendedor Y` (`ficha_de_args`). El código decide con esos datos y no vuelve a pedir la ficha (`dossier_items(ficha_manual=...)`). Si pasa, Hermes sigue con `redacta-chollo` en el mismo turno.
  - `sin_validar` ya no manda mensaje a Telegram; solo `descartado`.
  - Descartado: fiarse del precio de PCComTracker sin abrir la tienda (el #188: tracker 15,98 €, tienda 21,99 €).
  - Instalar: `bash scripts/instalar_en_hermes.sh` (ya copia las tres habilidades) y reiniciar Hermes.

- **Precios con el euro delante, candidatas con muro antibot y Idealo (17/11, mediodía, a petición de Zeta).**
  - **Precios mal leídos.** Alternate y Expert escriben "€ 265,00" y `tarjetas_html` cogía el número anterior al símbolo (la pantalla "32:9" a 9 €, una Krups "KP2431" a 2.431 €). `euro_delante(plano)` decide por página; las tiendas de siempre no cambian. Alternate pasa de 5 a 15 productos y Expert de 3 a 10.
  - **Muro antibot en la ficha: `muro N` y veredicto `a_mano`.** Si Hermes encuentra un muro (Cloudflare en PcComponentes: #189, #193) ya no reintenta: ejecuta `muro N`, que apunta la tienda (`meta` `muro:<dominio>`, `MURO_HORAS` 6) y le manda a Zeta el enlace con "comprueba N precio X stock si". Durante 6 h las candidatas sin validar de esa tienda van directas a Zeta, sin despertar a la IA. Hermes: solo si se reinicia (ver abajo).
  - **Procesadores fuera de los códigos de modelo.** `RE_PROCESADOR`: `I5-13420H`, `I7-1185G7`, `7533HS` o `3700U` ya no cuentan como modelo del portátil. Antes la búsqueda en PrecioReal era `lenovo I5-13420H` (devolvía otros Lenovo) o `3700U` (placas de inducción), y dos portátiles con el mismo procesador contaban como "mismo código exacto".
  - **Idealo no se deja fuera.** Da 403 a programas **y** a un Chromium normal (probado el 17/11: página de 3,9 KB sin contenido): su antibot, no su robots.txt. No se esquiva. `idealo.es` sale de `robots_siempre`; tras un 403 se le deja `IDEALO_REINTENTO_H` (3) horas (archivo `data/idealo_pausa.txt`, vale para todas las rondas) y se usa PrecioReal; pasado el plazo se prueba una vez y, si responde, se usa solo. Cada aviso sin precio de mercado lleva "Idealo, a mano: <enlace>". Sustituye la decisión del 16/11 de no pedirlo.
  - **Medido el 17/11:** solo el 33-37 % de las consultas de mercado encuentran precio (212 de 571 hoy, 545 de 1.659 ayer): por ahí se pierde casi todo lo que pasa los filtros de tienda (a las 8:00, 234 pasan y 8 llegan a tener contraste). El filtro de demanda no es la causa: de 194 tiendas activas se saltan 123, pero 104 tienen 0 hilos calientes en 120 días y 19 solo uno. Decathlon (34 hilos calientes) está apartada por antibot.
  - **Hermes sin reiniciar:** el proceso `gateway run` arrancó el 15/11 a las 11:11. Carga los plugins al arrancar, así que sigue con el plugin antiguo (manda siempre "verifica N"): en todo el 17/11 solo hay un `decidir` en `log.txt` (el #189) y los #190-#195 fueron por el camino antiguo. Hasta reiniciar Hermes no funcionan `decidir` desde el botón, "comprueba N" ni `muro N`.
  - **Hermes reiniciado (17/11, 13:27, por Claude con permiso de Zeta).** `Stop-ScheduledTask Hermes_Gateway` no cierra el proceso: hay que cerrar el `python ... gateway run` y lanzar la tarea. El log confirma `Wired native handlers from plugin 'vigilante-botones'`.
- **Accesorios, mínimo propio de 14 días y por qué casi todo es PcComponentes (17/11, tarde, a petición de Zeta: "solo ofertas de PcComponentes y cosas que no se venden").**
  - **Mínimo propio con menos de 14 días.** `descuento_real` contaba el "mínimo visto por el vigilante" con 1 día de seguimiento (B&O Beoplay HX #196, 1 día; microSD #174, 7 días). Ahora exige `historial_propio_dias` (14), como dice la regla del histórico propio.
  - **Accesorios y recambios "para" otro producto.** `es_accesorio`: fundas, bolsas, carcasas, cables, filtros, tóner, cartuchos, recambios y bisagras al principio del nombre, con un "para". No se envían salvo error de precio o demanda probada. En la copia de Chollometro se calientan (300°) el 7 %, frente al 25 % de media. Las mochilas, cargadores, adaptadores y teclados "para" algo se calientan como la media y no se cortan. De los 202 avisos enviados habría cortado 2 (carcasa Acer 5750 y funda HP).
  - **Ya cortado antes:** localizador a 1 € y cables de la DJI Power 1000 a 49 € ("sin precio oficial" de PCComTracker y `precio_sospechoso`).
  - **Por qué casi todo es PcComponentes:** es la única tienda con un histórico que se lee (PCComTracker). Las demás necesitan precio de mercado, y con Idealo cerrado por antibot PrecioReal solo lo encuentra en un tercio de las consultas. Más tiendas necesitan otro comparador (Keepa, CamelCamelCamel para Amazon...) o histórico propio de 14 días. Decide Zeta.
- **Idealo con navegador visible (17/11, tarde, autorizado por Zeta).** Probado a mano: la búsqueda de Idealo responde 200 en un Chrome visible normal (56 resultados de la MSI MAG A750GLS), y 403 con descarga o con Chromium oculto. `idealo_navegador(productos, cfg)`: Chrome visible (`navegador.canal`), sin perfil (no choca con el cerrojo de las rondas de navegador), una pestaña, unos 4 s por consulta. Busca por marca + modelo (`consulta_comparador`), no por EAN: un EAN desconocido da 404 con productos al azar (Dyson, iPhone). Empareja con `mismo_modelo` (`precio_mismo_modelo`, compartido con PrecioReal). En `ronda` solo entra si la descarga está en pausa (`idealo_navegador`, `idealo_navegador_max` 15). Lo que no encuentra sigue por PrecioReal, y si da 403 se para 3 h (`data/idealo_nav_pausa.txt`). Prueba en vivo con 5 avisos de hoy: Garmin Instinct E (167,71 €) y Logitech G203 (18,89 €) encontrados. MSI A750GLS, Samsung TQ55Q7F5AU y HP 15-fc1002nsx no están en Idealo: salen modelos vecinos (A750GL, Q7FAAU, fc1001nsx) y se descartan, como debe ser.
- **Recorrido de tiendas (17/11, tarde, a petición de Zeta).** Medido en `recorrido.txt`: rondas de descarga cada 5 min (101 hoy, todas las categorías) y de navegador cada 15 min (22-34 por categoría). Cada tienda lee 1-2 páginas por ronda y entre 1 y 10 distintas al día. PcComponentes lee 183 (catálogo de PCComTracker). Se leen 63 tiendas y 123 quedan apartadas por demanda.
  - **"Navegador ocupado" ya no cuenta como fallo de la tienda** (`RE_NAV_OCUPADO` en `registrar_fallos`). De 10:43 a 12:43 el navegador estuvo ocupado (la ventana de `login`, entre otras cosas). Eso dejó en pausa a Amazon, AliExpress, Carrefour, El Corte Inglés, Coolmod, Samsung, MiElectro, Electro Depot y Huawei, y a Fnac y Neobyte 24 h. Se les quitó la pausa a mano. Las de Adidas (403), Puma (404), AliExpress (antibot), Nike, Privalia, Private Sport Shop y Tradeinn son fallos reales y siguen.
  - **Apartadas por dar 0 productos en todas sus lecturas del 17/11:** Nintendo, GOG, Fanatical, Aldi, Mango Outlet, NBA Store, Action, Ninja Kitchen, Shark, Zalando Privé y Showroomprivé. Las dos últimas, solo hasta que se lean las campañas de dentro: tienen mucha demanda (289 y 242 hilos). Sus `logins` se conservan.
  - **Pendiente:** dar más páginas a las de más demanda (Amazon, MediaMarkt, Miravia, El Corte Inglés, Carrefour), y comprobar si la copia de Chollometro se pierde hilos. Nike sale con 0 hilos en 114 días y Zooplus con 2, cuando la copia guarda unos 130 hilos al día, y 75 hilos no tienen tienda. Si se pierde hilos, el filtro de demanda aparta tiendas que sí tienen demanda.
- **Copia de Chollometro y más páginas para las tiendas con más demanda (17/11, noche, a petición de Zeta).**
  - **Temperaturas congeladas.** La temperatura se guardaba al ver el hilo en `/nuevos`, recién publicado, y solo se actualizaba si salía en la página 1 de la portada: el 92 % de los hilos de hace 3-10 días tenía la de sus primeras 6 h. Ahora cada actualización (cada 4 min) lee también una página de la portada, rotando de la 2 a la `CM_PORTADA_PAGINAS` (8, una semana de destacados). Así cada página se refresca cada ~30 min, con una sola petición más.
  - **Hueco del 9 al 13/11** (no se guardó nada). Recuperado una vez leyendo las páginas 2-40 de la portada (37 días atrás): 4 tiendas más con demanda (166). Nike sale con 0 hilos y es correcto: sus chollos van por Zalando, Privé o Showroomprivé; nike.com directo, 4.
  - **Amazon:** la portada no da categorías (son `/b?node=...` y `\d{5,}` las descarta). `catalogo_por_tienda` admite ahora `urls` (lista fija). Amazon lee 13 páginas de más vendidos por categoría (47-50 productos, ~6 s con navegador). `diy`, `shoes` y `home` no existen.
  - **Lector de tarjetas:** el enlace a las opiniones (`/product-reviews/`) partía la tarjeta de Amazon y el producto salía con el nombre "4,6 de 5 estrellas 292.057". Se ignoran `RE_ENLACE_OPINIONES` y los nombres que son una valoración (`RE_VALORACION`).
  - **Catálogo:** solo el mismo host (Carrefour metía `seguros.carrefour.es`) y sin `/servicios/`.
  - **Amazon, El Corte Inglés y Carrefour:** 2 categorías cada 15 min (antes 1 cada 60 min), con el tiempo de navegador que liberaron las 11 tiendas apartadas.
  - **Ventana de `login` olvidada:** la de `login idealo.es` (15:12) siguió abierta y las rondas de navegador encontraron el navegador ocupado hasta cerrarla. Tras un `login`, cerrar la ventana.
  - **Sin arreglo:** Fnac da 403 también con navegador (antibot). Miravia: su página de ofertas guardada es la de una marca (`/ch/loreal/ofertas`), y la portada con navegador da 0 productos. Hay que buscarle otra página a mano.
  - **Miravia, arreglado el mismo día:** página de ofertas `https://www.miravia.es/ch/pg/ofertas` en `data/urls_ofertas.json` (17 productos de 7 marcas por descarga, antes 12 de L'Oréal). Black Friday, Flash Sale y la campaña 11.11 cargan con JavaScript y dan 0 por descarga. Con navegador dan 0 todas, también esta: Miravia es de Alibaba y probablemente le pone un reto al navegador. Se queda por descarga.
- **Moda y deporte sin PrecioReal como contraste (17/11, noche, a raíz del #203).** Unas Levi's 726 High Rise Flare "clean getaway" de Zalando a 44,95 € salieron con "-29 % sobre mercado" contra 63 € de PrecioReal: era la ficha del modelo en general, sin lavado ni talla, y ya daba 404. En ropa y calzado el comparador empareja por modelo, pero el precio depende del color, el lavado y la talla. Categorías `moda` y `deporte`: `sin_precioreal` true (`idealo_red(p, precioreal=False)`). Valen Idealo, el histórico propio y la demanda probada. Hasta hoy fueron 4 avisos.
- **Reglas de formato de fichas (17/11, noche, pasadas por Zeta; copia en `docs/FORMATO.md`).** Sustituyen al formato de 5 mensajes.
  - `entregar`: 6 mensajes en orden (foto sola, título, enlace, precio, INFO, descripción). Título sin etiqueta y recortado a 140. Descripción con negrita real (`**x**` → HTML, `a_html_telegram`). Tema del grupo por categoría si se rellena `entrega.hilos` (`message_thread_id`).
  - `bloques_de_codigo`: precio con oferta, habitual acreditado (PCComTracker), PVP de la tienda y portes (`limpiar_envio`). INFO de resultado, no de proceso: 🏷️ etiqueta, 💶, 🚚, 🕒 detectada/enviada (`_hora_entrega`, con la fecha de `lista`), 📉, 🔎 hasta 3 antecedentes con enlace completo (`antecedentes_chollometro`, `enlace_hilo`: `/ofertas/<slug>-<id>` redirige al hilo) o "no localizado en la consulta" con la búsqueda manual, ⚠️ avisos. Fuera "Comprobado:", "Prueba:", "PENDIENTE…" y "Precio inicial publicado". Las tallas solo en ropa y calzado. `eur()` con coma decimal.
  - Habilidades `redacta-chollo` y `verifica-chollo` reescritas con las reglas de título y descripción. Hace falta el instalador y reiniciar Hermes.
  - Junior, GS, youth y teen se añaden a `RE_INFANTIL`. El filtro infantil vale ya para la ropa y el calzado de cualquier categoría (`familia_de`), no solo en moda y deporte.
  - **Fallo encontrado al probarlo con el #198:** "HDR10" contaba como código de modelo. La Samsung TQ55Q7F5AU se emparejó con un hilo de un TCL 65C6K de 3.516° ("demanda probada", y así pasó) y salían antecedentes de Philips y Xiaomi. `lista_codigos` ya no cuenta HDR, DVB-T2/S2/C ni H.26x.
  - Temas del grupo por categoría: Zeta no los quiere por ahora (17/11/2026); todo va al chat de siempre y `entrega.hilos` queda vacío. Pendiente: el mínimo con fecha (PCComTracker no la da); los tráileres y las recopilaciones de Steam.
- **Historial de precios (17/11, noche, autorizado por Zeta).** `productos` (una fila por producto: clave, tienda, nombre, EAN y enlace, con índice por EAN) y `precios` (una fila solo cuando cambia el precio; `WITHOUT ROWID` con clave (producto, fecha) para leer el historial de un producto de una vez, e índice por fecha). Lo apunta `novedad` en cada lectura. Los productos que ya estaban entran con su precio la próxima vez que se lean: no hay historial anterior al 17/11. Se guarda 400 días; los productos huérfanos se borran una vez al día. `minimo_con_fecha` pone en la INFO "📉 Mínimo visto por el vigilante en esta tienda: X € (fecha); seguido desde el ...". Comando `historial <texto|EAN|enlace>`: busca por nombre con LIKE, que tarda milisegundos con unos 57.000 productos, así que no hace falta índice de texto.
- **Spartoo sin la página de niños (17/11, noche).** Su página principal era `Ofertas-st-11977-0-ninos.php`: 127 de 145 productos de niño, que la regla de junior descarta. Fue la tienda que más leyó del día (92.894 productos) con 0 candidatas. Ahora en `data/urls_ofertas.json`: principal `...-hombre-zapatos.php` (el calzado es la familia con más demanda, 1,42) y extras `...-mujer-zapatos.php` y `...-mujer.php`. Cada una da 139-147 productos y ninguno de niño. `RE_SEGMENTO` ya evitaba elegir páginas infantiles al descubrir; esta era anterior. Los mosaicos de categoría ("Eco-sostenibles", "Pantalones" a 60 €) se leen como productos, pero nunca pasan: no tienen precio de mercado.
- **Multicompra: 2.ª unidad al X %, 2x1, 3x2 (17/11, noche, a petición de Zeta).** `promo_multicompra` lee el texto de la tarjeta (`promos_html`, desde el enlace del producto hasta el del siguiente) o el nombre, y marca `promo`, `unidades` y `precio_unidad`. "2.ª unidad al 70 %" se toma como un 70 % de descuento en la segunda: (1 + 0,3) / 2 = 0,65 del precio. En la ronda se usa el precio por unidad (mínimo, mercado, filtros); el suelto queda en `precio_suelto`. El aviso dice "X € por unidad comprando N (Y € la unidad suelta; promo)". La ficha pone precio por unidad, unidad suelta y total, y "⚠️ Promoción leída en el listado: hay que comprar N y confirmarla en la cesta". Al verificar, la ficha da la unidad suelta y se le aplica el mismo factor. Se excluye "acum. en tu tarjeta" o saldo (Alcampo): no es descuento en caja. Probado: Dia, 28 de 46 productos bien calculados; Alcampo, 2 de 218, porque el resto de promociones va en datos internos que no quedan junto a la tarjeta; 0 falsos positivos en 10 tiendas sin promociones (unos 640 productos). Solo ve lo que aparece en la página: lo que se aplica en la cesta no se ve sin comprar.
- **Dos fallos vistos en la ronda de prueba de las 17:26-17:35 (17/11).** 1) `idealo_navegador` se caía entero con "Page.content: … page is navigating" (Idealo redirige tras cargar): ahora espera a que termine la carga y reintenta 3 veces, y un fallo de una consulta ya no corta el lote. 2) `test_precioreal_si_idealo_bloquea` (clase `Piezas`, sin carpeta temporal) borraba `data/idealo_pausa.txt` de producción en cada pasada de pruebas, y las rondas volvían a pedir Idealo por descarga (403) en vez de usar el navegador. Ahora usa una carpeta propia. Resultado de la ronda: 115 candidatas pasan los filtros de tienda, 0 con contraste real, 0 avisos.
- **Precios históricos cruzados (17/11, noche, a petición de Zeta).** 1) **PCComTracker:** `historico_comparador` lee ahora la fecha del mínimo (`minPriceDate` de sus datos) y, si el texto no da el habitual, toma `averagePrice` (la "mediana de 12 meses" que enseña como precio habitual). La INFO pone "📉 Mínimo registrado en pccomtracker.com: 84,99 € (16/11/2026); habitual 153,98 €". 2) **Chollometro:** tabla `cm_codigos` (código de modelo → hilo), rellenada por `guardar_hilos` y una vez para los hilos que ya había. `hilos_del_producto` va por el índice cuando el nombre tiene código (4 ms en vez de recorrer la copia, ~1 s). 3) **INFO:** "📉 Mejor precio publicado en Chollometro: X € (fecha, grados); igual o mejor que ahora / ahora N % más caro" (`mejor_chollometro`). Son precios de chollo, no el habitual. 4) **Portada antigua:** lectura única de las páginas 41 en adelante (3 s entre páginas, permitido por su robots.txt) para tener meses de destacados.
- **Más contraste sin relajar reglas (17/11, noche, autorizado por Zeta).** 1) **PCComTracker para otras tiendas:** su buscador va por `/api/` (prohibido en su robots.txt), así que no se busca. Tabla `pcct` (código de modelo → ficha), que llena `guardar_pcct` cada vez que se lee su catálogo. En la ronda, los productos sin precio de mercado de otras tiendas (Amazon, MediaMarkt…) con el mismo código (`pcct_de`, regla estricta) usan esa ficha como histórico, como mucho `PCCT_MAX_RONDA` (10) por ronda. Pasan si están cerca del mínimo de PcComponentes (+3 %) o un 10 % por debajo de su habitual. Etiqueta: "PcComponentes, según PCComTracker". 2) **Chollo previo:** `chollo_previo` acepta como contraste un hilo del mismo código con 200° o más en los últimos 180 días si ahora está igual o más barato (sin bajar del 60 %). Va por el índice `cm_codigos`; sin código de modelo no se intenta. Sigue exigiendo el 20 % de descuento real sobre el precio de tienda, igual que la demanda probada.
- **Buscadores de Chollometro y PCComTracker (17/11, noche, autorizado por Zeta tras vaciar robots).** 1) **Chollometro** (`buscar_en_chollometro`): busca marca + código (o el principio del nombre) y guarda en la copia lo que encuentra. Caché de 6 h por consulta, 2 s de pausa y como mucho `CM_BUSCAR_MAX` (5) por ronda, solo para las candidatas finales, y otra al pulsar Verificar. Sale desde la conexión de casa de Zeta: por eso tan poco. Ejemplo: la Samsung TQ55Q7F5AU pasó de 0 antecedentes a 13 hilos (389 € el 16/06/2026 con 162°). Se apaga con `filtros_codigo.buscar_en_chollometro` false. 2) **PCComTracker** (`pcct_de`): primero el índice `pcct`; si no está, su `/api/search?q=…&limit=8` (caché de 24 h). La ficha se monta con `slug_pcct`, la misma fórmula que su JavaScript: con otra, redirige a una página vacía. Lo que encuentra va al índice. Cuenta cada consulta en el límite de 10 por ronda, y no se busca en moda, deporte, supermercado, belleza, mascotas, videojuegos, viajes ni juguetes (`PCCT_FUERA`). Ejemplo: Corsair RM850e de Amazon → mínimo 100,99 € (16/11/2026), habitual 119,90 €. Ojo: empareja por código, y la RM850e negra salió con la ficha de la blanca (mismo modelo, otro color).
- **Hermes no redactaba (17/11, 13:27-…).** Al reiniciar el gateway cerrando el proceso a la fuerza (Stop-Process) con un turno a medias, Hermes deja la sesión de Telegram en pausa al arrancar ("Session … paused: unknown_execution" en `agent.log`). El botón decide bien (siete "pasa" entre #197 y #207), pero "redacta N" no se ejecuta. Arreglo: `python -m hermes_cli.main sessions discard <sesión>` desde la carpeta `workspace` del entorno de Hermes, y confirmar (el turno perdido no se repite). **Para reiniciar Hermes:** mirar antes en `agent.log` que no haya un turno en marcha, y no cerrarlo a la fuerza con un mensaje entrando.
- **Duplicados de Chollometro (17/11, noche, a raíz de Zeta: "no se detectan hilos duplicados").** Con el buscador, sobre los avisos #196-#207: el #206 (LEGO Minecraft Casa-Cerdo a 11,99 €, publicado el 16/11 en Amazon al mismo precio) y el #204 (Dragon Ball, en PcComponentes el 17/11) no se detectaban, y el #205 (Garmin Instinct 3) casaba con un Fenix 8. Cambios: 1) `tokens` quita las tildes ("Bebé" se partía en "beb", "Niños" en "ni" + "os"; cambia `modelo_base`, así que los 29 veredictos guardados en `modelos` dejan de casar y caducan solos). 2) `mismo_post` no cuenta las palabras genéricas (`GENERICAS_POST`: mm, pulgadas, amoled, gps…) ni los números sueltos, y la regla del primer número solo separa números del mismo tipo: dos cortos (Buds 4 / Buds 6) sí; una edad "7+" frente al set 21268, no. 3) "10ATM" y "5BAR" no son código de modelo. Resultado: #204 y #206 salen como duplicados, #205 ya no casa con el Fenix, y #197 y #198 dejan de casar con un hilo de 1.389 € de otro televisor. **Portada antigua leída:** páginas 41-334, la copia pasa de 15.371 a 22.193 hilos y llega al 21/11/2025 (unos 900 calientes al mes desde febrero). Pendiente: cuando el buscador de Chollometro no encuentra nada, enseña los hilos calientes del día; no casan, pero se guardan en la copia.

- **Comparar con lo que ya guarda el vigilante (17/11, noche, a petición de Zeta: "que mejore a la hora de comparar").** Desde Claude en el móvil, en la rama `comparar-mejor`. Casi todo salía de PcComponentes (único con histórico, por PCComTracker) y el comparador solo encontraba precio en un tercio de las consultas. Dos fuentes nuevas, sin consultar a nadie y en milisegundos:
  - **Precio habitual propio** (`habitual_propio`, tabla `precios`): el precio actual empieza en el último cambio guardado; el habitual es la mediana ponderada por días de lo que costó en los 60 días anteriores (`HABITUAL_VENTANA_DIAS`), con al menos 7 días de historial previo (`HABITUAL_MIN_DIAS`), y solo si la bajada tiene 14 días o menos (`BAJADA_MAX_DIAS`). Un pico de un día no lo mueve. En la ronda entra después del comparador y de PCComTracker: pasa si baja al menos un 20 % (`filtros_codigo.bajada_min_habitual`). En la verificación, `dossier_items` lo pone como `historico` si no hay comparador, y `decidir_item` lo usa como el de PCComTracker. **Empieza a dar resultados hacia el 24/11** (el historial empezó el 17/11); se activa solo según se acumulan días.
  - **Mercado propio** (`mercado_propio`): el mismo EAN (con o sin ceros delante) en otras tiendas que lee el vigilante, visto en las últimas 48 h (`MERCADO_PROPIO_HORAS`). Sin la propia tienda, El Corte Inglés e Hipercor como una sola, y fuera precios de menos de la mitad o más del triple. **Solo cuando el comparador no da precio**: si dos tiendas bajan a la vez (promoción de marca, Black Friday), compararlas entre sí descartaría las dos; en ese caso decide el habitual propio. Funciona ya, para los productos con EAN.
  - Los candidatos con mercado o habitual propio entran aunque no quepan en las consultas externas (`max_consultas_idealo_por_tanda`): hasta `comparar_propio_max` (60) más por ronda.
  - El aviso dice "Mercado: X € en <tienda> (visto por el vigilante)" o "Histórico (historial del vigilante)". El candidato guarda `mp` y `hab`.
  - Se apaga con `filtros_codigo.comparar_propio` false. **Sin probar con datos reales**: mirar en `recorrido.txt` y `log.txt` que no salgan avisos raros; el riesgo es un EAN mal leído que case dos productos distintos.

- **Amazon sin resultados (17/11, noche, Zeta: "de Amazon no me está dando resultado").** Diagnóstico desde el código, sin los datos:
  - **Enlaces cambiantes (arreglado, `url_canonica`).** Amazon pone en el enlace el puesto en la lista (`/ref=zg_bs_..._3`) y la sesión. Si el puesto cambia, el enlace cambia, y `clave_vista` (tienda + enlace) creaba un producto nuevo: nunca llegaba a "bajada", nunca acumulaba historial y el habitual propio no le serviría. Ahora Amazon queda como `/dp/ASIN` y AliExpress como `/item/<id>.html` antes de `novedad`. El historial de Amazon guardado hasta hoy queda con claves viejas: empieza de cero. Las demás tiendas no cambian. `asin_de` saca el ASIN (para CamelCamelCamel o Keepa).
  - **Fuente equivocada (pendiente de probar en el portátil).** Lee "más vendidos" (`/gp/bestsellers/...`): productos que se venden mucho, no rebajados. Candidatas a probar: `/gp/movers-and-shakers/<categoría>` (los que más suben en ventas, a menudo por bajada de precio) y `/deals`.
  - **Sin EAN ni precio tachado en sus listados:** Idealo y PrecioReal buscan por nombre y aciertan poco; el mercado propio (por EAN) no le sirve. El histórico de verdad para Amazon sería CamelCamelCamel con el ASIN (`https://es.camelcamelcamel.com/product/<ASIN>`): falta comprobar desde casa si tiene antibot.
  - Para medir en el portátil: `grep amazon data/recorrido.txt | tail -n 20` (productos, nuevos o más baratos, pasan filtros) y `grep -i amazon data/log.txt | tail -n 20`.
- **Falso 2x1 (17/11, 22:18, aviso de Zeta).** Un aire acondicionado "Split 2x1" (dos unidades interiores y una exterior) salió como oferta 2x1 a mitad de precio. `promo_multicompra`: un NxM junto a palabras técnicas (`RE_NXM_TECNICO`: split, multisplit, HDMI, KVM, regleta, cable, puertos...) no cuenta salvo que vaya con una palabra de promoción (`RE_NXM_PROMO`: oferta, promo, llévate, paga...). En el nombre del producto (`nombre=True`) un NxM solo cuenta con palabra de promoción. Y en la tarjeta se quita el nombre antes de buscar la etiqueta. "2ª unidad al X %" y "Llévate 3, paga 2" no cambian.
- **Miravia y AliExpress con Chrome normal (17/11, noche, encargo 2 de Zeta).** `NavegadorNormal`: Chrome visible sin perfil, sin cambiar el agente, sin ocultar la automatización y sin bloquear imágenes (`config/tiendas.json` → `navegador_normal`: tiendas, 1 página, `pausa_min` 30). Va en su propio Chrome a la vez que las pestañas del perfil, en la ronda de navegador; Miravia pasa de descarga a `navegador` en generalistas. Lectores propios en `extraer`: `tarjetas_miravia` (nombre en el alt de la foto, precio a la vista, original en `item_price` del enlace, en céntimos) y `productos_aliexpress` (datos de la página: `salePriceAmount`/`originPriceAmount` en céntimos de euro, reseñas, `reviewStar` sobre 5, marca y `cupon_elegible` si pone "Elegible con cupones"). Página de AliExpress: SuperDeals (`/ssr/300002660/Deals-HomePage`, en `data/urls_ofertas.json`). Prueba real: Miravia 50 productos (antes 11, en inglés), AliExpress 52 (antes 0, en pausa por antibot), sin reto, 20 s. Si la página acaba en un reto (`/punish`, `x5secdata`, captcha, login) o enseña un muro: no se resuelve, pausa fija de 30 min, la misma aunque se repita y sin "no se lee" de 24 h; otros fallos, a los 3 seguidos, también 30 min. Ojo: el "precio original" de Miravia suele estar inflado (Ariel 37,93 € "antes" 98,93 €); manda el precio de mercado, como siempre. AliExpress además trae "Precio más bajo en 30 días" en la tarjeta: sin usar todavía.
- **Búsquedas por familia en Miravia y AliExpress (17/11, noche, Zeta: "por las categorías que más grados dan en Chollometro", 4 por ronda).** Ninguna de las dos necesita sesión para esto (probado). Las categorías de AliExpress (`/category/<id>/...`) dan 0 productos y su portada no las enlaza; sus búsquedas (`/w/wholesale-<palabras>.html`) sí funcionan, con lector propio (`productos_aliexpress_busqueda`: el precio original no viene y se calcula con el % de descuento). En Miravia, `/catalog/?q=<palabras>` da unos 80 productos (`/search?q=` da la mitad). `busquedas_familia` en `tiendas.json`: consultas agrupadas por familia (`familia_de`); `busquedas_turno` elige 4 por tienda y ronda de navegador (como mucho cada 15 min), por turnos ponderados: cada consulta avanza 1/tasa, donde la tasa es la parte de los hilos de esa familia **en esa tienda** que llegan a 200° en la copia de Chollometro (`tasas_familia_tienda`, caché de 6 h). Ejemplo: en AliExpress las consolas llegan al 88 % y los robots aspiradores al 46 %; en Miravia aspiración 90 % y gran electrodoméstico 86 %. Ojo: una familia con más consultas sale más veces; se reparte cambiando las consultas en `tiendas.json`. Un tercio de los hilos de estas tiendas no encaja en ninguna familia y no cuenta. Se leen como catálogo (1 página) en el Chrome normal, y un reto en una búsqueda también para la tienda 30 min. Prueba real de una ronda: 8 búsquedas, 552 productos en 67 s, sin reto.
- **Amazon: el tachado de /deals como contraste (17/11, noche, autorizado por Zeta: "haz la 1").** Diagnóstico: Amazon pasaba los filtros de tienda (30-45 por ronda) pero el diario marcaba "0 con contraste real" en todas: sin mercado (Idealo por nombre casi no acierta) ni histórico. Por la norma europea de precios (Omnibus), en los datos de `/deals` cada precio tachado lleva su etiqueta: "Recomendado" (PVPR, 23 de 30 el 17/11), "Mediano" (mediana de lo pagado en Amazon en 90 días, 6) y "Más bajo" (el más bajo de 30 días, 1). `etiquetas_amazon` las lee (cada producto empieza por `{"asin","title"`; las variantes de color no llevan title) y deja `ref_tienda` en el producto si el precio cuadra. `contraste_ref_tienda`: "Mediano" y "Más bajo" cuentan como histórico de la tienda, con la misma bajada que el habitual propio (20 %, `bajada_min_habitual`); "Recomendado" no cuenta. Va después del mercado y de PCComTracker. En la verificación (`dossier_items`, con `rt` del candidato) y en la ficha ("Precio más bajo de los últimos 30 días (según Amazon)" o "Precio habitual ... mediana de 90 días", sin repetirlo como PVP). Con la página del 17/11 pasarían 3 de 30 (pañales Dodot -34 %, toallitas -40 %, medicube -31 %). Se apaga con `filtros_codigo.ref_amazon` false.
  - `/gp/movers-and-shakers/<categoría>` no sirve: Amazon España responde "no hay Productos del momento disponibles en esta categoría".
  - **CamelCamelCamel:** la descarga da 403; con Chrome visible normal (una consulta de prueba, autorizada) responde sin reto y trae en el HTML mínimo de siempre con fecha, máximo, actual y media del precio de Amazon y de terceros (Dodot B07M98CL6Y: mínimo 45,49 € el 09/07/2020, media 81,06 €). No está en las rondas: falta que Zeta decida si se usa así.
  - `/deals` sigue dando 30 productos por lectura (no tiene página siguiente).
