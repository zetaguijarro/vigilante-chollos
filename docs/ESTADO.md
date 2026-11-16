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

   - Escribe `vigilante_data/entrega_N.json` y ejecuta `vigilante.py entregar N`, que manda **5 mensajes** en formato móvil.

3. Lo programa el **Programador de tareas de Windows** (con `pythonw.exe` y `vig_<categoría>.py`), no Hermes.



## Comandos

`descubrir <cat>` · `prueba <cat>` · `volcar <tiendas...>` · `wrappers` · `login <tienda>` · `informe [días]` · `dossier N...` · `recordar N buena|dudosa|caducada motivo` · `entregar N` · `<cat>` (ronda).



`catalogo [categorías|tiendas]`: saca de la portada las categorías normales de cada tienda a `vigilante_data/urls_catalogo.json`.



Pruebas: `& $py -m unittest test_vigilante`. Son 145 y deben pasar siempre. Añade pruebas con cada cambio.



## Decisiones de Zeta (no cambiar sin preguntar)

- Sin modo Morralla y sin tope de precio. Formato móvil de 5 mensajes, siempre.

- AliExpress: se admiten vendedores terceros si la tienda tiene buenas ventas y reseñas.

- Amazon: dentro, con reseñas y Keepa o CamelCamelCamel. Admite la etiqueta [BUENA].

- Viajes: FUERA las webs "piratas" (ViajerosPiratas apartada el 7/11/2026, regla de Instinct que Zeta pidió aplicar).

- **Reglas de Instinct (7/11/2026, "haz lo mismo")**: nada sin verificar; BUENA = al menos 25 % de descuento REAL (`descuento_real_min` 0.25, `descuento_real`: mercado, PCComTracker, mínimo propio o precio inicial de algo que ya fue caliente; el "antes" de la tienda solo no vale); escala EXCEPCIONAL > OJO OJITO > MUY BUENA > BUENA; bricolaje sí, jardín no; fuera motor; Back Market solo oficial, garantía y grado excelente; cupones personales se retienen; Amazon DE/FR/IT con aviso de envío (~8 €); ficha sin precio = "sin validar" (`validada` en el dossier); avisos sin emojis; el "antes" se muestra como "referencia de la tienda". Duplicados: copia local de Chollometro (30 días de /nuevos con caducados + portada) y búsqueda manual de Zeta con el enlace del aviso (`enlace_busqueda_chollometro`): ni el script ni Hermes abren /search. Manual técnico de Instinct (3 partes) aplicado: `validar_ficha` (título esperado + precio + sin muro antibot `RE_ANTIBOT` + mismo EAN), `comprobado` (hora), `busquedas_chollometro` (referencia, modelo y nombre, a mano), y en SKILL.md: vendedor tercero, variantes, total orientativo, condiciones del cupón, "vigencia no publicada", 2-4 comparables exactos (similares aparte), subir de categoría solo con pruebas, opinión marcada, retener lo no validado. No hecho: lectura de la llamada de variantes de cada web (p. ej. Cortefiel, Product-GetProductVariantData), porque es distinta en cada tienda. Instinct manda MUY BUENA y BUENA a Notion: NO hecho (haría falta un token de la API de Notion de Zeta); todo sigue yendo por Telegram.

- Juegos de Steam, Epic, Android e iOS: 80 % de descuento o más y populares.

- Exclusiones: Xbox, Shein, Temu, Wallapop y comida a domicilio.

- Silencio: de 01:00 a 08:00 no hay rondas; de 14:00 a 15:00 no se entregan ofertas (se guardan, se reverifican y se envían después).

- Un solo verificador de histórico por producto: PCComTracker, Keepa, Kimovil, perfumon, psdeals, IsThereAnyDeal o PrecioReal (y comparaloo como alternativa).

- Más reglas y lo aprendido del chat con Instinct: `LECCIONES_INSTINCT.md`.



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
