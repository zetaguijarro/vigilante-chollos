# Lecciones del chat con Instinct (6/11/2026)

Reglas y datos sacados de un día de uso real. Sin datos personales.

## Reglas que Zeta pidió y no están (o no así) en el prompt v2

### Videojuegos
- Steam, Epic, Android e iOS: solo con un descuento del 80 % o más y juegos populares con reseñas. Indicar la valoración y el número de reseñas.
- PS Store y eShop: mínimo histórico según psdeals y los trackers de eShop. Idealo y PrecioReal comparan sobre todo copias físicas.
- El modo Morralla nunca baja el listón de los juegos.

### Modo Morralla
- Interruptor: "morralla" activa un listón más bajo solo en ofertas generales, marcadas como [Morralla]. "Quita morralla" vuelve a los filtros normales.

### Etiquetas
- Etiquetas finales: [MUY BUENA] y [EXCEPCIONAL]. Amazon admite también [BUENA].
- [EXCEPCIONAL] solo después de comparar con alternativas de la misma marca y gama.

### Amazon
- Ha cambiado varias veces: fuera, después solo multicompra, y al final dentro con [BUENA] y [MUY BUENA], verificando con Keepa o CamelCamelCamel.
- Con descarga normal da datos antiguos: hay que usar el navegador.
- La sección de Ofertas de Keepa no termina de cargar ni con sesión iniciada. Las fichas sí cargan.

### AliExpress
- Zeta quitó la regla de "solo vendedores oficiales": se admiten vendedores terceros indicando el vendedor y su nota.
- Esto contradice el prompt v2, que exige tiendas oficiales. Hay que confirmarlo con Zeta.

### Descripción
- Párrafo corrido, sin precio, sin nombre de tienda y sin encabezados.
- Contenido: qué es, composición, especificaciones y tallas.
- Nada sin verificar.
- Sin "cómo conseguirlo" ni "letra pequeña": las condiciones (cuenta obligatoria, cupón) van aparte.

### Multicompra (3x2, 2.ª unidad)
- La descripción lleva 5 o 6 productos de ejemplo con su enlace, más un listado de nombres.

### Vigencia
- Siempre "vigente desde / hasta".
- Las promociones viejas o recicladas se descartan.

### Duplicados
- Buscar también dentro de las recopilaciones de Chollometro (posts con varios títulos) y en lo caducado.
- Buscar por modelo y por tienda. Las variantes de color cuentan como el mismo producto.
- Comparar también con lo que ya ha publicado Zeta (PorLa_Jeta).
- **Última comprobación en Chollometro justo antes de enviar**: Silent Hill f se publicó a las 22:35 y se envió a las 22:49 como si fuera nuevo.

### Histórico
- Chollometro rechazó "Club Alcampo 2.ª unidad al 50 %" por ser el precio habitual. Sin histórico claro, no se envía.

### Horario
- Standby total de 01:00 a 08:00, sin rondas.
- De 14:00 a 15:00 no se entregan ofertas: lo encontrado se guarda, se reverifica y se envía después.

### Entrega
- Ofertas siempre por texto y foto, nunca en audio.
- Modo móvil: 5 mensajes por oferta.

### Forma de trabajar
- "Menos preguntas y más contraste: decide tú." Calidad antes que cantidad.

## Qué se calienta en Chollometro (análisis de Más votados y Destacados)
- Lo **gratis** gana siempre: juegos, comida, gimnasios, cursos.
- Después: viajes baratos (por ejemplo, Budapest por 85 €, con unos 3.500°), alimentación básica (AOVE de 5 L, jamón) y promociones de restaurantes (Dunkin, restaurante de IKEA).
- Las recopilaciones funcionan muy bien, pero hay que darlas montadas y con los cupones comprobados.
- Reparto de la portada: electrónica 22 %, moda 15 %, hogar 13 %. AliExpress y Amazon suman el 41 %.
- Usuario "lampara" (62 % de sus ofertas en hot): el 85–90 % son videojuegos digitales (recopilaciones de mínimos de Steam, eShop con precios regionales, Xbox, PS Plus, apps de Android gratis), más gratuitos, VPN, software y suscripciones.

## Verificadores de histórico (uno por producto)
PCComTracker (PcComponentes) · Keepa o CamelCamelCamel (Amazon) · Kimovil (móviles) · perfumon (perfumería) ·
psdeals (PS Store, eShop) · IsThereAnyDeal (juegos de PC) · PrecioReal (resto) y comparaloo si PrecioReal no lo tiene.

## Estado de las tiendas según Instinct
- **Outlet PC es outlet-pc.es**, no outletpc.com, que redirige a otra tienda.
- Entran con navegador: Samsung, Xiaomi, Phone House, Adidas Outlet, Coolmod, Zalando, PcComponentes (tras un reto de Cloudflare de unos 20 s), Douglas, Foot Locker, Lidl, Cecotec, H&M, Pull&Bear, Stradivarius, Massimo Dutti, Lefties, Primark, Epic, PS Store, Nintendo eShop, Fanatical, Humble y Google Play.
- Bloquean cualquier navegador automático (Akamai): New Balance, Sephora y H&M Home.
- Fnac: captcha intermitente.
- AliExpress: captcha en las fichas.
- Mango Outlet: antibots y tallas sin verificar.
- Viajes: Zeta descartó las fuentes "piratas" (ViajerosPiratas y similares). Hay que usar las webs que salen en las ofertas de viajes de Chollometro.
- Tiendas pedidas que faltan: Action, PowerPlanet, y las tiendas oficiales de Shark, Ninja, Dyson y Philips.

## Tiempos medidos
- Lectura por descarga normal: 1–5 s.
- Con navegador: 5–30 s, más si salta un captcha.
- Ronda ligera: 1–2 minutos. Con navegador y verificaciones: 3–8 minutos.
