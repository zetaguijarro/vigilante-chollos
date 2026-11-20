# Cómo se leen las tiendas (método de Zeta, 20/11/2026)

Fuente: el prompt "Cómo leo cada tienda" de Zeta (estado a las 13:20 del 20/11/2026). Las rutas concretas, en `config/rutas_campana.json`; se añaden al catálogo con `vigilante.py rutas` (idempotente).

## Qué se aprende de las URLs
- **Campañas primero.** El punto de partida es el hub de campaña de cada tienda (Black Friday, promociones, ofertas) y de ahí salen las categorías: MediaMarkt `?promotions=Semanas%20Black`, Carrefour `/black-friday`, Miravia `/cp/black-friday-2026/…`, Sports Direct y Coolmod por su directorio de campañas/promociones, PSS `/event/<campaña>` desde la portada.
- **No se inventan rutas.** Una URL construida por nombre no es una ruta: salen de enlaces reales de la página, de banners o de la paginación. Una ruta solo cuenta si aporta URLs de producto que no estaban ya (Hombre/Mujer/Niños/Nike de Foot Locker son subconjuntos del mismo listado). La cobertura se declara en URLs únicas de producto, no en páginas visitadas.
- **Una campaña con 0 productos es indeterminada**, no vacía, hasta comprobarlo (de las campañas de PSS, 21 daban productos y 7 cero).
- **Paginación por tienda** (`pagina_siguiente_por_tienda`): Carrefour `?offset=24`, Decathlon `?from=N&size=40`, Coolmod `?pagina=N`, Sports Direct `?dcp=N`, Private Sport Shop y Deporte-Outlet `?p=N`, Toys R Us `?page=N` (desde 0, con `?q=:relevance`), El Corte Inglés `/N/` al final de la ruta. Funciona desde la primera página, que no lleva número. El tope sigue siendo el de cada tienda (10 páginas); llegar al tope no es agotar la tienda.
- **Tiendas con ruta y aún fuera de la configuración** (`pendientes_de_alta` en el JSON): AEG, Decathlon, SharkNinja, Sports Direct y Nike. No se leen hasta que estén en una categoría de `tiendas.json`.

## Reglas de lectura que ya cumple el vigilante (o no)
- Cumple: campañas antes de categorías (catálogo), aplicar cupones y promos antes de juzgar, Chollometro antes de los comparadores, PrecioReal antes que Idealo, ficha con la URL de la tienda, moda infantil fuera, envío nunca como corte.
- No cumple todavía: sesión de 45 min por tienda, "nunca cerrar una lectura antes de tiempo", abstención con aviso cuando una etiqueta exige tallas no verificables (hoy se descarta o pasa según el caso), descubrir rutas nuevas del hub en cada tanda.
- Bloqueos: un bloqueo terminal (antibot de cuenta, captcha) no se reintenta; un KO de datos no es bloqueo.

## Histórico comercial (data/historico.db)
- La edición de Zeta (8 tablas) se carga con `historico.importar` como sustitución. Las lecturas que hace el vigilante van a **`lecturas_vigilante`** (mismas columnas que `observations`), que `importar` no toca; la vista `observaciones_todas` junta las dos con una columna `origen_dato` (`zeta` o `vigilante`).
- Cada cambio de precio que lee una ronda (primera vez que ve un producto y cada cambio) se apunta en `novedad` y se vuelca al terminar cada categoría (`volcar_a_historico`): `price_scope = listing_price`, `identity_status = URL_ONLY_UNRESOLVED_VARIANT`, `admissible = 0` (nada se valida), `source_id = vigilante-rondas` (fila propia en `sources`), `conditions_json` con el precio anterior de la tienda, el cupón y la marca de socio. Mismo producto, precio y minuto = mismo evento. Un fallo aquí no para la ronda; se apaga con `historico.guardar_lecturas: false`.
- **Enlace con Chollometro** (`enlace_cm`): por URL (mismo destino que "Ir al chollo", `cm_destino`) y por código de modelo con el 80 % de parecido. Las lecturas nuevas se enlazan al guardarlas; los hilos nuevos de Chollometro se enlazan con el histórico rehaciendo la tabla como mucho una vez al día en las rondas rápidas (`historico.enlazar_cada_horas`, 47 s).

## Qué pasa cuando una ruta desaparece y cómo se sabe cuáles rinden (20/11/2026)
Módulo `src/rutas.py`, tabla `rutas` en `vistos.db` (informe: `vigilante.py rutas informe`).
- **Rendimiento:** cada lectura de una ruta apunta productos, nuevos y cuántos pasan los filtros de tienda. `catalogo_turno` multiplica el peso de demanda por un factor (0,5 a 3) según lo que da la ruta por lectura; las rutas con menos de 3 lecturas valen 1 (no se juzgan).
- **Retirada:** 404, 410, antibot y robots salían solos. Ahora también: 3 lecturas seguidas con 0 productos o 2 que acaban en la portada (la tienda redirige: `redirige a la portada`). Con fecha y motivo. Una ruta retirada no se vuelve a sembrar en 14 días.
- **Hubs:** al final de la ronda de navegador, una vez al día por tienda (como mucho 4 por vez), se lee el hub de `rutas_campana.json` > `hubs` con el Chrome normal; los enlaces de campaña de la misma tienda (sin fichas de producto) se comprueban (máx. 4 por hub, 3 s entre cargas) y solo entran al catálogo si traen 8 productos o más y el 50 % o más no los conocía el vigilante. Cada candidata queda en `rutas` con su motivo, entre y no entre.
- **No cuenta como mérito de una campaña** que sea de Black Friday: lo que decide si es buena es lo que producen sus productos con las reglas del Modo Clásico, no la campaña.
