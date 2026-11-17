"""Botón "Verificar" debajo de cada candidato del vigilante de chollos.

El vigilante (src/vigilante.py) manda cada candidato con un botón en línea cuyo callback_data es
"vg:verifica:<N>". El adaptador de Telegram de Hermes solo atiende sus propios prefijos, así que este
plugin registra un manejador acotado a "vg:" (API pública: ctx.register_platform_handler) que:
  1. comprueba que quien pulsa está autorizado (la misma lista que el resto de botones de Hermes),
  2. lanza `vigilante.py decidir N`: la ficha se lee en vivo y los filtros los aplica el código, sin IA,
  3. según el resultado:
       - descartado: el script ya ha mandado "#N descartado: motivo". No se despierta a la IA.
       - pasa: entrega a Hermes "redacta N", una tarea corta (solo título y descripción).
       - sin validar (la ficha no se pudo leer: PcComponentes da 403 al script): entrega "comprueba N".
         Hermes solo abre la ficha con su navegador, apunta precio y stock, y el código decide con eso.
       - a mano (la tienda tiene un muro antibot reciente, `muro N`): el script ya le ha mandado el enlace a
         Zeta. No se despierta a la IA.
       - cualquier fallo del script: entrega "verifica N", el camino completo de siempre.

Desde el 17/11/2026 (Zeta: "¿siempre tenemos que hacerlo a través de una IA el último paso?").
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

PATRON = r"^vg:verifica:\d{1,7}$"
ESPERA_S = 240  # leer una ficha con navegador puede tardar; pasado esto se usa el camino de la IA


def texto_de(callback_data):
    """'vg:verifica:47' -> 'verifica 47' (None si no es nuestro)."""
    m = re.fullmatch(r"vg:verifica:(\d{1,7})", callback_data or "")
    return f"verifica {m.group(1)}" if m else None


def rutas_de_skill(texto):
    """(python, vigilante.py) tal como los declara la habilidad instalada: las líneas "PY = ..." y "V = ...".
    Así el plugin usa siempre el mismo script que la habilidad, sin rutas repetidas aquí."""
    py = re.search(r"^PY\s*=\s*(.+?)\s*$", texto or "", re.M)
    v = re.search(r"^V\s*=\s*(.+?)\s*$", texto or "", re.M)
    return (py.group(1), v.group(1)) if py and v else None


def _skill_instalada():
    casa = os.environ.get("HERMES_HOME") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes")
    return Path(casa) / "skills" / "verifica-chollo" / "SKILL.md"


def accion_de(salida, n):
    """Qué hacer con lo que imprime `decidir N` (la última línea es JSON).
    -> (texto para Hermes o None, texto del botón)."""
    try:
        d = json.loads((salida or "").strip().splitlines()[-1])
        veredicto = d["veredicto"]
    except (ValueError, IndexError, KeyError, TypeError):
        return f"verifica {n}", f"Verificando #{n}..."
    if veredicto == "descartado":
        return None, f"Descartado #{n}"
    if veredicto == "pasa":
        if d.get("entregado"):
            return None, f"Enviado #{n}"
        return f"redacta {n}", f"Redactando #{n} ({d.get('etiqueta', '')})"
    if veredicto == "a_mano":  # muro antibot reciente en la tienda: el script ya le ha mandado el enlace a Zeta
        return None, f"Lo miras tú #{n}"
    if veredicto == "sin_validar":
        return f"comprueba {n}", f"Leyendo la ficha #{n}..."
    return f"verifica {n}", f"Verificando #{n}..."


def decidir_por_codigo(n):
    """Lanza `vigilante.py decidir N` y devuelve lo que imprime; None si no se puede."""
    rutas = None
    try:
        rutas = rutas_de_skill(_skill_instalada().read_text(encoding="utf-8"))
    except OSError:
        pass
    if not rutas:
        return None
    try:
        r = subprocess.run([rutas[0], rutas[1], "decidir", str(n)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=ESPERA_S)
        return r.stdout
    except Exception as exc:  # el script no arranca o tarda demasiado: que lo mire la IA
        logger.warning("vigilante-botones: decidir %s falló: %s", n, exc)
        return None


def register(ctx):
    def _wire(application, adapter):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import CallbackQueryHandler
        from gateway.platforms.event import MessageEvent, MessageType

        async def _boton(query, n, texto):
            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(texto[:60], callback_data=f"vg:hecho:{n}")]]))
            except Exception:
                pass

        async def _pulsado(update, context):
            query = update.callback_query
            texto = texto_de(getattr(query, "data", None))
            if not texto or not query.message:
                return
            try:
                autorizado = await adapter._callback_authorized(query, adapter._callback_ctx(query), "No autorizado")
            except Exception as exc:  # si cambia la API interna, mejor no dejar pasar a nadie
                logger.warning("vigilante-botones: no se pudo comprobar la autorización: %s", exc)
                await query.answer(text="No disponible")
                return
            if not autorizado:
                return
            n = texto.split()[1]
            await query.answer(text=f"Comprobando #{n}...")
            await _boton(query, n, f"Comprobando #{n}...")
            try:
                salida = await asyncio.to_thread(decidir_por_codigo, n)
                para_hermes, rotulo = accion_de(salida, n)
            except Exception as exc:
                logger.warning("vigilante-botones: %s", exc)
                para_hermes, rotulo = texto, f"Verificando #{n}..."
            await _boton(query, n, rotulo)
            if not para_hermes:
                return
            msg, user = query.message, query.from_user
            source = adapter.build_source(
                chat_id=str(msg.chat_id), chat_name=getattr(msg.chat, "full_name", None), chat_type="dm",
                user_id=str(user.id), user_name=user.full_name, message_id=str(msg.message_id))
            evento = MessageEvent(
                text=para_hermes, message_type=MessageType.TEXT, source=source,
                user_id=str(user.id), user_name=user.full_name,
                message_id=f"vg-{query.id}", reply_to_message_id=str(msg.message_id),
                reply_to_is_own_message=True)
            await adapter.handle_message(evento)

        async def _ya_pulsado(update, context):
            await update.callback_query.answer(text="Ya está comprobado")

        application.add_handler(CallbackQueryHandler(_pulsado, pattern=PATRON))
        application.add_handler(CallbackQueryHandler(_ya_pulsado, pattern=r"^vg:hecho:\d{1,7}$"))

    ctx.register_platform_handler("telegram", _wire)
