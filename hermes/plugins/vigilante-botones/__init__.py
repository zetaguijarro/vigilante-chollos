"""Botón "Verificar" debajo de cada candidato del vigilante de chollos.

El vigilante (scripts/vigilante.py) manda cada candidato con un botón en línea cuyo callback_data es
"vg:verifica:<N>". El adaptador de Telegram de Hermes solo atiende sus propios prefijos, así que este
plugin registra un manejador acotado a "vg:" (API pública: ctx.register_platform_handler) que:
  1. comprueba que quien pulsa está autorizado (la misma lista que el resto de botones de Hermes),
  2. entrega a Hermes el texto "verifica <N>" como un mensaje normal del usuario, respondiendo al aviso,
  3. cambia el botón por "Verificando #N..." para que no se pulse dos veces.
"""

import logging
import re

logger = logging.getLogger(__name__)

PATRON = r"^vg:verifica:\d{1,7}$"


def texto_de(callback_data):
    """'vg:verifica:47' -> 'verifica 47' (None si no es nuestro)."""
    m = re.fullmatch(r"vg:verifica:(\d{1,7})", callback_data or "")
    return f"verifica {m.group(1)}" if m else None


def register(ctx):
    def _wire(application, adapter):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.ext import CallbackQueryHandler
        from gateway.platforms.event import MessageEvent, MessageType

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
            await query.answer(text=f"Verificando #{n}...")
            try:
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"Verificando #{n}...", callback_data=f"vg:hecho:{n}")]]))
            except Exception:
                pass
            msg, user = query.message, query.from_user
            source = adapter.build_source(
                chat_id=str(msg.chat_id), chat_name=getattr(msg.chat, "full_name", None), chat_type="dm",
                user_id=str(user.id), user_name=user.full_name, message_id=str(msg.message_id))
            evento = MessageEvent(
                text=texto, message_type=MessageType.TEXT, source=source,
                user_id=str(user.id), user_name=user.full_name,
                message_id=f"vg-{query.id}", reply_to_message_id=str(msg.message_id),
                reply_to_is_own_message=True)
            await adapter.handle_message(evento)

        async def _ya_pulsado(update, context):
            await update.callback_query.answer(text="Ya se está verificando")

        application.add_handler(CallbackQueryHandler(_pulsado, pattern=PATRON))
        application.add_handler(CallbackQueryHandler(_ya_pulsado, pattern=r"^vg:hecho:\d{1,7}$"))

    ctx.register_platform_handler("telegram", _wire)
