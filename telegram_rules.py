"""Conservative classification of Telegram links explicitly published in HTML.

No network access is used: link purpose can only be inferred from local labels.
Ambiguous links are intentionally retained for a person's review.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag

HOSTS = {"t.me", "telegram.me", "telegram.dog"}
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z", re.ASCII)
SERVICE_PATHS = {
    "share", "iv", "proxy", "socks", "addstickers", "addemoji", "setlanguage",
    "login", "auth", "confirmphone", "invoice", "giftcode", "boost", "nft",
}
BOT_QUERY = {"start", "startgroup", "startapp", "attach", "startattach"}
VISIBLE_URL = re.compile(
    r"(?<![\w@/.:~-])(?:(?:https?://|//)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']+)",
    re.I,
)
CHANNEL = re.compile(
    r"канал|подпи(?:с|ш)|новост|блог|дайджест|"
    r"\b(?:channel|subscribe|newsletter|news|blog|digest)\b", re.I,
)
BOT_CONTEXT = re.compile(r"\b(?:бот(?:а|у|ом|ы)?|bot|chatbot|чат[- ]?бот)\b", re.I)
CONTACT = re.compile(
    r"напи(?:сать|шите|ши|сав)|связ(?:аться|ь\s+с)|свяжитесь|"
    r"обсуд(?:ить|им)|задать\s+вопрос|спросить|заказать|"
    r"(?:чат|связь)\s+(?:с\s+)?(?:менеджер|нами|команд|отдел|поддержк)|"
    r"(?:менеджер|отдел\s+продаж|поддержк)[^.!?]{0,35}(?:telegram|телеграм)|"
    r"(?:telegram|телеграм)[^.!?]{0,30}(?:для\s+связи|for\s+contact)|"
    r"(?:для\s+связи|for\s+contact)[^.!?]{0,30}(?:telegram|телеграм)|"
    r"\b(?:write\s+to\s+us|message\s+us|contact\s+us|talk\s+to\s+us|"
    r"chat\s+with\s+(?:us|sales)|get\s+in\s+touch)\b", re.I,
)


def _link(raw: str) -> tuple[str | None, str | None, str]:
    """Return (canonical, mandatory decision or None, reason)."""
    raw = raw.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    elif re.match(r"^(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", raw, re.I):
        raw = "https://" + raw
    try:
        p = urlsplit(raw)
        query = parse_qs(p.query, keep_blank_values=True)
        query = {key.lower(): value for key, value in query.items()}
        if p.scheme.lower() == "tg":
            if p.netloc.lower() in {"msg_url", "share", "addstickers", "addemoji", "socks", "proxy", "setlanguage"}:
                return None, "reject", "Служебная ссылка Telegram / кнопка «Поделиться»"
            if p.netloc.lower() != "resolve":
                return None, "review", "Ссылка Telegram без проверяемого username"
            if {"post", "comment", "thread", "story"}.intersection(query):
                return None, "reject", "Ссылка на отдельный пост / комментарий"
            if BOT_QUERY.intersection(query):
                return None, "reject", "Команда запуска бота / Telegram-приложения"
            names = query.get("domain", [])
            if len(names) != 1:
                return None, "review", "tg://resolve без однозначного username"
            name = names[0]
        else:
            host = (p.hostname or "").lower().removeprefix("www.")
            if p.scheme.lower() not in {"http", "https"} or host not in HOSTS:
                return None, None, ""
            if p.username or p.password or p.port not in {None, 80, 443}:
                return None, "review", "Необычная ссылка Telegram"
            parts = unquote(p.path).strip("/").split("/")
            first = parts[0].lower()
            if first in SERVICE_PATHS:
                return None, "reject", "Служебная ссылка Telegram / кнопка «Поделиться»"
            if first in {"s", "c"} or (len(parts) > 1 and parts[-1].isdigit()):
                return None, "reject", "Канал или ссылка на отдельный пост"
            if first.startswith("+") or first == "joinchat":
                return None, "review", "Пригласительная ссылка: тип и назначение неизвестны"
            if len(parts) != 1:
                return None, "review", "Ссылка Telegram без однозначного username"
            if BOT_QUERY.intersection(query):
                return None, "reject", "Команда запуска бота / Telegram-приложения"
            if {"post", "comment", "thread", "story", "boost"}.intersection(query):
                return None, "reject", "Ссылка на отдельный контент / служебное действие"
            name = parts[0]
        if not USERNAME.fullmatch(name):
            return None, "review", "Username не соответствует обычному формату; нужна проверка"
        if name.lower().endswith("bot"):
            return None, "reject", "Username бота"
        return "https://t.me/" + name.lower(), None, ""
    except (ValueError, UnicodeError):
        return None, "review", "Некорректная ссылка Telegram"


def canonical_telegram(raw: str) -> str | None:
    """Normalize an explicit ordinary user URL; never invent usernames."""
    canonical, mandatory, _ = _link(raw)
    return canonical if mandatory is None else None


def _is_telegram(raw: str) -> bool:
    candidate = raw.strip().lower()
    if candidate.startswith("tg:"):
        return True
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif re.match(r"^(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", candidate):
        candidate = "https://" + candidate
    try:
        return (urlsplit(candidate).hostname or "").removeprefix("www.") in HOSTS
    except ValueError:
        return False


def _label(tag: Tag) -> str:
    labels = [tag.get_text(" ", strip=True)]
    for key in ("title", "aria-label", "data-title"):
        value = tag.get(key)
        if isinstance(value, str):
            labels.append(value)
    for img in tag.find_all("img"):
        labels.extend(str(img.get(key, "")) for key in ("alt", "title"))
    return " ".join(" ".join(labels).split())[:1000]


def _published_url(anchor: Tag) -> str | None:
    href = str(anchor.get("href", "")).strip()
    if _is_telegram(href):
        return href
    # Workspace exposes its link target explicitly while the href points to a
    # tracking redirect. Read the published URL without following the tracker.
    analytics = anchor.get("data-analytics-id", "")
    if isinstance(analytics, str) and re.match(r"^https?://", analytics, re.I) and _is_telegram(analytics):
        return analytics.strip()
    return None


def _context(anchor: Tag) -> str:
    own = _label(anchor)
    # Clear link text is the strongest evidence. In particular, the word
    # "Написать" must never override "канал" elsewhere on the same button.
    if CHANNEL.search(own) or BOT_CONTEXT.search(own):
        return own
    parent = anchor.parent
    # A small nearby block may label an icon. Do not borrow headings from a
    # footer/page or purposes from a sibling Telegram link.
    for _ in range(2):
        if not isinstance(parent, Tag) or parent.name in {
            "body", "html", "footer", "header", "nav", "main", "section", "form",
        }:
            break
        text = parent.get_text(" ", strip=True)
        telegram_links = [a for a in parent.find_all("a") if _published_url(a)]
        if len(text) > 220 or len(telegram_links) != 1:
            break
        if text and text != anchor.get_text(" ", strip=True):
            return " ".join((own + " " + text).split())[:1000]
        parent = parent.parent
    return own


def extract_telegram(html: str, page_url: str) -> list[dict]:
    """Classify published hyperlinks without visiting Telegram or resolving users.

    Every result has telegram, decision, reason and context. The caller should
    export only decision=contact, retain review separately and discard reject.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    candidates: list[tuple[str, str]] = []
    for anchor in soup.find_all("a"):
        if anchor.find_parent(["script", "style", "template", "noscript"]):
            continue
        raw = _published_url(anchor)
        if raw is None:
            continue
        candidates.append((raw, _context(anchor)))
    # Explicit visible URLs are public links too. Bare @mentions are deliberately
    # ignored; there is no attempt to reconstruct an unpublished username.
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        if node.find_parent(["a", "script", "style", "template", "noscript", "head"]):
            continue
        visible = str(node).strip()
        matches = list(VISIBLE_URL.finditer(visible))
        for match in matches:
            raw = match.group().rstrip(".,;:!?)]}»…")
            # Long paragraphs or multiple URLs cannot supply a local purpose.
            context = visible if len(visible) <= 220 and len(matches) == 1 else raw
            candidates.append((raw, context))
    for raw, context in candidates:
        canonical, decision, reason = _link(raw)
        if decision is None:
            if CHANNEL.search(context):
                decision, reason = "reject", "Локальная подпись указывает на канал / новости / подписку"
            elif BOT_CONTEXT.search(context):
                decision, reason = "reject", "Локальная подпись указывает на бота"
            elif CONTACT.search(context):
                decision, reason = "contact", "Явное локальное приглашение связаться в Telegram"
            else:
                decision, reason = "review", "Нет явного локального назначения ссылки для связи"
        # Keep the original link for anything without a valid ordinary username.
        link = canonical or urljoin(page_url, raw)
        key = (link, decision, context)
        if key not in seen:
            seen.add(key)
            result.append({"telegram": link, "decision": decision, "reason": reason, "context": context})
    return result
