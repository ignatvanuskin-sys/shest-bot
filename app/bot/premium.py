"""Premium (custom) Telegram emoji — the single place where emoji IDs live.

Telegram renders a custom emoji when the outgoing HTML text carries
``<tg-emoji emoji-id="...">глиф</tg-emoji>``, or when an inline keyboard button
carries ``icon_custom_emoji_id``. Both were verified against the live Bot API.

To change an emoji, edit ``PREMIUM_EMOJI`` below: every message and button picks
it up automatically. The comment on each entry is the owner's name for that
emoji — where the owner's list had no exact match, the nearest one was chosen and
the comment says so.

IMPORTANT: any message built with :func:`emoji` must be sent with
``parse_mode=ParseMode.HTML``, and every *dynamic* value in it must be escaped
with :func:`aiogram.utils.markdown.html_decoration.quote` (see ``cards.py``).
"""

from __future__ import annotations

from aiogram.utils.markdown import html_decoration

# name -> (custom emoji id, fallback glyph)
PREMIUM_EMOJI: dict[str, tuple[str, str]] = {
    # --- owner's map, exact matches ---
    "bot": ("6030400221232501136", "👋"),          # 🤖 Бот (приветствие)
    "stats": ("5870921681735781843", "📊"),        # 📊 Статистика график
    "settings": ("5870982283724328568", "⚙️"),     # ⚙ Настройки
    "check": ("5870633910337015697", "✅"),        # ✅ Галочка
    "cross": ("5870657884844462243", "❌"),        # ❌ Крестик
    "pencil": ("5870676941614354370", "✏️"),       # 🖋 Карандаш
    "company": ("5873147866364514353", "🏢"),      # 🏘 Дом
    "city": ("6042011682497106307", "📍"),         # 📍 Геометка
    "address": ("6042011682497106307", "🏠"),      # 📍 Геометка (тот же id, что у города)
    "phone": ("6039451237743595514", "📞"),        # 📎 Скрепка
    "email": ("5870528606328852614", "✉️"),        # 📁 Файл
    "instagram": ("6035128606563241721", "📸"),    # 🖼 Медиа фото
    "telegram": ("6039422865189638057", "✈️"),     # 📣 Рупор
    "website": ("5769289093221454192", "🌐"),      # 🔗 Ссылка
    "category": ("5886285355279193209", "🏷️"),     # 🏷 Бирка
    "services": ("5884479287171485878", "🔧"),     # 📦 Коробка
    "rating": ("5870930636742595124", "⭐"),        # 📊 Рост график
    "reviews": ("5870772616305839506", "👥"),      # 👥 Люди (кол-во отзывов)
    "contact": ("5870994129244131212", "👤"),      # 👤 Профиль
    "description": ("5870753782874246579", "📝"),  # ✍ Писать
    "warning": ("6028435952299413210", "⚠️"),      # ℹ Инфо
    "analyzing": ("5345906554510012647", "🔎"),    # 🔄 Загрузка
    "collecting": ("6039802767931871481", "📥"),   # ⬇ Скачать
    "row": ("5870528606328852614", "📄"),          # 📁 Файл (строка в таблице)
    # "если встретится" — пока не используются, лежат наготове для владельца.
    "time": ("5775896410780079073", "⏰"),          # 🕓 Время прошло
    "party": ("6041731551845159060", "🎉"),        # 🎉 Ура
    "lock": ("6037249452824072506", "🔒"),         # 🔒
    "unlock": ("6037496202990194718", "🔓"),       # 🔓
    # --- nearest match: no dedicated emoji in the owner's list ---
    "whatsapp": ("6039451237743595514", "💬"),     # 📎 Скрепка (ближайшее: мессенджер-контакт)
    "tags": ("5886285355279193209", "🏷️"),         # 🏷 Бирка (ближайшее: как категория)
    "source": ("5769289093221454192", "🔎"),       # 🔗 Ссылка (ближайшее: откуда пришёл лид)
}

# Reversed view for convenience when the owner wants to look up an id.
EMOJI_IDS: dict[str, str] = {name: emoji_id for name, (emoji_id, _) in PREMIUM_EMOJI.items()}


def _entry(name: str) -> tuple[str, str]:
    try:
        return PREMIUM_EMOJI[name]
    except KeyError:  # pragma: no cover - defensive, names are literals in code
        known = ", ".join(sorted(PREMIUM_EMOJI))
        raise KeyError(f"unknown premium emoji {name!r}; known names: {known}") from None


def emoji_id(name: str) -> str:
    """Return the raw custom-emoji id (for ``icon_custom_emoji_id``)."""
    return _entry(name)[0]


def emoji(name: str, fallback: str = "") -> str:
    """Return ``<tg-emoji emoji-id="...">глиф</tg-emoji>`` for *name*.

    ``fallback`` overrides the glyph placed inside the tag (what clients that
    cannot render custom emoji show); by default the canonical glyph from
    ``PREMIUM_EMOJI`` is used. The glyph is HTML-escaped, so the result is always
    safe to drop into an HTML message.
    """
    emoji_id_, glyph = _entry(name)
    return (
        f'<tg-emoji emoji-id="{emoji_id_}">'
        f"{html_decoration.quote(fallback or glyph)}"
        f"</tg-emoji>"
    )
