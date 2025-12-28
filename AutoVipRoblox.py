from __future__ import annotations
import json
import time
import requests
import re
import logging
import threading
import os
import datetime
import random
from typing import TYPE_CHECKING, Dict, Any, List

import telebot
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B

from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
from FunPayAPI.types import OrderStatuses

from tg_bot import CBT

if TYPE_CHECKING:
    from cardinal import Cardinal

NAME = "AutoVIP Roblox"
VERSION = "v10.13"
DESCRIPTION = "Автоматическая аренда VIP-Server Roblox"
CREDITS = "@qorexdev лучший кодер фанпей коммьюнити btw"
UUID = "75e4241f-128a-4cd7-bad6-7e67961fced7"

# ВНИМАНИЕ! Eсли вы купили этот плагин — вас обманули.
# Плагин полностью бесплатный и распространяется по лицензии CC BY-NC-SA 4.0.
# Продажа запрещена. Если вам его продали — напишите мне в Telegram: @qorexdev
LICENSE_WARNING = "⚠️ Плагин БЕСПЛАТНЫЙ. Продажа ЗАПРЕЩЕНА! Купили данный плагин? Сообщите @qorexdev"
SETTINGS_PAGE = True

PLUGIN_NAME_LOWER = "autoroblox"
logger = logging.getLogger(f"FPC.{PLUGIN_NAME_LOWER}_plugin")
LOGGER_PREFIX = f"[{NAME.upper()}]"
FUNPAY_SUBCATEGORY_ID = 3503

PLUGIN_DIR = f"storage/plugins/{PLUGIN_NAME_LOWER}"
os.makedirs(PLUGIN_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
ACTIVE_LINKS_FILE = os.path.join(PLUGIN_DIR, "active_links.json")
LOTS_CONFIG_FILE = os.path.join(PLUGIN_DIR, "auto_roblox_lots.json")

DEFAULT_TEMPLATES = {
    "search": "🔍 Спасибо за покупку! Ищу свободный сервер для тебя...",
    "generating": "✨ Нашёл! Генерирую твою персональную ссылку...",
    "success": """🎉 Готово! Твой VIP-сервер активирован!

🔗 Ссылка для входа:
{link}

⏰ Время аренды: {duration}
📅 Активен до: {expiry} (МСК)

{bonus_info}
💫 Не забудь подтвердить заказ после проверки!
👉 https://funpay.com/orders/{order_id}/

Приятной игры! 🎮""",
    "error": "😔 Упс, что-то пошло не так. Продавец уже в курсе и скоро напишет тебе!",
    "no_servers": "😅 Все серверы сейчас заняты. Продавец уведомлён и скоро свяжется!",
    "expiry": """👋 Привет! Время аренды по заказу #{order_id} закончилось.

Надеюсь, тебе всё понравилось! 😊
Если всё ок — подтверди заказ, буду очень благодарен! 🙏""",
    "renewal": """✅ Аренда продлена!

⏰ Добавлено: {duration}
📅 Теперь активен до: {expiry} (МСК)

Спасибо, что остаёшься с нами! 💜""",
    "bonus_review": "🎁 Оставь отзыв 5⭐ и получи +{bonus_time} к аренде!",
    "bonus_promo": "🎁 Бонус за покупку {qty} шт: +{hours}ч к аренде!"
}

DEFAULT_SETTINGS = {
    "roblox_accounts": {},
    "sales_enabled": True,
    "notification_chats": [],
    "notifications_enabled": True,
    "auto_toggle_lots": True,
    "blacklist": [],
    "promotions": {"enabled": False, "quantity_required": 5, "bonus_hours": 1},
    "review_bonus": {"enabled": False, "bonus_time_str": "1h", "min_rating": 5},
    "display": {"show_server_name": False, "timezone": "МСК"},
    "templates": DEFAULT_TEMPLATES.copy()
}

DEFAULT_LOTS_CONFIG = {"lot_mapping": {}}

SETTINGS: Dict[str, Any] = {}
LOTS_CONFIG: Dict[str, Any] = {}
roblox_api_instances: Dict[str, 'RobloxAPI'] = {}
tg = None
bot: telebot.TeleBot = None
cardinal_instance: 'Cardinal' = None
stop_expiration_checker = threading.Event()


class RobloxAPI:
    BASE_URL = "https://games.roblox.com"
    
    def __init__(self, cookie: str, account_id: str = None):
        self.account_id = account_id
        self.session = requests.Session()
        self.session.cookies[".ROBLOSECURITY"] = cookie
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.csrf_token = None
        self._refresh_csrf()
    
    def _refresh_csrf(self) -> bool:
        for _ in range(3):
            try:
                resp = self.session.post("https://auth.roblox.com/v2/logout", timeout=10)
                if "x-csrf-token" in resp.headers:
                    self.csrf_token = resp.headers["x-csrf-token"]
                    self.session.headers["X-CSRF-TOKEN"] = self.csrf_token
                    return True
            except Exception as e:
                logger.warning(f"{LOGGER_PREFIX} CSRF error: {e}")
                time.sleep(1)
        return False
    
    def _request(self, method: str, url: str, retries: int = 3, **kwargs) -> requests.Response | None:
        kwargs.setdefault('timeout', 15)
        for attempt in range(retries):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 403 and "x-csrf-token" in resp.headers:
                    if self._refresh_csrf():
                        continue
                    return None
                if resp.status_code == 401:
                    logger.error(f"{LOGGER_PREFIX} Cookie expired")
                    return None
                return resp
            except requests.exceptions.RequestException as e:
                logger.warning(f"{LOGGER_PREFIX} Request error: {e}")
                if attempt < retries - 1:
                    time.sleep(2)
        return None
    
    def get_user_info(self) -> Dict | None:
        resp = self._request("GET", "https://users.roblox.com/v1/users/authenticated")
        return resp.json() if resp and resp.status_code == 200 else None
    
    def get_private_servers(self, place_id: int) -> List[Dict] | None:
        url = f"{self.BASE_URL}/v1/games/{place_id}/private-servers"
        all_servers, cursor = [], None
        for _ in range(10):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = self._request("GET", url, params=params)
            if not resp or resp.status_code != 200:
                return None
            data = resp.json()
            all_servers.extend(data.get("data", []))
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        return all_servers
    
    def regenerate_link(self, vip_server_id: int) -> str | None:
        url = f"{self.BASE_URL}/v1/vip-servers/{vip_server_id}"
        resp = self._request("PATCH", url, json={"newJoinCode": True})
        if not resp or resp.status_code != 200:
            return None
        try:
            link = resp.json().get("link", "")
            if not link:
                return None
            for pattern in [r'privateServerLinkCode=([a-zA-Z0-9]+)', r'code=([a-fA-F0-9]+)']:
                match = re.search(pattern, link)
                if match:
                    return match.group(1)
            return link if "roblox.com" in link else None
        except:
            return None
    
    def set_server_active(self, vip_server_id: int, active: bool) -> bool:
        url = f"{self.BASE_URL}/v1/vip-servers/{vip_server_id}"
        resp = self._request("PATCH", url, json={"active": active})
        return resp is not None and resp.status_code == 200


def load_settings():
    global SETTINGS
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                SETTINGS = json.load(f)
        else:
            SETTINGS = DEFAULT_SETTINGS.copy()
    except:
        SETTINGS = DEFAULT_SETTINGS.copy()
    
    for key, value in DEFAULT_SETTINGS.items():
        if key not in SETTINGS:
            SETTINGS[key] = value
        elif isinstance(value, dict) and isinstance(SETTINGS.get(key), dict):
            for sub_key, sub_value in value.items():
                SETTINGS[key].setdefault(sub_key, sub_value)


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, indent=4, ensure_ascii=False)


def load_lots_config():
    global LOTS_CONFIG
    try:
        if os.path.exists(LOTS_CONFIG_FILE):
            with open(LOTS_CONFIG_FILE, "r", encoding="utf-8") as f:
                LOTS_CONFIG = json.load(f)
        else:
            LOTS_CONFIG = DEFAULT_LOTS_CONFIG.copy()
    except:
        LOTS_CONFIG = DEFAULT_LOTS_CONFIG.copy()
    if "lot_mapping" not in LOTS_CONFIG:
        LOTS_CONFIG["lot_mapping"] = {}


def save_lots_config():
    with open(LOTS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(LOTS_CONFIG, f, indent=4, ensure_ascii=False)


def load_active_links() -> List[Dict]:
    try:
        if os.path.exists(ACTIVE_LINKS_FILE):
            with open(ACTIVE_LINKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return []


def save_active_links(links: List[Dict]):
    with open(ACTIVE_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, indent=4, ensure_ascii=False)


def get_api(account_id: str) -> RobloxAPI | None:
    account_id = str(account_id)
    if account_id in roblox_api_instances:
        api = roblox_api_instances[account_id]
        if api.get_user_info():
            return api
        del roblox_api_instances[account_id]
    
    account_data = SETTINGS.get("roblox_accounts", {}).get(account_id)
    if not account_data or "cookie" not in account_data:
        return None
    
    try:
        api = RobloxAPI(account_data["cookie"], account_id)
        if api.get_user_info():
            roblox_api_instances[account_id] = api
            return api
    except:
        pass
    return None


def parse_duration(duration_str: str) -> int | None:
    if not isinstance(duration_str, str):
        return None
    match = re.search(r'(\d+)\s*(m|h)', duration_str, re.IGNORECASE)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2).lower()
    return value * 60 if unit == 'm' else value * 3600


def format_duration(duration_str: str) -> str:
    match = re.search(r'(\d+)\s*(m|h)', duration_str, re.IGNORECASE)
    if not match:
        return duration_str
    value, unit = int(match.group(1)), match.group(2).lower()
    
    def plural(n, one, two, five):
        if n % 10 == 1 and n % 100 != 11:
            return one
        if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
            return two
        return five
    
    if unit == 'm':
        return f"{value} {plural(value, 'минуту', 'минуты', 'минут')}"
    return f"{value} {plural(value, 'час', 'часа', 'часов')}"


def get_template(key: str) -> str:
    return SETTINGS.get("templates", {}).get(key) or DEFAULT_TEMPLATES.get(key, "")


def render_message(key: str, **kwargs) -> str:
    template = get_template(key)
    kwargs.setdefault("bonus_info", "")
    try:
        return template.format(**kwargs)
    except:
        return template


def get_server_config(order_description: str) -> Dict | None:
    lot_mapping = LOTS_CONFIG.get("lot_mapping", {})
    best_match, max_len = None, 0
    for lot_data in lot_mapping.values():
        config_name = lot_data.get("name", "").strip()
        if config_name and config_name in order_description.strip():
            if len(config_name) > max_len:
                max_len = len(config_name)
                best_match = lot_data
    return best_match


def send_tg(message: str):
    if not SETTINGS.get("notifications_enabled"):
        return
    for chat_id in SETTINGS.get("notification_chats", []):
        try:
            bot.send_message(chat_id, message, parse_mode="HTML", disable_web_page_preview=True)
        except:
            pass


def toggle_lot(cardinal: 'Cardinal', lot_id: int, active: bool) -> bool:
    if not SETTINGS.get("auto_toggle_lots"):
        return True
    try:
        fields = cardinal.account.get_lot_fields(lot_id)
        if fields.active == active:
            return True
        fields.active = active
        cardinal.account.save_lot(fields)
        return True
    except:
        return False


def handle_new_order(cardinal: 'Cardinal', event: NewOrderEvent):
    order = event.order
    order_id = order.id
    
    if order.status == OrderStatuses.CLOSED or not SETTINGS.get("sales_enabled"):
        return
    if order.status != OrderStatuses.PAID or order.subcategory.id != FUNPAY_SUBCATEGORY_ID:
        return
    
    try:
        full_order = cardinal.account.get_order(order.id)
        chat_id = full_order.chat_id
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Order error: {e}")
        send_tg(f"❌ Ошибка заказа <code>#{order_id}</code>")
        return
    
    buyer = order.buyer_username
    if buyer in SETTINGS.get("blacklist", []):
        send_tg(f"🚫 {buyer} в ЧС, заказ <code>#{order_id}</code>")
        return
    
    server_config = get_server_config(order.description)
    if not server_config:
        return
    
    base_duration = parse_duration(server_config.get("time", "0m"))
    if not base_duration:
        cardinal.send_message(chat_id, render_message("error"))
        return
    
    quantity = order.amount if isinstance(order.amount, int) and order.amount > 0 else 1
    duration_sec = base_duration * quantity
    
    active_links = load_active_links()
    
    renewal = next((l for l in active_links if l.get("buyer_username") == buyer and l.get("awaiting_renewal")), None)
    if renewal:
        idx = active_links.index(renewal)
        new_expires = renewal["expires_at"] + duration_sec
        active_links[idx]["expires_at"] = new_expires
        active_links[idx].pop("awaiting_renewal", None)
        save_active_links(active_links)
        
        tz = SETTINGS.get("display", {}).get("timezone", "МСК")
        expiry_str = datetime.datetime.fromtimestamp(new_expires).strftime('%d.%m.%Y %H:%M')
        duration_str = str(datetime.timedelta(seconds=duration_sec))
        
        cardinal.send_message(chat_id, render_message("renewal", duration=duration_str, expiry=f"{expiry_str} ({tz})"))
        send_tg(f"🔄 Продление: {buyer} +{duration_str}")
        return
    
    server_pool = server_config.get("servers", [])
    if not server_pool:
        cardinal.send_message(chat_id, render_message("error"))
        return
    
    cardinal.send_message(chat_id, render_message("search"))
    
    rented_names = {l['server_name'] for l in active_links}
    free_server = next((s for s in server_pool if s.get("vipname") not in rented_names), None)
    
    if not free_server:
        cardinal.send_message(chat_id, render_message("no_servers"))
        toggle_lot(cardinal, server_config.get("lot_id"), False)
        send_tg(f"❌ Нет серверов для #{order_id}")
        return
    
    place_id = int(free_server["vipgame"])
    server_name = free_server["vipname"]
    account_id = free_server.get("account_id")
    
    api = get_api(account_id) if account_id else None
    if not api:
        cardinal.send_message(chat_id, render_message("error"))
        send_tg(f"❌ API ошибка #{order_id}")
        return
    
    servers = api.get_private_servers(place_id)
    target_server = next((s for s in (servers or []) if s.get("name") == server_name), None)
    
    if not target_server:
        cardinal.send_message(chat_id, render_message("error"))
        send_tg(f"❌ Сервер {server_name} не найден")
        return
    
    vip_server_id = target_server.get("vipServerId")
    if not vip_server_id:
        cardinal.send_message(chat_id, render_message("error"))
        return
    
    cardinal.send_message(chat_id, render_message("generating"))
    time.sleep(1)
    
    link_code = api.regenerate_link(vip_server_id)
    if not link_code:
        cardinal.send_message(chat_id, render_message("error"))
        send_tg(f"❌ Ошибка ссылки для #{order_id}")
        return
    
    link = link_code if link_code.startswith("http") else f"https://www.roblox.com/share?code={link_code}&type=Server"
    
    bonus_info = ""
    promo = SETTINGS.get("promotions", {})
    if promo.get("enabled") and quantity >= promo.get("quantity_required", 5):
        bonus_hours = promo.get("bonus_hours", 1)
        duration_sec += bonus_hours * 3600
        bonus_info = render_message("bonus_promo", qty=quantity, hours=bonus_hours) + "\n"
    
    review_bonus = SETTINGS.get("review_bonus", {})
    if review_bonus.get("enabled"):
        bonus_time = format_duration(review_bonus.get("bonus_time_str", "1h"))
        bonus_info += render_message("bonus_review", bonus_time=bonus_time) + "\n"
    
    expires_at = int(time.time()) + duration_sec
    tz = SETTINGS.get("display", {}).get("timezone", "МСК")
    
    active_links.append({
        "order_id": order_id, "buyer_username": buyer, "vip_server_id": vip_server_id,
        "server_name": server_name, "issued_at": int(time.time()), "expires_at": expires_at,
        "lot_id": server_config.get("lot_id"), "chat_id": chat_id, "account_id": account_id
    })
    save_active_links(active_links)
    
    duration_str = str(datetime.timedelta(seconds=duration_sec))
    expiry_str = datetime.datetime.fromtimestamp(expires_at).strftime('%d.%m.%Y %H:%M')
    
    cardinal.send_message(chat_id, render_message("success",
        link=link, duration=duration_str, expiry=f"{expiry_str} ({tz})",
        order_id=order_id, bonus_info=bonus_info))
    
    send_tg(f"✅ Выдано: {buyer} #{order_id}")
    
    current_rented = {l['server_name'] for l in active_links}
    if not any(s.get("vipname") not in current_rented for s in server_pool):
        toggle_lot(cardinal, server_config.get("lot_id"), False)


def check_expirations():
    while not stop_expiration_checker.is_set():
        try:
            if not cardinal_instance:
                stop_expiration_checker.wait(30)
                continue
            
            active_links = load_active_links()
            now = int(time.time())
            expired = [l for l in active_links if l["expires_at"] <= now]
            
            if expired:
                remaining = [l for l in active_links if l["expires_at"] > now]
                for link in expired:
                    api = get_api(link.get("account_id"))
                    if api:
                        vip_id = link["vip_server_id"]
                        if api.set_server_active(vip_id, False):
                            time.sleep(2)
                            api.set_server_active(vip_id, True)
                            api.regenerate_link(vip_id)
                        
                        lot_id = link.get("lot_id")
                        if lot_id:
                            toggle_lot(cardinal_instance, lot_id, True)
                    
                    chat_id = link.get("chat_id")
                    if chat_id:
                        try:
                            cardinal_instance.send_message(chat_id, render_message("expiry", order_id=link["order_id"]))
                        except:
                            pass
                
                save_active_links(remaining)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Expiration error: {e}")
        
        stop_expiration_checker.wait(60)


def handle_renewal_command(cardinal: 'Cardinal', event: NewMessageEvent):
    msg = event.message
    if not msg.text or msg.author == "FunPay" or msg.text.lower() != "!продление":
        return
    
    author = msg.author
    buyer = author.username if hasattr(author, 'username') else str(author)
    
    active_links = load_active_links()
    rental = next((l for l in active_links if l.get("buyer_username") == buyer), None)
    
    if not rental:
        cardinal.send_message(msg.chat_id, "😔 Не нашёл активной аренды для продления.")
        return
    
    if rental.get("awaiting_renewal"):
        cardinal.send_message(msg.chat_id, "⏳ Лот уже активирован! Просто купи его для продления.")
        return
    
    lot_id = rental.get("lot_id")
    if lot_id and toggle_lot(cardinal, lot_id, True):
        for i, l in enumerate(active_links):
            if l.get("buyer_username") == buyer:
                active_links[i]["awaiting_renewal"] = True
                break
        save_active_links(active_links)
        cardinal.send_message(msg.chat_id, "✅ Лот активирован! Купи его для продления аренды.")
        send_tg(f"🔄 {buyer} запросил продление")
    else:
        cardinal.send_message(msg.chat_id, render_message("error"))


def build_menu(chat_id: int):
    if chat_id not in SETTINGS.get("notification_chats", []):
        SETTINGS.setdefault("notification_chats", []).append(chat_id)
        save_settings()
    
    accounts = SETTINGS.get("roblox_accounts", {})
    active = load_active_links()
    lot_mapping = LOTS_CONFIG.get("lot_mapping", {})
    
    text = f"""🎮 <b>{NAME}</b> <code>{VERSION}</code>
━━━━━━━━━━━━━━━━━━━━
<b>{LICENSE_WARNING}</b>

"""
    
    if accounts:
        text += "👤 <b>Аккаунты Roblox:</b>\n"
        for acc in accounts.values():
            text += f"   └ <code>{acc.get('name')}</code> ✅\n"
    else:
        text += "⚠️ <b>Аккаунты не подключены!</b>\n"
    
    text += f"\n📊 <b>Статистика:</b>\n   └ Активных аренд: <code>{len(active)}</code>\n\n"
    
    if lot_mapping:
        text += "📦 <b>Пулы серверов:</b>\n"
        rented = {l['server_name'] for l in active}
        for lot in lot_mapping.values():
            pool = lot.get("servers", [])
            total = len(pool)
            free = sum(1 for s in pool if s.get("vipname") not in rented)
            emoji = "🟢" if free > 0 else "🔴"
            text += f"   └ {emoji} {lot.get('name', 'N/A')}: {free}/{total}\n"
    
    kb = K(row_width=2)
    
    sales = "🟢 Автовыдача" if SETTINGS.get("sales_enabled") else "🔴 Автовыдача"
    notif = "🔔" if SETTINGS.get("notifications_enabled") else "🔕"
    auto_lot = "🟢 Авто-лоты" if SETTINGS.get("auto_toggle_lots") else "🔴 Авто-лоты"
    
    kb.add(B("👤 Аккаунты", callback_data="arp_accounts"), B("📦 Лоты", callback_data="arp_lots_menu"))
    kb.add(B(f"📋 Аренды ({len(active)})", callback_data="arp_rentals"), B(f"{sales}", callback_data="arp_toggle_sales"))
    kb.add(B(f"{auto_lot}", callback_data="arp_toggle_auto_lots"), B(f"{notif} Уведомления", callback_data="arp_toggle_notif"))
    kb.add(B("🎁 Бонусы", callback_data="arp_bonuses"), B("✏️ Шаблоны", callback_data="arp_templates"))
    kb.add(B("⚙️ Настройки", callback_data="arp_display"), B("🚫 ЧС", callback_data="arp_blacklist"))
    kb.add(B("👨‍💻 Автор", callback_data="arp_author"), B("🔄 Обновить", callback_data="arp_refresh"))
    kb.add(B("◀️ Назад", callback_data=f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    
    return text, kb


def templates_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    text = """✏️ <b>Редактор шаблонов</b>
━━━━━━━━━━━━━━━━━━━━

Здесь можно настроить сообщения для покупателей.

<b>Доступные переменные:</b>
• <code>{link}</code> — ссылка на сервер
• <code>{duration}</code> — время аренды
• <code>{expiry}</code> — дата окончания
• <code>{order_id}</code> — номер заказа
• <code>{bonus_info}</code> — инфо о бонусах"""
    
    kb = K(row_width=1)
    template_names = {
        "search": "🔍 Поиск сервера",
        "generating": "✨ Генерация ссылки", 
        "success": "🎉 Успешная выдача",
        "error": "😔 Ошибка",
        "no_servers": "😅 Нет серверов",
        "expiry": "👋 Окончание аренды",
        "renewal": "✅ Продление"
    }
    
    for key, name in template_names.items():
        kb.add(B(name, callback_data=f"arp_tpl_edit:{key}"))
    
    kb.add(B("🔄 Сбросить все", callback_data="arp_tpl_reset"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def edit_template(call, cardinal):
    key = call.data.split(":")[1]
    current = get_template(key)
    
    text = f"Текущий шаблон <b>{key}</b>:\n\n<code>{current[:500]}</code>\n\nОтправь новый текст:"
    
    kb = K().add(B("❌ Отмена", callback_data="arp_templates"))
    msg = bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="HTML")
    bot.register_next_step_handler(msg, on_template_received, key)
    bot.answer_callback_query(call.id)


def on_template_received(message, key):
    SETTINGS.setdefault("templates", DEFAULT_TEMPLATES.copy())[key] = message.text
    save_settings()
    bot.send_message(message.chat.id, "✅ Шаблон сохранён!")
    handle_command(message)


def reset_templates(call, cardinal):
    SETTINGS["templates"] = DEFAULT_TEMPLATES.copy()
    save_settings()
    bot.answer_callback_query(call.id, "✅ Шаблоны сброшены")
    templates_menu(call, cardinal)


def author_menu(call, cardinal):
    text = """👨‍💻 <b>Об авторе</b>
━━━━━━━━━━━━━━━━━━━━

Привет! Меня зовут <b>qorexdev</b> 👋

Я разрабатываю плагины и инструменты для FunPay.
Если у тебя есть вопросы, предложения или нужна помощь — пиши!

📱 <b>Telegram:</b> @qorexdev
🐙 <b>GitHub:</b> github.com/qorexdev

━━━━━━━━━━━━━━━━━━━━

⭐ <b>FunPay Sigma</b> — улучшенный форк Cardinal!
Больше фич, стабильнее работа, активная поддержка.

🚀 Переходи на Sigma:
github.com/qorexdev/FunPaySigma

⚠️ <b>ВНИМАНИЕ:</b>
Этот плагин — <b>БЕСПЛАТНЫЙ</b>.
Если тебе его продали, значит тебя обманули.
Срочно напиши мне в ЛС!

Спасибо, что пользуешься моими работами! 💜"""
    
    kb = K(row_width=1)
    kb.add(B("📱 Написать в Telegram", url="https://t.me/qorexdev"))
    kb.add(B("🐙 GitHub", url="https://github.com/qorexdev"))
    kb.add(B("⭐ FunPay Sigma", url="https://github.com/qorexdev/FunPaySigma"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    bot.answer_callback_query(call.id)


def rentals_menu(call, cardinal, page=0):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    active_links = load_active_links()
    
    text = f"""📋 <b>Управление арендами</b>
━━━━━━━━━━━━━━━━━━━━

Всего активных: <code>{len(active_links)}</code>

"""
    
    if not active_links:
        text += "<i>Нет активных аренд</i>"
    
    kb = K(row_width=1)
    
    per_page = 5
    start = page * per_page
    end = start + per_page
    page_items = active_links[start:end]
    
    now = int(time.time())
    tz = SETTINGS.get("display", {}).get("timezone", "МСК")
    
    for i, rental in enumerate(page_items):
        idx = start + i
        buyer = rental.get("buyer_username", "?")
        expires = rental.get("expires_at", 0)
        remaining = expires - now
        
        if remaining > 0:
            hours = remaining // 3600
            mins = (remaining % 3600) // 60
            time_left = f"{hours}ч {mins}м"
        else:
            time_left = "истекла"
        
        kb.add(B(f"👤 {buyer} | ⏰ {time_left}", callback_data=f"arp_rental:{idx}"))
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(B("⬅️", callback_data=f"arp_rentals_page:{page-1}"))
    if end < len(active_links):
        nav_buttons.append(B("➡️", callback_data=f"arp_rentals_page:{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)
    
    kb.add(B("🗑️ Завершить все", callback_data="arp_rentals_clear_confirm"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def rentals_page(call, cardinal):
    page = int(call.data.split(":")[1])
    rentals_menu(call, cardinal, page)


def rental_details(call, cardinal):
    idx = int(call.data.split(":")[1])
    active_links = load_active_links()
    
    if idx >= len(active_links):
        bot.answer_callback_query(call.id, "❌ Аренда не найдена", show_alert=True)
        return rentals_menu(call, cardinal)
    
    rental = active_links[idx]
    now = int(time.time())
    tz = SETTINGS.get("display", {}).get("timezone", "МСК")
    
    buyer = rental.get("buyer_username", "?")
    order_id = rental.get("order_id", "?")
    server_name = rental.get("server_name", "?")
    issued = datetime.datetime.fromtimestamp(rental.get("issued_at", 0)).strftime('%d.%m.%Y %H:%M')
    expires = rental.get("expires_at", 0)
    expires_str = datetime.datetime.fromtimestamp(expires).strftime('%d.%m.%Y %H:%M')
    
    remaining = expires - now
    if remaining > 0:
        hours = remaining // 3600
        mins = (remaining % 3600) // 60
        time_left = f"{hours}ч {mins}м"
        status = "🟢 Активна"
    else:
        time_left = "0"
        status = "🔴 Истекла"
    
    text = f"""📋 <b>Детали аренды</b>
━━━━━━━━━━━━━━━━━━━━

👤 <b>Покупатель:</b> <code>{buyer}</code>
🆔 <b>Заказ:</b> <code>#{order_id}</code>
🖥️ <b>Сервер:</b> <code>{server_name}</code>

📅 <b>Выдано:</b> {issued} ({tz})
📅 <b>Истекает:</b> {expires_str} ({tz})
⏰ <b>Осталось:</b> {time_left}

<b>Статус:</b> {status}"""
    
    kb = K(row_width=1)
    kb.add(B("🛑 Завершить аренду", callback_data=f"arp_rental_end_confirm:{idx}"))
    kb.add(B("◀️ К списку", callback_data="arp_rentals"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def rental_end_confirm(call, cardinal):
    idx = int(call.data.split(":")[1])
    
    text = """⚠️ <b>Подтверждение</b>

Ты уверен, что хочешь завершить эту аренду досрочно?

Сервер будет перезапущен и освобождён для новых покупателей."""
    
    kb = K(row_width=2)
    kb.add(B("✅ Да, завершить", callback_data=f"arp_rental_end:{idx}"))
    kb.add(B("❌ Отмена", callback_data=f"arp_rental:{idx}"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def rental_end(call, cardinal):
    idx = int(call.data.split(":")[1])
    active_links = load_active_links()
    
    if idx >= len(active_links):
        bot.answer_callback_query(call.id, "❌ Аренда не найдена", show_alert=True)
        return rentals_menu(call, cardinal)
    
    rental = active_links[idx]
    server_name = rental.get("server_name", "?")
    buyer = rental.get("buyer_username", "?")
    account_id = rental.get("account_id")
    vip_server_id = rental.get("vip_server_id")
    lot_id = rental.get("lot_id")
    
    api = get_api(account_id) if account_id else None
    if api and vip_server_id:
        if api.set_server_active(vip_server_id, False):
            time.sleep(1)
            api.set_server_active(vip_server_id, True)
            api.regenerate_link(vip_server_id)
    
    if lot_id:
        toggle_lot(cardinal, lot_id, True)
    
    active_links.pop(idx)
    save_active_links(active_links)
    
    send_tg(f"🛑 Аренда {server_name} ({buyer}) завершена вручную")
    
    bot.answer_callback_query(call.id, f"✅ Аренда {server_name} завершена")
    rentals_menu(call, cardinal)


def rentals_clear_confirm(call, cardinal):
    active_links = load_active_links()
    
    text = f"""⚠️ <b>Подтверждение</b>

Ты уверен, что хочешь завершить ВСЕ аренды ({len(active_links)} шт)?

Все серверы будут перезапущены и освобождены."""
    
    kb = K(row_width=2)
    kb.add(B("✅ Да, завершить все", callback_data="arp_rentals_clear"))
    kb.add(B("❌ Отмена", callback_data="arp_rentals"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def rentals_clear(call, cardinal):
    active_links = load_active_links()
    count = len(active_links)
    
    for rental in active_links:
        account_id = rental.get("account_id")
        vip_server_id = rental.get("vip_server_id")
        lot_id = rental.get("lot_id")
        
        api = get_api(account_id) if account_id else None
        if api and vip_server_id:
            try:
                api.set_server_active(vip_server_id, False)
                time.sleep(0.5)
                api.set_server_active(vip_server_id, True)
                api.regenerate_link(vip_server_id)
            except:
                pass
        
        if lot_id:
            toggle_lot(cardinal, lot_id, True)
    
    save_active_links([])
    
    send_tg(f"🛑 Все аренды ({count} шт) завершены вручную")
    
    bot.answer_callback_query(call.id, f"✅ Завершено {count} аренд")
    rentals_menu(call, cardinal)

def display_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    display = SETTINGS.get("display", {})
    show_name = "✅" if display.get("show_server_name") else "❌"
    tz = display.get("timezone", "МСК")
    
    text = f"""⚙️ <b>Настройки отображения</b>
━━━━━━━━━━━━━━━━━━━━

{show_name} Показывать имя сервера покупателю
🕐 Часовой пояс: <code>{tz}</code>"""
    
    kb = K(row_width=1)
    kb.add(B(f"{show_name} Имя сервера", callback_data="arp_toggle_show_name"))
    kb.add(B(f"🕐 Часовой пояс: {tz}", callback_data="arp_set_tz"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def toggle_show_name(call, cardinal):
    SETTINGS.setdefault("display", {})["show_server_name"] = not SETTINGS.get("display", {}).get("show_server_name", False)
    save_settings()
    display_menu(call, cardinal)


def set_timezone(call, cardinal):
    kb = K(row_width=3)
    timezones = ["МСК", "UTC", "UTC+1", "UTC+2", "UTC+3", "UTC+4", "UTC+5", "UTC+6"]
    for tz in timezones:
        kb.add(B(tz, callback_data=f"arp_tz:{tz}"))
    kb.add(B("◀️ Отмена", callback_data="arp_display"))
    
    bot.edit_message_text("🕐 Выбери часовой пояс:", call.message.chat.id, call.message.id, reply_markup=kb)
    bot.answer_callback_query(call.id)


def on_timezone_selected(call, cardinal):
    tz = call.data.split(":")[1]
    SETTINGS.setdefault("display", {})["timezone"] = tz
    save_settings()
    bot.answer_callback_query(call.id, f"✅ Часовой пояс: {tz}")
    display_menu(call, cardinal)


def bonuses_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    promo = SETTINGS.get("promotions", {})
    review = SETTINGS.get("review_bonus", {})
    
    promo_status = "🟢" if promo.get("enabled") else "🔴"
    review_status = "🟢" if review.get("enabled") else "🔴"
    
    text = f"""🎁 <b>Настройка бонусов</b>
━━━━━━━━━━━━━━━━━━━━

<b>Акция за количество:</b>
{promo_status} Статус: {'Включена' if promo.get('enabled') else 'Выключена'}
📦 Мин. кол-во: <code>{promo.get('quantity_required', 5)}</code> шт
⏰ Бонус: <code>{promo.get('bonus_hours', 1)}</code>ч

<b>Бонус за отзыв:</b>
{review_status} Статус: {'Включён' if review.get('enabled') else 'Выключен'}
⏰ Бонус: <code>{review.get('bonus_time_str', '1h')}</code>
⭐ Мин. оценка: <code>{review.get('min_rating', 5)}</code>"""
    
    kb = K(row_width=2)
    kb.add(B(f"{promo_status} Акция", callback_data="arp_promo_toggle"), B("📦 Кол-во", callback_data="arp_promo_qty"))
    kb.add(B("⏰ Часы акции", callback_data="arp_promo_hrs"))
    kb.add(B(f"{review_status} Отзыв", callback_data="arp_review_toggle"), B("⏰ Время", callback_data="arp_review_time"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def handle_command(message):
    text, kb = build_menu(message.chat.id)
    bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="HTML")


def open_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    text, kb = build_menu(call.message.chat.id)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    except:
        pass
    bot.answer_callback_query(call.id)


def toggle_setting(call, cardinal, key):
    SETTINGS[key] = not SETTINGS.get(key, False)
    save_settings()
    open_menu(call, cardinal)


def accounts_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    accounts = SETTINGS.get("roblox_accounts", {})
    text = "👤 <b>Аккаунты Roblox</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "Здесь можно добавлять и удалять аккаунты."
    
    kb = K(row_width=1)
    for acc_id, acc in accounts.items():
        kb.add(B(f"❌ {acc.get('name')}", callback_data=f"arp_del_acc:{acc_id}"))
    kb.add(B("➕ Добавить аккаунт", callback_data="arp_add_acc"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def add_account_start(call, cardinal):
    kb = K().add(B("❌ Отмена", callback_data="arp_accounts"))
    msg = bot.send_message(call.message.chat.id, "🔑 Отправь <b>.ROBLOSECURITY</b> cookie:", reply_markup=kb, parse_mode="HTML")
    bot.register_next_step_handler(msg, on_cookie_received)
    bot.answer_callback_query(call.id)


def on_cookie_received(message):
    try:
        bot.delete_message(message.chat.id, message.id)
    except:
        pass
    
    cookie = message.text.strip()
    try:
        api = RobloxAPI(cookie)
        info = api.get_user_info()
        if not info:
            raise ValueError()
        
        user_id = str(info["id"])
        SETTINGS.setdefault("roblox_accounts", {})[user_id] = {"cookie": cookie, "id": info["id"], "name": info["name"]}
        roblox_api_instances[user_id] = api
        save_settings()
        bot.send_message(message.chat.id, f"✅ Аккаунт <b>{info['name']}</b> добавлен!", parse_mode="HTML")
    except:
        bot.send_message(message.chat.id, "❌ Cookie невалиден или истёк")
    
    handle_command(message)


def delete_account(call, cardinal):
    acc_id = call.data.split(":")[1]
    if acc_id in SETTINGS.get("roblox_accounts", {}):
        del SETTINGS["roblox_accounts"][acc_id]
        roblox_api_instances.pop(acc_id, None)
        save_settings()
    accounts_menu(call, cardinal)


def lots_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    text = "📦 <b>Привязка лотов</b>\n━━━━━━━━━━━━━━━━━━━━"
    kb = K(row_width=1)
    for lot_key, lot in LOTS_CONFIG.get("lot_mapping", {}).items():
        servers = len(lot.get("servers", []))
        kb.add(B(f"⚙️ {lot.get('name', 'N/A')[:30]} ({servers})", callback_data=f"arp_lot:{lot_key}"))
    kb.add(B("➕ Добавить лот", callback_data="arp_add_lot"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def add_lot_start(call, cardinal):
    kb = K().add(B("❌ Отмена", callback_data="arp_lots_menu"))
    msg = bot.send_message(call.message.chat.id, "📝 Введи ID лота с FunPay:", reply_markup=kb)
    bot.register_next_step_handler(msg, on_lot_id_received)
    bot.answer_callback_query(call.id)


def on_lot_id_received(message):
    try:
        lot_id = int(message.text)
        fields = cardinal_instance.account.get_lot_fields(lot_id)
        name = fields.fields.get("fields[summary][ru]", "Без названия")
        
        lot_key = f"lot_{int(time.time())}_{random.randint(100, 999)}"
        LOTS_CONFIG.setdefault("lot_mapping", {})[lot_key] = {"name": name, "lot_id": lot_id, "time": "1h", "servers": []}
        save_lots_config()
        bot.send_message(message.chat.id, f"✅ Лот <b>{name}</b> добавлен!", parse_mode="HTML")
    except ValueError:
        bot.send_message(message.chat.id, "❌ ID должен быть числом")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
    handle_command(message)


def edit_lot(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    lot_key = call.data.split(":")[1]
    lot = LOTS_CONFIG.get("lot_mapping", {}).get(lot_key)
    if not lot:
        return lots_menu(call, cardinal)
    
    text = f"""⚙️ <b>Настройка лота</b>
━━━━━━━━━━━━━━━━━━━━

📌 <b>Название:</b> {lot.get('name')}
⏰ <b>Время аренды:</b> <code>{lot.get('time')}</code>
📦 <b>Серверов:</b> {len(lot.get('servers', []))}"""
    
    kb = K(row_width=1)
    kb.add(B("🗂️ Управление серверами", callback_data=f"arp_pool:{lot_key}"))
    kb.add(B("⏰ Изменить время", callback_data=f"arp_lot_time:{lot_key}"))
    kb.add(B("🗑️ Удалить лот", callback_data=f"arp_lot_del:{lot_key}"))
    kb.add(B("◀️ Назад", callback_data="arp_lots_menu"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def set_lot_time(call, cardinal):
    lot_key = call.data.split(":")[1]
    kb = K().add(B("❌ Отмена", callback_data=f"arp_lot:{lot_key}"))
    msg = bot.send_message(call.message.chat.id, "⏰ Введи время (напр. <code>1h</code> или <code>30m</code>):", reply_markup=kb, parse_mode="HTML")
    bot.register_next_step_handler(msg, on_lot_time_received, lot_key)
    bot.answer_callback_query(call.id)


def on_lot_time_received(message, lot_key):
    time_str = message.text.strip()
    if parse_duration(time_str):
        LOTS_CONFIG["lot_mapping"][lot_key]["time"] = time_str
        save_lots_config()
        bot.send_message(message.chat.id, "✅ Время обновлено!")
    else:
        bot.send_message(message.chat.id, "❌ Неверный формат")
    handle_command(message)


def delete_lot(call, cardinal):
    lot_key = call.data.split(":")[1]
    LOTS_CONFIG["lot_mapping"].pop(lot_key, None)
    save_lots_config()
    lots_menu(call, cardinal)


def pool_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    lot_key = call.data.split(":")[1]
    lot = LOTS_CONFIG["lot_mapping"].get(lot_key)
    if not lot:
        return lots_menu(call, cardinal)
    
    pool = lot.get("servers", [])
    text = f"🗂️ <b>Пул серверов</b>\n<i>{lot['name']}</i>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    kb = K(row_width=1)
    if pool:
        for i, s in enumerate(pool):
            acc_name = SETTINGS.get("roblox_accounts", {}).get(str(s.get("account_id")), {}).get("name", "?")
            text += f"{i+1}. <code>{s['vipname']}</code> ({acc_name})\n"
            kb.add(B(f"🗑️ {i+1}. {s['vipname'][:20]}", callback_data=f"arp_pool_del:{lot_key}:{i}"))
    else:
        text += "<i>Пул пуст</i>"
    
    kb.add(B("➕ Добавить сервер", callback_data=f"arp_pool_add:{lot_key}"))
    kb.add(B("◀️ Назад", callback_data=f"arp_lot:{lot_key}"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def add_server_start(call, cardinal):
    lot_key = call.data.split(":")[1]
    accounts = SETTINGS.get("roblox_accounts", {})
    if not accounts:
        bot.answer_callback_query(call.id, "❌ Сначала добавь аккаунт!", show_alert=True)
        return
    
    kb = K(row_width=1)
    for acc_id, acc in accounts.items():
        kb.add(B(f"👤 {acc['name']}", callback_data=f"arp_pool_acc:{lot_key}:{acc_id}"))
    kb.add(B("❌ Отмена", callback_data=f"arp_pool:{lot_key}"))
    
    bot.edit_message_text("<b>Шаг 1/3:</b> Выбери аккаунт:", call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def add_server_game(call, cardinal):
    _, lot_key, acc_id = call.data.split(":")
    kb = K().add(B("❌ Отмена", callback_data=f"arp_pool:{lot_key}"))
    msg = bot.send_message(call.message.chat.id, "<b>Шаг 2/3:</b> Введи Place ID игры:", reply_markup=kb, parse_mode="HTML")
    bot.register_next_step_handler(msg, on_game_id_received, lot_key, acc_id)
    bot.answer_callback_query(call.id)


def on_game_id_received(message, lot_key, acc_id):
    try:
        game_id = message.text.strip()
        int(game_id)
        kb = K().add(B("❌ Отмена", callback_data=f"arp_pool:{lot_key}"))
        msg = bot.send_message(message.chat.id, "<b>Шаг 3/3:</b> Введи имя сервера:", reply_markup=kb, parse_mode="HTML")
        bot.register_next_step_handler(msg, on_server_name_received, lot_key, acc_id, game_id)
    except:
        bot.send_message(message.chat.id, "❌ ID должен быть числом")
        handle_command(message)


def on_server_name_received(message, lot_key, acc_id, game_id):
    server_name = message.text.strip()
    LOTS_CONFIG["lot_mapping"][lot_key]["servers"].append({"vipgame": game_id, "vipname": server_name, "account_id": acc_id})
    save_lots_config()
    bot.send_message(message.chat.id, "✅ Сервер добавлен!")
    handle_command(message)


def delete_server(call, cardinal):
    parts = call.data.split(":")
    lot_key, idx = parts[1], int(parts[2])
    pool = LOTS_CONFIG["lot_mapping"][lot_key].get("servers", [])
    if 0 <= idx < len(pool):
        pool.pop(idx)
        save_lots_config()
    pool_menu(call, cardinal)


def toggle_promo(call, cardinal):
    SETTINGS["promotions"]["enabled"] = not SETTINGS["promotions"].get("enabled", False)
    save_settings()
    bonuses_menu(call, cardinal)


def toggle_review(call, cardinal):
    SETTINGS["review_bonus"]["enabled"] = not SETTINGS["review_bonus"].get("enabled", False)
    save_settings()
    bonuses_menu(call, cardinal)


def set_promo_qty(call, cardinal):
    msg = bot.send_message(call.message.chat.id, "📦 Введи минимальное кол-во для бонуса:")
    bot.register_next_step_handler(msg, on_promo_value, "quantity_required")
    bot.answer_callback_query(call.id)


def set_promo_hrs(call, cardinal):
    msg = bot.send_message(call.message.chat.id, "⏰ Введи кол-во бонусных часов:")
    bot.register_next_step_handler(msg, on_promo_value, "bonus_hours")
    bot.answer_callback_query(call.id)


def on_promo_value(message, key):
    try:
        value = int(message.text)
        if value > 0:
            SETTINGS["promotions"][key] = value
            save_settings()
            bot.send_message(message.chat.id, "✅ Сохранено!")
    except:
        bot.send_message(message.chat.id, "❌ Введи число")
    handle_command(message)


def set_review_time(call, cardinal):
    msg = bot.send_message(call.message.chat.id, "⏰ Введи время бонуса (напр. <code>1h</code> или <code>30m</code>):", parse_mode="HTML")
    bot.register_next_step_handler(msg, on_review_time)
    bot.answer_callback_query(call.id)


def on_review_time(message):
    time_str = message.text.strip()
    if parse_duration(time_str):
        SETTINGS["review_bonus"]["bonus_time_str"] = time_str
        save_settings()
        bot.send_message(message.chat.id, "✅ Сохранено!")
    else:
        bot.send_message(message.chat.id, "❌ Неверный формат")
    handle_command(message)


def blacklist_menu(call, cardinal):
    try:
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
    except:
        pass
    bl = SETTINGS.get("blacklist", [])
    text = "🚫 <b>Чёрный список</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "\n".join(f"• <code>{u}</code>" for u in bl) if bl else "<i>Список пуст</i>"
    
    kb = K(row_width=2)
    kb.add(B("➕ Добавить", callback_data="arp_bl_add"), B("➖ Удалить", callback_data="arp_bl_del"))
    kb.add(B("◀️ Назад", callback_data="arp_back"))
    
    bot.edit_message_text(text, call.message.chat.id, call.message.id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(call.id)


def bl_add(call, cardinal):
    msg = bot.send_message(call.message.chat.id, "Введи никнейм для добавления в ЧС:")
    bot.register_next_step_handler(msg, on_bl_user, "add")
    bot.answer_callback_query(call.id)


def bl_del(call, cardinal):
    msg = bot.send_message(call.message.chat.id, "Введи никнейм для удаления из ЧС:")
    bot.register_next_step_handler(msg, on_bl_user, "del")
    bot.answer_callback_query(call.id)


def on_bl_user(message, action):
    username = message.text.strip()
    bl = set(SETTINGS.get("blacklist", []))
    if action == "add":
        bl.add(username)
    else:
        bl.discard(username)
    SETTINGS["blacklist"] = sorted(list(bl))
    save_settings()
    handle_command(message)


def refresh(call, cardinal):
    load_settings()
    load_lots_config()
    bot.answer_callback_query(call.id, "✅ Обновлено")
    open_menu(call, cardinal)


def init(cardinal: 'Cardinal'):
    global tg, bot, cardinal_instance
    tg, bot, cardinal_instance = cardinal.telegram, cardinal.telegram.bot, cardinal
    
    load_settings()
    load_lots_config()
    
    for acc_id in SETTINGS.get("roblox_accounts", {}):
        get_api(acc_id)
    
    threading.Thread(target=check_expirations, daemon=True).start()
    
    cardinal.add_telegram_commands(UUID, [("roblox_rent", "VIP-серверы Roblox", True)])
    tg.msg_handler(handle_command, commands=["roblox_rent"])
    
    def wrap(fn):
        def wrapper(call):
            fn(call, cardinal_instance)
        return wrapper
    
    handlers = {
        f"{CBT.PLUGIN_SETTINGS}:{UUID}": open_menu, "arp_back": open_menu, "arp_refresh": refresh,
        "arp_toggle_sales": lambda c, cd: toggle_setting(c, cd, "sales_enabled"),
        "arp_toggle_notif": lambda c, cd: toggle_setting(c, cd, "notifications_enabled"),
        "arp_toggle_auto_lots": lambda c, cd: toggle_setting(c, cd, "auto_toggle_lots"),
        "arp_accounts": accounts_menu, "arp_add_acc": add_account_start, "arp_del_acc:": delete_account,
        "arp_lots_menu": lots_menu, "arp_add_lot": add_lot_start, "arp_lot:": edit_lot,
        "arp_lot_time:": set_lot_time, "arp_lot_del:": delete_lot,
        "arp_pool:": pool_menu, "arp_pool_add:": add_server_start, "arp_pool_acc:": add_server_game, "arp_pool_del:": delete_server,
        "arp_bonuses": bonuses_menu, "arp_promo_toggle": toggle_promo, "arp_promo_qty": set_promo_qty,
        "arp_promo_hrs": set_promo_hrs, "arp_review_toggle": toggle_review, "arp_review_time": set_review_time,
        "arp_templates": templates_menu, "arp_tpl_edit:": edit_template, "arp_tpl_reset": reset_templates,
        "arp_display": display_menu, "arp_toggle_show_name": toggle_show_name, "arp_set_tz": set_timezone, "arp_tz:": on_timezone_selected,
        "arp_blacklist": blacklist_menu, "arp_bl_add": bl_add, "arp_bl_del": bl_del,
        "arp_author": author_menu,
        "arp_rentals": lambda c, cd: rentals_menu(c, cd),
        "arp_rentals_page:": rentals_page,
        "arp_rental:": rental_details,
        "arp_rental_end_confirm:": rental_end_confirm,
        "arp_rental_end:": rental_end,
        "arp_rentals_clear_confirm": rentals_clear_confirm,
        "arp_rentals_clear": rentals_clear,
    }
    
    for data, handler in handlers.items():
        tg.cbq_handler(wrap(handler), lambda c, d=data: c.data.startswith(d))
    
    handle_new_order.plugin_uuid = UUID
    handle_renewal_command.plugin_uuid = UUID
    
    if handle_new_order not in cardinal.new_order_handlers:
        cardinal.new_order_handlers.append(handle_new_order)
    if handle_renewal_command not in cardinal.new_message_handlers:
        cardinal.new_message_handlers.append(handle_renewal_command)
    
    logger.info(f"{LOGGER_PREFIX} {NAME} v{VERSION} initialized")


def on_delete():
    stop_expiration_checker.set()


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = [on_delete]