---
name: verifica-chollo
description: Verifica a fondo los candidatos de chollos que el vigilante envía por Telegram cuando Zeta escribe "verifica N" (o "verifica 3 7 12") y deja la publicación lista para Chollometro., en 5 mensajes.
---

# Verifica chollo

Zeta publica en Chollometro con el perfil PorLa_Jeta. El script le manda candidatos numerados (#N). Tú verificas y dejas cada oferta lista para copiar. Nunca publiques nada en su nombre. Responde en español: corto, directo, sin emojis y sin inflar. Pocas preguntas: decide como experto. Calidad antes que cantidad.

Objetivo: ofertas online en España validadas con rigor. **Nada sin verificar.** Solo lectura: nada de compras. Logins solo con la cuenta de Zeta ya iniciada; contraseñas nunca por chat.

## 0. Reglas de oferta
- Debe haber un precio inicial publicado. Solo online. Sin precio techo. Bricolaje sí; jardín no.
- Fuera: comida a domicilio, Shein, Temu, Xbox, motor (motos, neumáticos, accesorios de moto) y webs de viajes "piratas".
- Back Market y reacondicionados: solo vendedor oficial, garantía legal visible y grado equivalente a "excelente". Si no se ve, descarta.
- Cupones de newsletter, personales o de bienvenida de un solo uso: no se publican. Retenlos: "#N retenido: cupón personal".
- Amazon DE/FR/IT: avisa "envío a España no verificado" y suma unos 8 € de envío estimado.

PY = C:\Users\Zeus\AppData\Local\hermes\installs\d64b124a26ddeb1a\environments\3bd123044b224acf8036cde493ed887c\venv\Scripts\python.exe
V = C:\Users\Zeus\proyectos\vigilante-chollos\src\vigilante.py

## 1. Datos
Ejecuta `PY V dossier N M ...`, con todos los números a la vez.

Por cada candidato devuelve:
- num: número de la lista.
- n: producto. t: tienda. u: enlace. p: precio. a: precio anterior.
- id: mejor precio en Idealo. ch: Chollometro.
- val y res: valoración y número de reseñas o ventas.
- ficha: precio_ahora, stock, vendedor, envio, foto y fotos (hasta 2), referencia (sku/mpn), variantes (tallas o versiones con "hay stock", "agotada" o "seleccionable" = el desplegable no dice si hay stock), resumen, cupones (códigos encontrados), vigencia y specs (características reales). Lo que no venga en la ficha NO está comprobado: dilo ("envío sin verificar", "tallas sin verificar"), no lo deduzcas.
- Etiquetas honestas: si no hay comparables exactos, "sin comparar con otras tiendas"; si no hay histórico, "histórico sin verificar"; si no has hecho la búsqueda manual en Chollometro, "duplicado sin revisar". Nunca "mínimo histórico" sin datos que lo demuestren.
- historico: mínimo histórico ya consultado por el script (minimo, fuente, url, es_minimo y, si lo hay, habitual = precio habitual en esa tienda).
- err=1: posible error de precio. gratis=1: se regala.
- ch_ahora: Chollometro comprobado ahora mismo.
- memoria: veredicto anterior del modelo.
- aviso: si el precio o Chollometro han cambiado.
- validada y validacion: true solo si la ficha real muestra el producto esperado (título), su precio, sin muro antibot y con el mismo EAN si lo hay. false: validacion dice por qué.
- comprobado: fecha y hora de la comprobación (el stock y el precio caducan en minutos).
- dr: descuento real y sobre qué referencia independiente se calculó (mercado, PCComTracker, mínimo visto por el vigilante...).
- hot: si ese producto ya fue chollo caliente en Chollometro (temperatura, precio y hace cuánto).
- buscar_chollometro: enlaces de búsqueda en Chollometro (referencia, modelo y nombre) para que Zeta las haga A MANO.

Principio: una oferta es una hipótesis hasta que pasa todos los filtros. Precisión, no volumen. Mejor cero ofertas que una mala.

**Cierre obligatorio.** Cada candidato acaba en "enviado", "descartado" o "sin validar". No existen "ventaja moderada", "no priorizada" ni "pendiente". Todo descarte lleva el motivo con números: qué umbral se exigía y qué valor tenía ("descartado: baja un 12 % sobre el habitual y se exige 20 %"). No haber terminado de comprobar no es un motivo: si se acaban las 2 búsquedas sin prueba, es "descartado: sin prueba de ventaja".

No vuelvas a comprobar lo que ya trae.

## 2. Cómo verificar (máximo 2 búsquedas por candidato)

Descartes inmediatos, en una línea y sin buscar:
- memoria = "dudosa".
- aviso_ch.
- ficha.stock = "no".
- validada = false: "#N sin validar (validacion): retenido". Un 200 con HTML genérico, un shell de JavaScript o un muro antibot (DataDome, Cloudflare) NO verifica nada.
- Precio de lista inflado (PVP irreal, "antes" que nunca se vendió): desconfía; sin dr, descarta.

Pasos:
1. Ficha real: precio final, stock y variantes (talla, color, capacidad). Muchas ofertas solo están en una variante: dilo. Comprueba que es exactamente el mismo modelo o referencia (EAN, MPN o SKU), no una versión parecida.
2. Vendedor: en marketplaces con vendedor tercero, lee valoración, garantía y quién envía. Si no está claro, baja de categoría o descarta.
3. Total: precio + envío + cupón + recargos. No se prueba la cesta: escribe "total orientativo, cesta no probada". Cupón: mínimo de compra, exclusiones, si es acumulable y caducidad. Si la fecha de fin no está publicada, escribe "vigencia no publicada": nunca la inventes. Devolución y garantía, si se ven.
4. Comparables: el mismo modelo exacto en 2 a 4 tiendas más, con precio, portes y stock. Los "similares" no son comparables: menciónalos aparte. Los comparadores (PrecioReal, idealo, Google Shopping) son solo una pista: confírmalo leyendo la tienda.
5. Duplicados (obligatorio): ch_ahora es la copia local de Chollometro (con caducados), y NO sustituye a la búsqueda. La búsqueda la hace Zeta a mano con los enlaces de buscar_chollometro (referencia, modelo y nombre, en "Todas", por más recientes y sin ocultar caducados; también el foro). Ponlos en "info" como "PENDIENTE antes de publicar: buscar en Chollometro <enlaces>". Tú NO abras chollometro.com/search: su robots.txt lo prohíbe a los programas. Si ya existe activa o reciente: descarta. Si existe caducada con otro precio: anótalo y compara.
6. Histórico: nunca digas "mínimo histórico" ni "descuento real" sin un histórico independiente (Keepa en Amazon, PCComTracker, historial de Chollometro o dr). El "precio más bajo en 30 días" o el "precio medio" que tacha la tienda es una "referencia de la tienda": cítalo con esa etiqueta y no es descuento real.

Histórico: si el dossier trae "historico", úsalo y NO busques (solo comprueba que el producto de la url sea el mismo). Si no lo trae, UN solo verificador por producto, elegido por este orden:
- Tienda PcComponentes: PCComTracker.
- Amazon: Keepa o CamelCamelCamel.
- Móviles: Kimovil.
- Perfumería (Druni, Primor, Notino, Douglas, Perfumerías.com, Perfumes Club...): perfumon.com.
- PS Store y Nintendo eShop: psdeals.
- Juegos de PC (Steam, Epic, GOG, Fanatical, Humble, Instant Gaming...): IsThereAnyDeal.
- Resto: PrecioReal. Si PrecioReal no tiene el producto, comparaloo.es. No consultes los dos si el primero ya responde.
- Indica en "info" qué verificador usaste.

Qué hacer con el histórico (tres salidas, sin zona gris):
- **Hay histórico y es favorable:** el precio cumple el filtro de su categoría (apartado "Filtros"). Sigue.
- **Hay histórico y marca precio habitual:** descarta siempre, aunque el descuento impreso sea enorme. Es lo que mira el soporte de Chollometro: 13 de 15 rechazos fueron por esto.
- **No hay histórico:** no descarta por sí solo, pero entonces hace falta una de estas dos pruebas: (a) el mismo modelo exacto, nuevo y comprable ahora en otra tienda española, confirmado leyendo esa tienda, más caro en el porcentaje del filtro; o (b) `hot`: ya fue chollo caliente en Chollometro a este precio o más. Sin ninguna de las dos: "descartado: sin prueba de ventaja".
- Precio habitual = el que se mantuvo al menos 7 días antes de la bajada. La bajada tiene que tener 14 días o menos; un precio que lleva más tiempo igual ya es el habitual. Si el histórico no enseña fechas, vale su veredicto (mínimo o buen precio) junto a un precio anterior visible.
- Un resultado de búsqueda o un precio antiguo no es una alternativa comprable confirmada.

Calidad (solo si no hay memoria):
- Modelo descontinuado.
- Modelo más nuevo y más barato de la misma marca.
- Problemas conocidos.
- Compara con la alternativa equivalente de la misma marca y gama.
- Error a no repetir: DeLonghi Autentica ETAM29.620 (de 2016, descontinuada y con un PVPR viejo).

Por tienda:
- AliExpress: se admiten vendedores terceros, pero mira las ventas y las valoraciones de la TIENDA y del producto. Pocas ventas o mala nota: descarta. Indica vendedor, nota, ventas, plazo de envío a España e IVA.
- Amazon: solo si lo vende Amazon o un vendedor con buenas valoraciones y ventas.
- Steam, Epic, Android e iOS: descuento del 80 % o más y juegos populares. Indica la valoración y el número de reseñas.
- Ropa y calzado: regla de Chollometro, al menos 2 tallas con stock en ropa y 3 en calzado al precio de oferta. Con menos, descarta. Las tallas disponibles van en el título.
- Portes: se indican y se cuentan al comparar, pero no descartan por sí solos.
- socio=1 (Costco, clubs): es precio para socios. Dilo claramente en "info" y explica qué hace falta para conseguirlo.
- Viajes: solo si es un chollo de verdad (Europa 60–110 €, larga distancia 400–450 €). Título en MAYÚSCULAS con gancho, por ejemplo: "¡4 DÍAS en BUDAPEST! VUELOS + HOTEL + DESAYUNO por solo 85€". En la descripción: duración, vuelos directos o no, ciudades de salida, alojamiento y régimen (si es un hostal compartido, dilo) y fechas. Indica que es precio por persona y que el hotel es un ejemplo.

Chollometro (sin buscar: lo mira el script en su copia local):
- "?": la copia no estaba al día. Indícalo y que Zeta lo compruebe con buscar_chollometro.
- "otra:X": vale si ahora es más barato; indícalo. Otra tienda no es duplicado, salvo El Corte Inglés e Hipercor, que cuentan como una.
- "misma:X": misma tienda. Con hilo de menos de un mes solo vale si baja más de un 5 % Y más de 1 € (el script ya lo filtra). Con más de un mes, vale si el precio es distinto o el anterior se agotó. Si el hilo agotado era de Zeta, avísale para que pida reactivarlo en vez de publicar otro.
- hot: si ya fue chollo caliente, dilo en "info" (da confianza en el precio y en la demanda).

Promoción:
- Indica "vigente desde / hasta". Descarta las promociones recicladas.
- Cupón: usa ficha.cupones (comprueba que se aplican a este producto). No busques más.
- Vigencia: usa ficha.vigencia; si no viene, "inicio no confirmado".
- err=1: confirma en la ficha que el precio es real y avísalo en "info" (puede durar poco). gratis=1: indica hasta cuándo es gratis y en qué tienda se canjea.
- Si el descuento es en cupón para compras futuras, indica el precio en caja y el precio efectivo.
- Multicompra (2x1, 3x2, 2.ª unidad): calcula el precio real por unidad.

## 3. Memoria
Ejecuta `PY V recordar N buena "motivo corto"`, o `dudosa` en vez de `buena`. El motivo va sin comillas dentro.
- `dudosa`: el producto o el chollo no valen (datos contradictorios, sin ficha, no llega al filtro de SUBIENDO, precio habitual...). Bloquea el modelo 30 días.
- `caducada`: el aviso era bueno (precio de entonces por debajo del mercado), pero al verificar el precio ya ha subido o se ha agotado. Así el modelo no se bloquea: el vigilante avisa otra vez si vuelve a ese precio o menos.

## Filtros (Modo Clásico, todas las categorías)
Los porcentajes son "al menos", no "alrededor de", y no sustituyen la comparación: se miden contra el precio habitual del histórico o contra comparables exactos, nunca contra el PVP tachado, que es solo "referencia de la tienda".

| Etiqueta | Tecnología, hogar y deporte | Consumibles | Moda y calzado |
|---|---|---|---|
| SUBIENDO | 20 % bajo el habitual; como mucho un 15 % sobre el mínimo | 20 % por unidad | 30 % de descuento y 15 % frente a otras tiendas |
| DESTACADOS | 25 %; mejor precio con envío y como mucho un 8 % sobre el mínimo | 30 % por unidad o regalo útil | 50 % de descuento y 25 % frente a otras tiendas; 4 tallas en calzado o 3 en ropa |
| EXCEPCIONAL | Mínimo histórico verificado con fuente independiente Y más barato que todas las demás tiendas | igual | igual |

- Juegos digitales: 80 % de rebaja y popularidad o reseñas.
- Por debajo de SUBIENDO no es oferta: "descartado", con el porcentaje que tiene y el que se exige.
- Sin histórico, lo aceptado por otra tienda más cara o por `hot` es siempre SUBIENDO.
- Subir de etiqueta solo con la prueba de su fila. Ante la duda, la inferior.
- No hay veto por "no tiene gancho", ni modo morralla, ni reglas especiales por tienda.

## Redacción
- Datos verificados separados de impresiones: el veredicto es opinión y se marca como tal ("Opinión: ...").
- Sin afirmaciones médicas ni de durabilidad sin prueba.
- Incluye siempre la hora de la comprobación (comprobado).

## 4. Entrega (formato móvil, siempre)
Si pasa, escribe el archivo C:\Users\Zeus\proyectos\vigilante-chollos\data\entrega_N.json, en UTF-8, con estas 5 claves:

- "titulo": [EXCEPCIONAL], [DESTACADOS] o [SUBIENDO] + título sobrio: marca, modelo y especificación clave. En ropa y calzado, las tallas disponibles. Sin precio, sin emojis y sin tienda.
- "precios": "Precio inicial publicado: X €\nPrecio actual: Y €\nReferencia: Z € (fuente)\nEnvío: E €\nTotal: T € (orientativo, cesta no probada)"
- "info": INFO ADICIONAL: comprobado (hora), stock y variantes, vendedor, comparables exactos con portes (y similares aparte), mínimo histórico (solo si es independiente, con fecha y fuente), cupón con condiciones, vigencia (o "no publicada"), devolución, estado en Chollometro, "PENDIENTE antes de publicar: buscar en Chollometro <enlaces>" y "Opinión: merece la pena / dudosa", con el porqué en una línea, comparado con la alternativa.
- "enlace": el enlace u, solo.
- "descripcion": detallada, en dos partes y sin encabezados:
  1. Un párrafo de 4 a 6 frases: qué es, para quién es, qué lo distingue y para qué sirve, redactado a partir de ficha.specs, ficha.resumen y el nombre del producto.
  2. Una línea en blanco y las características clave, una por línea empezando por "• ": entre 6 y 10, las más útiles para decidir la compra (en tecnología: procesador, RAM, almacenamiento, pantalla, gráficos, batería/autonomía, conectividad, puertos, sistema operativo, peso; en audio: cancelación de ruido, autonomía, Bluetooth, códecs, resistencia al agua; en ropa: composición y tallas disponibles). Formato "• Nombre: valor".
  Fuentes, por este orden: ficha.specs (si trae ficha.specs_fuente, son del mismo modelo en un comparador), ficha.resumen y las especificaciones que aparecen en el propio nombre del producto (n). Si con eso no llegas a 6 características, pon las que haya: nada inventado ni sacado de un modelo parecido.
  Sin precio, sin tienda y sin "letra pequeña". En multicompras: 5 o 6 productos de ejemplo con su enlace y un listado de nombres.

Si la foto de la ficha es mejor que img, añade "foto": url.

Después ejecuta `PY V entregar N`. El script manda los 5 mensajes: el primero con la foto, y los otros cuatro con los precios, la información, el enlace y la descripción.

Tu respuesta final es solo: "#N enviado".
Si no pasa: "#N descartado: motivo con números", en una línea, sin archivo.
Si no se puede validar algo esencial (ficha, precio, stock): "#N sin validar: motivo. Retenido", sin archivo. Nada de compras ni suscripciones.
