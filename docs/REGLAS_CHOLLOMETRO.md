# Reglas de publicación de Chollometro

Fuente: https://www.chollometro.com/reglas (leída el 18/11/2026). También existen /normas (Normas Comunitarias) y /faqs.

## No se permiten

- Ilegal, político, religioso, polémico; apuestas, réplicas, piratería, porno, armas, drogas, tabaco y vapeo.
- Ofertas bancarias (salvo que el banco lo apruebe).
- Autopromoción: referidos, códigos amigo, **afiliados**, cualquier cosa que dé comisión.
- Vendedores o tiendas no fiables (antigüedad, popularidad, opiniones).
- Difícil acceso: una sola unidad, códigos de un solo uso, **ropa con menos de 2 tallas**, **calzado con menos de 3 tallas**, sorteos, "gratis" a cambio de acciones sociales, **productos que no envían a España**, errores de precio de tiendas pequeñas o de tiendas que suelen cometerlos.
- Exclusivas de otra plataforma; plataformas de cashback (Beruby, Letyshops); copiadas de otra web de ofertas.
- Kindle gratis, inmuebles, coches con financiación, viajes sin fecha o destino.

## Duplicados (regla 1)

- Mismo enlace = mismo chollo. Enlace distinto con el mismo color suele ser el mismo producto.
- Anterior de **más de 1 mes**: precio distinto, o mismo precio con el anterior agotado → vale el nuevo. Mismo precio sin agotar → vale el antiguo.
- Anterior de **menos de 1 mes**: solo vale el nuevo si es **más de un 5 % y más de 1 € más barato**.
- Oferta agotada que vuelve: se reactiva la última publicación (regla 4).

## Otras

- Regla 6: la misma oferta en dos tiendas se permite, **salvo El Corte Inglés e Hipercor** (comparten stock).
- Regla 5: un producto suelto que ya está en una recopilación de productos similares, con foto y título, no.
- Regla 8: con menos tallas que las mínimas, solo en recopilación de productos similares.
- Regla 9: AliExpress solo cuando la oferta oficial ya esté disponible.
- Regla 10: recopilaciones solo de productos relacionados (similares, misma promoción o categoría).

## En el vigilante (18/11/2026)

- Hecho: duplicados (regla 1, en `estado_chollometro` y `ya_publicado_igual_o_mas_barato`), El Corte Inglés/Hipercor (regla 6), otra tienda con la oferta ya agotada no cuenta, enlaces limpios sin afiliado (`url_canonica`).
- Pendiente: tallas mínimas (2 ropa / 3 calzado: `variantes_de` las lee, falta aplicar la regla), una sola unidad, no envía a España, códigos de un solo uso.
