import telebot
import meshtastic.tcp_interface
from pubsub import pub
import logging
import time
import threading
import json
import queue
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, Callable, List
import sys
import random
import requests
from enum import Enum
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
import os
import hashlib
import re
import socket
import traceback

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.getLogger().setLevel(logging.ERROR)

file_handler = logging.FileHandler('gateway.log', encoding='utf-8', mode='a')
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
file_handler.setLevel(logging.ERROR)
logging.getLogger().addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console_handler.setFormatter(console_formatter)
console_handler.setLevel(logging.ERROR)
logging.getLogger().addHandler(console_handler)

logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('telebot').setLevel(logging.ERROR)
logging.getLogger('meshtastic').setLevel(logging.ERROR)

app_logger = logging.getLogger(__name__)
app_logger.setLevel(logging.ERROR)


# ==================== КЛАССЫ ====================

class RadioMode(Enum):
    """Режимы работы шлюза"""
    GATEWAY = "gateway"
    RADIO = "radio"


class PingMode(Enum):
    """Режимы ответа на ping"""
    OFF = "off"
    TEST = "test"
    AUTO = "auto"


class PendingMessage:
    """Ожидающее подтверждение сообщение"""
    def __init__(self, telegram_msg_id: int, text: str, sent_time: float, message_hash: str):
        self.telegram_msg_id = telegram_msg_id
        self.text = text
        self.sent_time = sent_time
        self.message_hash = message_hash
        self.status = 'sent'
        self.delivery_time = None
        self.delivered_at = None
        self.ack_received = False

    def mark_as_delivered(self, delivery_time: float):
        self.status = 'delivered'
        self.delivered_at = time.time()
        self.delivery_time = delivery_time
        self.ack_received = True

    def mark_as_unknown(self):
        self.status = 'unknown'
        self.delivered_at = time.time()
        self.delivery_time = self.delivered_at - self.sent_time


class UserSettings:
    """Настройки пользователя Telegram"""
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.mode = RadioMode.RADIO  # По умолчанию режим радио
        self.pending_messages: Dict[str, PendingMessage] = {}
        self.last_battery_warning = 0

    def switch_mode(self):
        self.mode = RadioMode.RADIO if self.mode == RadioMode.GATEWAY else RadioMode.GATEWAY
        return self.mode

    def set_mode(self, mode: RadioMode):
        self.mode = mode

    def add_pending_message(self, message_hash: str, telegram_msg_id: int, text: str, sent_time: float):
        self.pending_messages[message_hash] = PendingMessage(
            telegram_msg_id=telegram_msg_id,
            text=text,
            sent_time=sent_time,
            message_hash=message_hash
        )

    def get_pending_message(self, message_hash: str) -> Optional[PendingMessage]:
        return self.pending_messages.get(message_hash)

    def remove_pending_message(self, message_hash: str):
        if message_hash in self.pending_messages:
            del self.pending_messages[message_hash]

    def find_recent_messages(self, max_age: float = 30.0) -> List[Tuple[str, PendingMessage]]:
        current_time = time.time()
        recent = []
        for h, msg in self.pending_messages.items():
            if current_time - msg.sent_time < max_age:
                recent.append((h, msg))
        return recent

    def get_all_pending_messages(self) -> Dict[str, PendingMessage]:
        return self.pending_messages.copy()

    def cleanup_old_messages(self, max_age: float = 300.0):
        current_time = time.time()
        to_remove = [h for h, m in self.pending_messages.items() if current_time - m.sent_time > max_age]
        for h in to_remove:
            del self.pending_messages[h]


class TelegramReactions:
    """Управление реакциями в Telegram"""
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self.last_error_time = 0
        self.error_count = 0

    def set_reaction(self, chat_id: str, message_id: int, reaction: str) -> bool:
        try:
            if time.time() - self.last_error_time < 60 and self.error_count > 10:
                return False

            emoji_map = {
                'delivered': '👍',
                'sent': '🕊️',
                'unknown': '🤷',
                'default': '🕊️'
            }
            emoji = emoji_map.get(reaction, '🕊️')
            url = f"{self.base_url}/setMessageReaction"
            payload = {
                'chat_id': chat_id,
                'message_id': message_id,
                'reaction': [{'type': 'emoji', 'emoji': emoji}],
                'is_big': False
            }
            response = self.session.post(url, json=payload, timeout=10)
            result = response.json()
            if result.get('ok'):
                self.error_count = 0
                return True
            else:
                app_logger.error(f"Ошибка реакции: {result.get('description')}")
                self.error_count += 1
                self.last_error_time = time.time()
                return False
        except Exception as e:
            app_logger.error(f"Ошибка реакции: {e}")
            self.error_count += 1
            self.last_error_time = time.time()
            return False


class MessageTracker:
    """Отслеживание сообщений и их статусов"""
    def __init__(self):
        self.radio_messages: List[Dict] = []
        self.max_radio_history = 1000
        self.last_packet_ids: Dict[str, Dict] = {}
        self.reaction_queue = queue.Queue()
        self.sent_messages_by_time: Dict[float, Tuple[str, str]] = {}
        self.cleanup_timer = None
        self._start_cleanup_timer()

    def add_sent_message(self, chat_id: str, message_hash: str, sent_time: float):
        rounded = round(sent_time, 1)
        self.sent_messages_by_time[rounded] = (chat_id, message_hash)

    def find_message_by_time(self, ack_time: float, tolerance: float = 5.0) -> Optional[Tuple[str, str]]:
        for t, (cid, h) in self.sent_messages_by_time.items():
            if abs(ack_time - t) < tolerance:
                return cid, h
        return None

    def _start_cleanup_timer(self):
        self.cleanup_timer = threading.Timer(300, self._cleanup_old_entries)
        self.cleanup_timer.daemon = True
        self.cleanup_timer.start()

    def _cleanup_old_entries(self):
        try:
            now = time.time()
            old_keys = [t for t in self.sent_messages_by_time.keys() if now - t > 300]
            for k in old_keys:
                del self.sent_messages_by_time[k]
            
            old_nodes = [n for n, d in self.last_packet_ids.items() if now - d.get('timestamp', 0) > 3600]
            for n in old_nodes:
                del self.last_packet_ids[n]
        except Exception as e:
            app_logger.error(f"Ошибка очистки: {e}")
        finally:
            self._start_cleanup_timer()

    def add_radio_message(self, message_data: Dict):
        self.radio_messages.append(message_data)
        if len(self.radio_messages) > self.max_radio_history:
            self.radio_messages = self.radio_messages[-self.max_radio_history:]

    def get_radio_history(self, limit: int = 20) -> List[Dict]:
        return self.radio_messages[-limit:] if self.radio_messages else []

    def is_duplicate_packet(self, from_node: str, packet_id: int) -> bool:
        if not from_node or from_node == 'Unknown':
            return False
        now = time.time()
        if from_node in self.last_packet_ids and self.last_packet_ids[from_node].get('packet_id') == packet_id:
            return True
        self.last_packet_ids[from_node] = {'packet_id': packet_id, 'timestamp': now}
        return False

    def add_reaction_task(self, chat_id: str, telegram_msg_id: int, reaction: str, delivery_time: float = None):
        self.reaction_queue.put({
            'chat_id': chat_id,
            'telegram_msg_id': telegram_msg_id,
            'reaction': reaction,
            'delivery_time': delivery_time,
            'timestamp': time.time()
        })

    def get_reaction_task(self) -> Optional[Dict]:
        try:
            return self.reaction_queue.get_nowait()
        except queue.Empty:
            return None


class MeshtasticGateway:
    """Класс для работы с Meshtastic устройством"""
    def __init__(self, host: str, config: Dict, on_connected_callback=None, on_battery_status=None):
        self.host = host
        self.config = config
        self.interface = None
        self.is_connected = False
        self.my_node_id = None
        self.message_callbacks = []
        self.on_connected_callback = on_connected_callback
        self.on_battery_status = on_battery_status
        self.should_reconnect = True
        self.reconnect_thread = None
        self.heartbeat_thread = None
        self.last_heartbeat_time = 0
        self.connection_lock = threading.Lock()
        self.battery_info = {}
        self.node_info_cache = {}

    def add_message_callback(self, callback: Callable):
        if callback not in self.message_callbacks:
            self.message_callbacks.append(callback)

    def connect(self) -> bool:
        with self.connection_lock:
            try:
                if not self._check_host_availability(self.host):
                    app_logger.error(f"Хост {self.host} недоступен")
                    self.is_connected = False
                    if self.on_connected_callback:
                        self.on_connected_callback(False)
                    return False
                
                self.interface = meshtastic.tcp_interface.TCPInterface(self.host)
                
                for _ in range(15):
                    if hasattr(self.interface, 'myInfo') and self.interface.myInfo:
                        break
                    time.sleep(1)
                
                if not hasattr(self.interface, 'myInfo') or not self.interface.myInfo:
                    raise ConnectionError("Не удалось получить myInfo")
                
                node_info = self.interface.getMyNodeInfo()
                self.my_node_id = node_info.get('user', {}).get('id', 'Unknown')
                self.is_connected = True
                self.last_heartbeat_time = time.time()
                
                self._update_battery_info()
                
                pub.subscribe(self._on_receive_packet, "meshtastic.receive")
                pub.subscribe(self._on_node_info, "meshtastic.node.updated")
                
                if self.on_connected_callback:
                    self.on_connected_callback(True)
                
                return True
                
            except Exception as e:
                app_logger.error(f"Ошибка подключения: {e}")
                self.is_connected = False
                if self.on_connected_callback:
                    self.on_connected_callback(False)
                return False

    def _check_host_availability(self, host: str, port: int = 4403, timeout: int = 3) -> bool:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except:
            return False

    def _update_battery_info(self):
        try:
            if not self.is_connected or not self.interface:
                return
            
            node_info = self.interface.getMyNodeInfo()
            device_metrics = node_info.get('deviceMetrics', {})
            
            if device_metrics:
                voltage = device_metrics.get('voltage')
                
                if voltage:
                    old_voltage = self.battery_info.get('voltage') if self.battery_info else None
                    self.battery_info = {
                        'voltage': voltage,
                        'timestamp': time.time(),
                        'node_id': self.my_node_id
                    }
                    
                    if old_voltage and abs(voltage - old_voltage) > 0.1:
                        app_logger.info(f"Батарея: {voltage:.2f}V")
                    
                    if self.on_battery_status:
                        self.on_battery_status(self.battery_info)
                    
                    return True
        except Exception as e:
            app_logger.error(f"Ошибка получения данных батареи: {e}")
        
        return False

    def get_battery_info(self) -> Dict:
        return self.battery_info.copy()

    def _on_node_info(self, node, interface):
        try:
            if node.get('num') == self.interface.getMyNodeInfo().get('num'):
                self._update_battery_info()
        except:
            pass

    def disconnect(self):
        with self.connection_lock:
            if self.interface:
                try:
                    pub.unsubscribe(self._on_receive_packet, "meshtastic.receive")
                    pub.unsubscribe(self._on_node_info, "meshtastic.node.updated")
                    self.interface.close()
                except:
                    pass
                self.interface = None
            self.is_connected = False

    def start_reconnect_loop(self):
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            return
        self.reconnect_thread = threading.Thread(target=self._reconnect_worker, daemon=True)
        self.reconnect_thread.start()
        
        if not self.heartbeat_thread:
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_worker, daemon=True)
            self.heartbeat_thread.start()

    def _reconnect_worker(self):
        while self.should_reconnect:
            if not self.is_connected:
                if self.connect():
                    self._notify_users("✅ Meshtastic: соединение восстановлено")
            time.sleep(self.config.get('mesh_reconnect_delay', 5))

    def _heartbeat_worker(self):
        while self.should_reconnect:
            time.sleep(self.config.get('mesh_heartbeat_interval', 30))
            
            if not self.is_connected:
                continue
                
            try:
                if self.interface and hasattr(self.interface, 'myInfo'):
                    self._update_battery_info()
                    
                    timeout = self.config.get('mesh_heartbeat_timeout', 60)
                    if time.time() - self.last_heartbeat_time > timeout:
                        app_logger.error("Heartbeat таймаут")
                        self.is_connected = False
                        self.disconnect()
                        self._notify_users("⚠️ Meshtastic: потеря связи")
                else:
                    self.is_connected = False
                    self._notify_users("⚠️ Meshtastic: потеря соединения")
            except Exception as e:
                app_logger.error(f"Ошибка heartbeat: {e}")
                self.is_connected = False

    def _notify_users(self, message: str):
        for callback in self.message_callbacks:
            try:
                if hasattr(callback, '__self__') and hasattr(callback.__self__, 'bot'):
                    bot = callback.__self__.bot
                    allowed_chats = self.config.get('allowed_chat_ids', [])
                    if not allowed_chats:
                        # Если нет ограничений, уведомляем всех пользователей
                        for chat_id, user in callback.__self__.user_settings.items():
                            try:
                                bot.send_message(chat_id, message, disable_notification=True)
                            except:
                                pass
                    else:
                        for chat_id in allowed_chats:
                            try:
                                bot.send_message(chat_id, message, disable_notification=True)
                            except:
                                pass
            except:
                pass

    def stop_reconnect_loop(self):
        self.should_reconnect = False

    def _extract_node_id(self, packet: Dict) -> Optional[str]:
        from_id = packet.get('fromId')
        if from_id and isinstance(from_id, str):
            if from_id.startswith('!'):
                return from_id
        
        from_num = packet.get('from')
        if from_num and isinstance(from_num, int):
            hex_id = format(from_num, '08x')
            return f"!{hex_id}"
        
        return None

    def _extract_hops(self, packet: Dict) -> Dict:
        result = {
            'hop_limit': None,
            'hop_start': None,
            'hops_traveled': None,
            'hops_valid': False
        }
        
        hop_limit = packet.get('hopLimit')
        hop_start = packet.get('hopStart')
        
        try:
            if hop_limit is not None:
                result['hop_limit'] = int(hop_limit)
        except (ValueError, TypeError):
            pass
        
        try:
            if hop_start is not None:
                result['hop_start'] = int(hop_start)
        except (ValueError, TypeError):
            pass
        
        if result['hop_start'] is not None and result['hop_limit'] is not None:
            if result['hop_start'] >= result['hop_limit']:
                result['hops_traveled'] = result['hop_start'] - result['hop_limit']
                result['hops_traveled'] = max(0, min(self.config.get('max_hops', 7), result['hops_traveled']))
                result['hops_valid'] = True
        
        return result

    def _on_receive_packet(self, packet: Dict, interface):
        try:
            self.last_heartbeat_time = time.time()
            
            node_id = self._extract_node_id(packet)
            hop_info = self._extract_hops(packet)
            
            decoded = packet.get('decoded', {})
            portnum = decoded.get('portnum', '')
            mesh_packet_id = packet.get('id', 0)
            rx_time = packet.get('rxTime', time.time())
            channel_index = packet.get('channel', 0)

            if portnum == 'TEXT_MESSAGE_APP':
                message_text = decoded.get('text', '').strip()
                if message_text and node_id:
                    for callback in self.message_callbacks:
                        try:
                            callback('text_message', {
                                'from': node_id,
                                'text': message_text,
                                'mesh_packet_id': mesh_packet_id,
                                'rx_time': rx_time,
                                'channel': channel_index,
                                'hop_limit': hop_info['hop_limit'],
                                'hop_start': hop_info['hop_start'],
                                'hops_traveled': hop_info['hops_traveled'],
                                'hops_valid': hop_info['hops_valid'],
                                'packet': packet
                            })
                        except Exception as e:
                            app_logger.error(f"Ошибка callback: {e}")

            elif portnum in ['ROUTING_APP', 'ACK_APP']:
                target = self.config.get('target_node', '')
                if target and node_id == target:
                    for callback in self.message_callbacks:
                        try:
                            callback('routing_ack', {
                                'mesh_packet_id': mesh_packet_id,
                                'from_node': target,
                                'rx_time': rx_time
                            })
                        except Exception as e:
                            app_logger.error(f"Ошибка callback ACK: {e}")

        except Exception as e:
            app_logger.error(f"Ошибка обработки пакета: {e}")

    def send_message(self, text: str, channel: int = 0, broadcast: bool = False) -> Tuple[bool, str, float]:
        try:
            if not self.is_connected:
                return False, "", 0.0
            
            sent_time = time.time()
            message_hash = hashlib.md5(f"{text}:{sent_time}:{random.random()}".encode()).hexdigest()[:16]
            
            max_size = self.config.get('max_message_size', 200)
            text_bytes = text.encode('utf-8')
            if len(text_bytes) > max_size:
                truncated = text_bytes[:max_size]
                while True:
                    try:
                        text = truncated.decode('utf-8')
                        break
                    except UnicodeDecodeError:
                        truncated = truncated[:-1]
                if len(text_bytes) > len(truncated):
                    text = text.rstrip() + "..."
            
            if broadcast:
                self.interface.sendText(text, destinationId="^all", wantAck=False, wantResponse=False, channelIndex=channel)
            else:
                target = self.config.get('target_node', '')
                if target:
                    self.interface.sendText(text, destinationId=target, wantAck=True, wantResponse=False, channelIndex=channel)
                else:
                    # Если нет целевой ноды, отправляем широковещательно
                    self.interface.sendText(text, destinationId="^all", wantAck=False, wantResponse=False, channelIndex=channel)
            
            self.last_heartbeat_time = time.time()
            
            return True, message_hash, sent_time
        except Exception as e:
            app_logger.error(f"Ошибка отправки: {e}")
            return False, "", 0.0


class MultiUserGateway:
    """Основной класс шлюза"""
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.telegram_token = config['telegram_token']
        self.meshtastic_host = config['meshtastic_host']

        self.meshtastic = MeshtasticGateway(
            self.meshtastic_host, 
            self.config,
            self._on_mesh_connection_change,
            self._on_battery_status
        )
        self.reactions = TelegramReactions(self.telegram_token)
        self.bot = telebot.TeleBot(self.telegram_token)
        self.tracker = MessageTracker()

        self.user_settings: Dict[str, UserSettings] = {}
        
        # Если нет разрешенных чатов - бот открыт для всех
        allowed_chats = config.get('allowed_chat_ids', [])
        if not allowed_chats:
            print("✅ Бот открыт для всех пользователей")
        else:
            for cid in allowed_chats:
                self.user_settings[cid] = UserSettings(cid)

        self.ping_mode = PingMode.AUTO
        self.ping_channel = config.get('ping_channel', 1)  # Канал для ping по умолчанию 1
        self.ping_counter = 0
        self.is_running = False
        self.timeout_timers: Dict[str, threading.Timer] = {}

        self.telegram_polling_thread = None
        self.telegram_polling_active = False
        self.battery_monitor_thread = None
        self.battery_monitor_active = False
        self.last_battery_warning_time = 0

        ping_keywords = ["ping", "пинг", "тест", "Ping", "Тест", "Test"]
        self.ping_keywords = ping_keywords
        
        # Проверка наличия целевой ноды
        self.has_target_node = bool(config.get('target_node') and config['target_node'] != '!00000000')
        if not self.has_target_node:
            print("⚠️ Целевая нода не указана - работа только в режиме радио")
            # Принудительно устанавливаем режим радио для всех пользователей
            for user in self.user_settings.values():
                user.set_mode(RadioMode.RADIO)

    def _on_mesh_connection_change(self, connected: bool):
        if connected:
            self._notify_all_users("✅ Meshtastic: соединение установлено")
        else:
            self._notify_all_users("⚠️ Meshtastic: потеря связи")

    def _on_battery_status(self, battery_info: Dict):
        try:
            voltage = battery_info.get('voltage')
            low_threshold = self.config.get('battery_low_threshold', 3.5)
            critical_threshold = self.config.get('battery_critical_threshold', 3.3)
            check_interval = self.config.get('battery_check_interval', 300)
            
            if voltage and voltage < low_threshold:
                current_time = time.time()
                if current_time - self.last_battery_warning_time > check_interval:
                    self.last_battery_warning_time = current_time
                    
                    level = "КРИТИЧЕСКИЙ" if voltage < critical_threshold else "НИЗКИЙ"
                    emoji = "🔴" if voltage < critical_threshold else "🟡"
                    
                    message = f"{emoji} {level} УРОВЕНЬ БАТАРЕИ: {voltage:.2f}V"
                    
                    self._notify_all_users(message)
        except Exception as e:
            app_logger.error(f"Ошибка обработки батареи: {e}")

    def _notify_all_users(self, message: str):
        allowed_chats = self.config.get('allowed_chat_ids', [])
        if not allowed_chats:
            # Уведомляем всех активных пользователей
            for chat_id in self.user_settings.keys():
                try:
                    self.bot.send_message(chat_id, message, disable_notification=True)
                except Exception as e:
                    app_logger.error(f"Ошибка уведомления {chat_id}: {e}")
        else:
            for chat_id in allowed_chats:
                try:
                    self.bot.send_message(chat_id, message, disable_notification=True)
                except Exception as e:
                    app_logger.error(f"Ошибка уведомления {chat_id}: {e}")

    def _format_node_id(self, node_id: Optional[str]) -> str:
        if not node_id:
            return 'Unknown'
        if node_id == 'Unknown' or node_id == 'unknown':
            return 'Unknown'
        if node_id.startswith('!'):
            if re.match(r'^![0-9a-f]{8}$', node_id.lower()):
                return node_id
        if re.match(r'^[0-9a-f]{8}$', node_id.lower()):
            return f"!{node_id.lower()}"
        return 'Unknown'

    def _format_hops_text(self, hops: Optional[int], hops_valid: bool = False) -> str:
        if not hops_valid or hops is None or hops == 0:
            return ""
        if hops == 1:
            return f" ({hops} хоп)"
        elif 2 <= hops <= 4:
            return f" ({hops} хопа)"
        else:
            return f" ({hops} хопов)"

    def get_user_menu(self, chat_id: str) -> ReplyKeyboardMarkup:
        markup = ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
        user = self.user_settings.get(chat_id)
        if not user:
            user = UserSettings(chat_id)
            # Если нет целевой ноды, принудительно устанавливаем режим радио
            if not self.has_target_node:
                user.set_mode(RadioMode.RADIO)
            self.user_settings[chat_id] = user
            
        # Показываем кнопку переключения режима только если есть целевая нода
        if self.has_target_node:
            if user.mode == RadioMode.GATEWAY:
                markup.add(KeyboardButton("📡 Режим радио"))
            else:
                markup.add(KeyboardButton("🎯 Режим моста"))
        else:
            markup.add(KeyboardButton("📡 Режим радио (только)"))
            
        markup.row(KeyboardButton("📊 Статус"), KeyboardButton("📜 История радио"))
        markup.row(KeyboardButton("ℹ️ Помощь"), KeyboardButton("🔄 Перезагрузить"))
        
        # Кнопки управления ping
        ping_row = []
        if self.ping_mode == PingMode.OFF:
            ping_row.append(KeyboardButton("✅ Ping: Вкл"))
        else:
            ping_row.append(KeyboardButton("🚫 Ping: Выкл"))
        
        # Кнопка выбора канала для ping
        ping_row.append(KeyboardButton(f"📻 Ping канал: {self.ping_channel}"))
        markup.row(*ping_row)
        
        return markup

    def is_allowed_chat(self, chat_id: str) -> bool:
        allowed_chats = self.config.get('allowed_chat_ids', [])
        # Если список пуст - разрешены все чаты
        if not allowed_chats:
            return True
        return chat_id in allowed_chats

    def set_ping_mode(self, mode: PingMode, notify_chat_id: str = None):
        old = self.ping_mode
        self.ping_mode = mode
        if notify_chat_id:
            names = {PingMode.OFF: "🚫 ВЫКЛЮЧЕН", PingMode.TEST: "🧪 ТЕСТОВЫЙ", PingMode.AUTO: "✅ АВТОМАТИЧЕСКИЙ"}
            self.bot.send_message(
                notify_chat_id,
                f"Режим ping: {names[mode]}",
                parse_mode=None,
                reply_markup=self.get_user_menu(notify_chat_id)
            )

    def set_ping_channel(self, channel: int, notify_chat_id: str = None):
        self.ping_channel = channel
        if notify_chat_id:
            self.bot.send_message(
                notify_chat_id,
                f"📻 Канал для ping: {channel}",
                parse_mode=None,
                reply_markup=self.get_user_menu(notify_chat_id)
            )

    def on_meshtastic_packet(self, packet_type: str, packet_data: Dict):
        try:
            if packet_type == 'text_message':
                from_node = packet_data.get('from')
                if not from_node:
                    return
                
                message_text = packet_data.get('text', '').strip()
                channel = packet_data.get('channel', 0)
                rx_time = packet_data.get('rx_time', time.time())
                mesh_packet_id = packet_data.get('mesh_packet_id', 0)
                
                hops_traveled = packet_data.get('hops_traveled')
                hops_valid = packet_data.get('hops_valid', False)
                
                formatted_node = self._format_node_id(from_node)
                if formatted_node == 'Unknown':
                    return
                
                if self.tracker.is_duplicate_packet(formatted_node, mesh_packet_id):
                    return
                
                time_str = datetime.fromtimestamp(rx_time).strftime("%H:%M:%S")
                
                radio_msg = {
                    'time': rx_time,
                    'from': formatted_node,
                    'text': message_text,
                    'channel': channel,
                    'packet_id': mesh_packet_id,
                    'hops_traveled': hops_traveled,
                    'hops_valid': hops_valid
                }
                self.tracker.add_radio_message(radio_msg)
                
                # Обработка ping
                if channel == self.ping_channel:
                    normalized = message_text.lower().strip()
                    if any(kw in normalized for kw in self.ping_keywords):
                        if formatted_node == self.meshtastic.my_node_id:
                            return
                        
                        self._handle_ping_request(
                            formatted_node, 
                            message_text, 
                            packet_data.get('hop_start'),
                            packet_data.get('hop_limit'),
                            hops_traveled,
                            hops_valid,
                            rx_time
                        )
                
                target_node = self.config.get('target_node', '')
                for chat_id, user in self.user_settings.items():
                    try:
                        if user.mode == RadioMode.RADIO and channel in (0, 1):
                            emoji = "📡" if channel == 0 else "📻"
                            hops_text = self._format_hops_text(hops_traveled, hops_valid)
                            radio_text = f"{emoji} CH{channel} [{formatted_node}]{hops_text} {time_str}\n{message_text}"
                            self.bot.send_message(chat_id, radio_text, parse_mode=None, disable_notification=True)
                        
                        elif user.mode == RadioMode.GATEWAY and target_node and formatted_node == target_node and channel == 0:
                            resp = f"📥 [{time_str}] {message_text}"
                            self.bot.send_message(chat_id, resp, parse_mode=None)
                    except Exception as e:
                        app_logger.error(f"Ошибка отправки пользователю {chat_id}: {e}")

            elif packet_type == 'routing_ack':
                mesh_id = packet_data.get('mesh_packet_id', 0)
                rx_time = packet_data.get('rx_time', time.time())
                from_node = packet_data.get('from_node', 'Unknown')
                
                target_node = self.config.get('target_node', '')
                if target_node and from_node == target_node:
                    result = self.tracker.find_message_by_time(rx_time)
                    
                    if result:
                        chat_id, msg_hash = result
                        user = self.user_settings.get(chat_id)
                        if user:
                            pend = user.get_pending_message(msg_hash)
                            if pend:
                                delivery = rx_time - pend.sent_time
                                pend.mark_as_delivered(delivery)
                                self.tracker.add_reaction_task(chat_id, pend.telegram_msg_id, 'delivered', delivery)
                                user.remove_pending_message(msg_hash)
                                if msg_hash in self.timeout_timers:
                                    self.timeout_timers[msg_hash].cancel()
                                    del self.timeout_timers[msg_hash]
                    else:
                        self._try_find_message_in_recent(rx_time)
                        
        except Exception as e:
            app_logger.error(f"Ошибка обработки пакета: {e}")

    def _handle_ping_request(self, from_node: str, message_text: str, hop_start: int, hop_limit: int, 
                            hops_traveled: int, hops_valid: bool, rx_time: float):
        try:
            self.ping_counter += 1
            
            if from_node == 'Unknown':
                return
            
            if hops_valid and hops_traveled is not None and hops_traveled > 0:
                steps = "🐰"
                response_text = f'Слышу {from_node} за {hops_traveled} {steps}.'
            else:
                response_text = f'Слышу {from_node}'
            
            for chat_id, user in self.user_settings.items():
                if user.mode == RadioMode.RADIO:
                    try:
                        if hops_valid and hops_traveled is not None:
                            hops_detail = f"Хопы: пройдено {hops_traveled} {steps}"
                        else:
                            hops_detail = "Хопы: не определены"
                        
                        notif = f"📡 PING от {from_node}\n{hops_detail}"
                        self.bot.send_message(chat_id, notif, parse_mode=None, disable_notification=True)
                    except Exception as e:
                        app_logger.error(f"Ошибка уведомления {chat_id}: {e}")
            
            if self.ping_mode == PingMode.AUTO:
                self.meshtastic.send_message(response_text, channel=self.ping_channel, broadcast=True)
                    
        except Exception as e:
            app_logger.error(f"Ошибка обработки ping: {e}")

    def _try_find_message_in_recent(self, ack_time: float):
        for cid, user in self.user_settings.items():
            if user.mode == RadioMode.GATEWAY:
                recent = user.find_recent_messages(max_age=30.0)
                for h, pend in recent:
                    if not pend.ack_received:
                        diff = abs(ack_time - pend.sent_time)
                        if diff < 10.0:
                            delivery = ack_time - pend.sent_time
                            pend.mark_as_delivered(delivery)
                            self.tracker.add_reaction_task(cid, pend.telegram_msg_id, 'delivered', delivery)
                            user.remove_pending_message(h)
                            if h in self.timeout_timers:
                                self.timeout_timers[h].cancel()
                                del self.timeout_timers[h]
                            self.tracker.add_sent_message(cid, h, pend.sent_time)
                            return

    def _setup_timeout_timer(self, message_hash: str, chat_id: str, telegram_msg_id: int):
        def handler():
            user = self.user_settings.get(chat_id)
            if user:
                pend = user.get_pending_message(message_hash)
                if pend:
                    pend.mark_as_unknown()
                    self.tracker.add_reaction_task(chat_id, telegram_msg_id, 'unknown')
                    user.remove_pending_message(message_hash)
            if message_hash in self.timeout_timers:
                del self.timeout_timers[message_hash]

        timer = threading.Timer(300, handler)
        timer.daemon = True
        timer.start()
        self.timeout_timers[message_hash] = timer

    def on_telegram_message(self, message):
        try:
            chat_id = str(message.chat.id)
            if not self.is_allowed_chat(chat_id):
                return
            
            if chat_id not in self.user_settings:
                user = UserSettings(chat_id)
                # Если нет целевой ноды, принудительно устанавливаем режим радио
                if not self.has_target_node:
                    user.set_mode(RadioMode.RADIO)
                self.user_settings[chat_id] = user
            
            text = message.text.strip() if message.text else ""
            if text.startswith('/'):
                return
            
            user = self.user_settings[chat_id]
            
            # Обработка кнопок
            if text == "📡 Режим радио":
                if self.has_target_node:
                    self.switch_to_radio_mode(message, user)
                else:
                    self.bot.send_message(chat_id, "⚠️ Режим моста недоступен (не указана целевая нода)", 
                                        parse_mode=None, reply_markup=self.get_user_menu(chat_id))
                return
                
            if text == "🎯 Режим моста":
                if self.has_target_node:
                    self.switch_to_gateway_mode(message, user)
                else:
                    self.bot.send_message(chat_id, "⚠️ Режим моста недоступен (не указана целевая нода)", 
                                        parse_mode=None, reply_markup=self.get_user_menu(chat_id))
                return
                
            if text == "📡 Режим радио (только)":
                self.switch_to_radio_mode(message, user)
                return
                
            if text == "📊 Статус":
                self.handle_status(message, user)
                return
                
            if text == "📜 История радио":
                self.handle_radio_history(message, user)
                return
                
            if text == "ℹ️ Помощь":
                self.handle_help(message, user)
                return
                
            if text == "🔄 Перезагрузить":
                self.handle_restart(message, user)
                return
                
            if text == "🚫 Ping: Выкл":
                self.set_ping_mode(PingMode.OFF, chat_id)
                return
                
            if text == "✅ Ping: Вкл":
                self.set_ping_mode(PingMode.AUTO, chat_id)
                return
                
            if text.startswith("📻 Ping канал:"):
                # Смена канала для ping
                new_channel = (self.ping_channel + 1) % 8  # Циклически 0-7
                self.set_ping_channel(new_channel, chat_id)
                return
            
            if not text:
                return
            
            max_size = self.config.get('max_message_size', 200)
            text_bytes = text.encode('utf-8')
            if len(text_bytes) > max_size:
                trunc = text_bytes[:max_size]
                while True:
                    try:
                        text = trunc.decode('utf-8')
                        break
                    except UnicodeDecodeError:
                        trunc = trunc[:-1]
                if len(text_bytes) > len(trunc):
                    text = text.rstrip() + "..."
                self.bot.send_message(chat_id, f"⚠️ Сообщение обрезано до {max_size} байт",
                                      reply_to_message_id=message.message_id, disable_notification=True)
            
            if user.mode == RadioMode.RADIO:
                ok, h, t = self.meshtastic.send_message(text, channel=0, broadcast=True)
                if ok:
                    self.reactions.set_reaction(chat_id, message.message_id, 'sent')
            else:
                if self.has_target_node:
                    ok, h, t = self.meshtastic.send_message(text, channel=0, broadcast=False)
                    if ok:
                        user.add_pending_message(h, message.message_id, text, t)
                        self.tracker.add_sent_message(chat_id, h, t)
                        self._setup_timeout_timer(h, chat_id, message.message_id)
                        self.reactions.set_reaction(chat_id, message.message_id, 'sent')
                    else:
                        self.reactions.set_reaction(chat_id, message.message_id, 'unknown')
                else:
                    self.bot.send_message(chat_id, "⚠️ Режим моста недоступен (не указана целевая нода)",
                                        reply_to_message_id=message.message_id)
                    
        except Exception as e:
            app_logger.error(f"Ошибка обработки Telegram: {e}")

    def switch_to_radio_mode(self, message, user: UserSettings):
        cid = str(message.chat.id)
        user.set_mode(RadioMode.RADIO)
        mode_names = {PingMode.OFF: "🚫 ВЫКЛЮЧЕН", PingMode.TEST: "🧪 ТЕСТОВЫЙ", PingMode.AUTO: "✅ АВТОМАТИЧЕСКИЙ"}
        msg = (
            f"📡 РЕЖИМ РАДИО\n\n"
            f"• Канал 0 и 1 - все сообщения\n"
            f"• Ping: {mode_names[self.ping_mode]}\n"
            f"• Ping канал: {self.ping_channel}\n"
            f"• Макс. хопов: {self.config.get('max_hops', 7)}"
        )
        self.bot.send_message(cid, msg, parse_mode=None, reply_markup=self.get_user_menu(cid))

    def switch_to_gateway_mode(self, message, user: UserSettings):
        cid = str(message.chat.id)
        user.set_mode(RadioMode.GATEWAY)
        target = self.config.get('target_node', 'не указана')
        msg = (
            f"🎯 РЕЖИМ МОСТА\n\n"
            f"• Нода: {target}\n"
            f"• Реакции: 🕊️->👍->🤷"
        )
        self.bot.send_message(cid, msg, parse_mode=None, reply_markup=self.get_user_menu(cid))

    def handle_status(self, message, user: UserSettings):
        cid = str(message.chat.id)
        try:
            status = "✅" if self.meshtastic.is_connected else "❌"
            
            battery_info = self.meshtastic.get_battery_info()
            battery_status = "Н/Д"
            if battery_info:
                voltage = battery_info.get('voltage')
                if voltage:
                    low = self.config.get('battery_low_threshold', 3.5)
                    critical = self.config.get('battery_critical_threshold', 3.3)
                    emoji = "🔴" if voltage < critical else "🟡" if voltage < low else "🟢"
                    battery_status = f"{emoji} {voltage:.2f}V"
            
            for u in self.user_settings.values():
                u.cleanup_old_messages()
            
            ping_names = {PingMode.OFF: "🚫 Выкл", PingMode.TEST: "🧪 Тест", PingMode.AUTO: "✅ Авто"}
            
            status_msg = (
                f"СТАТУС\n"
                f"• Meshtastic: {status}\n"
                f"• Батарея: {battery_status}\n"
                f"• Режим: {'📡 Радио' if user.mode == RadioMode.RADIO else '🎯 Мост'}\n"
                f"• Ping: {ping_names[self.ping_mode]}\n"
                f"• Ping канал: {self.ping_channel}\n"
                f"• Ping-запросов: {self.ping_counter}\n"
                f"• Сообщений в истории: {len(self.tracker.radio_messages)}"
            )
            
            if not self.has_target_node:
                status_msg += "\n• Целевая нода: не указана (только радио)"
                
            self.bot.send_message(cid, status_msg, parse_mode=None)
        except Exception as e:
            app_logger.error(f"Ошибка статуса: {e}")

    def handle_radio_history(self, message, user: UserSettings):
        cid = str(message.chat.id)
        hist = self.tracker.get_radio_history(limit=10)
        
        if not hist:
            self.bot.send_message(cid, "📭 История пуста", parse_mode=None)
            return
        
        resp = f"📡 ПОСЛЕДНИЕ {len(hist)}:\n\n"
        for msg in hist:
            tm = datetime.fromtimestamp(msg['time']).strftime("%H:%M")
            node = msg['from'] if msg['from'] != 'Unknown' else 'Unknown'
            emoji = "📡" if msg['channel'] == 0 else "📻"
            hops = msg.get('hops_traveled')
            hops_valid = msg.get('hops_valid', False)
            hops_text = self._format_hops_text(hops, hops_valid)
            resp += f"{emoji} CH{msg['channel']} [{node}]{hops_text} {tm}\n"
            resp += f"{msg['text'][:50]}{'...' if len(msg['text']) > 50 else ''}\n"
            resp += "─" * 20 + "\n"
        
        self.bot.send_message(cid, resp, parse_mode=None)

    def handle_help(self, message, user: UserSettings):
        cid = str(message.chat.id)
        target = self.config.get('target_node', 'не указана')
        
        help_txt = (
            f"Mesh-TG Gateway\n\n"
            f"Режимы:\n"
            f"📡 Радио - слушать все сообщения\n"
        )
        
        if self.has_target_node:
            help_txt += f"🎯 Мост - работать с {target}\n\n"
        else:
            help_txt += f"\n⚠️ Режим моста недоступен (целевая нода не указана)\n\n"
        
        help_txt += (
            f"Ping:\n"
            f"🚫 Выкл - нет ответа\n"
            f"✅ Вкл - автоматический ответ в сеть\n"
            f"📻 Ping канал - выбор канала для ping (0-7)\n\n"
        )
        
        if self.has_target_node:
            help_txt += (
                f"Реакции (мост):\n"
                f"🕊️ - отправлено\n👍 - доставлено\n🤷 - таймаут"
            )
        
        self.bot.send_message(cid, help_txt, parse_mode=None, reply_markup=self.get_user_menu(cid))

    def handle_restart(self, message, user: UserSettings):
        cid = str(message.chat.id)
        self.bot.send_message(cid, "🔄 Перезагрузка...", parse_mode=None)
        
        def restart():
            time.sleep(2)
            os.execl(sys.executable, sys.executable, *sys.argv)
        
        threading.Thread(target=restart, daemon=True).start()

    def process_reaction_queue(self):
        try:
            task = self.tracker.get_reaction_task()
            while task:
                ok = self.reactions.set_reaction(task['chat_id'], task['telegram_msg_id'], task['reaction'])
                if task.get('delivery_time') is not None and task['reaction'] == 'delivered' and ok:
                    try:
                        self.bot.send_message(task['chat_id'], f"⏱ {task['delivery_time']:.1f}с",
                                              reply_to_message_id=task['telegram_msg_id'], disable_notification=True)
                    except Exception as e:
                        app_logger.error(f"Ошибка отправки времени: {e}")
                task = self.tracker.get_reaction_task()
        except Exception as e:
            app_logger.error(f"Ошибка обработки реакций: {e}")

    def _telegram_polling_worker(self):
        delay = self.config.get('telegram_polling_restart_delay', 5)
        while self.telegram_polling_active:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=60, long_polling_timeout=30)
            except requests.exceptions.ReadTimeout:
                continue
            except requests.exceptions.ConnectionError as e:
                app_logger.error(f"Ошибка соединения с Telegram: {e}")
                if self.telegram_polling_active:
                    time.sleep(delay)
            except Exception as e:
                app_logger.error(f"Ошибка Telegram polling: {e}")
                if self.telegram_polling_active:
                    time.sleep(delay)
            finally:
                try:
                    self.bot.stop_polling()
                except:
                    pass

    def _battery_monitor_worker(self):
        interval = self.config.get('battery_check_interval', 300)
        while self.battery_monitor_active:
            try:
                time.sleep(interval)
                
                if self.meshtastic.is_connected:
                    self.meshtastic._update_battery_info()
            except Exception as e:
                app_logger.error(f"Ошибка мониторинга батареи: {e}")

    def setup_commands(self):
        @self.bot.message_handler(commands=['start'])
        def start_cmd(msg):
            cid = str(msg.chat.id)
            if not self.is_allowed_chat(cid):
                return
            if cid not in self.user_settings:
                user = UserSettings(cid)
                if not self.has_target_node:
                    user.set_mode(RadioMode.RADIO)
                self.user_settings[cid] = user
            user = self.user_settings[cid]
            self.bot.send_message(cid,
                f"Mesh-TG Gateway\nРежим: {'📡 Радио' if user.mode == RadioMode.RADIO else '🎯 Мост'}",
                parse_mode=None, reply_markup=self.get_user_menu(cid))

        @self.bot.message_handler(commands=['menu'])
        def menu_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid):
                self.bot.send_message(cid, "📱 Меню:", parse_mode=None, reply_markup=self.get_user_menu(cid))

        @self.bot.message_handler(commands=['status'])
        def status_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid) and cid in self.user_settings:
                self.handle_status(msg, self.user_settings[cid])

        @self.bot.message_handler(commands=['radio'])
        def radio_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid) and cid in self.user_settings:
                self.switch_to_radio_mode(msg, self.user_settings[cid])

        @self.bot.message_handler(commands=['gateway'])
        def gateway_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid) and cid in self.user_settings and self.has_target_node:
                self.switch_to_gateway_mode(msg, self.user_settings[cid])
            elif not self.has_target_node:
                self.bot.send_message(cid, "⚠️ Режим моста недоступен (не указана целевая нода)")

        @self.bot.message_handler(commands=['history'])
        def history_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid) and cid in self.user_settings:
                self.handle_radio_history(msg, self.user_settings[cid])

        @self.bot.message_handler(commands=['restart'])
        def restart_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid) and cid in self.user_settings:
                self.handle_restart(msg, self.user_settings[cid])

        @self.bot.message_handler(commands=['help'])
        def help_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid) and cid in self.user_settings:
                self.handle_help(msg, self.user_settings[cid])

        @self.bot.message_handler(commands=['pingoff'])
        def pingoff_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid):
                self.set_ping_mode(PingMode.OFF, cid)

        @self.bot.message_handler(commands=['pingon'])
        def pingon_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid):
                self.set_ping_mode(PingMode.AUTO, cid)

        @self.bot.message_handler(commands=['pingchannel'])
        def pingchannel_cmd(msg):
            cid = str(msg.chat.id)
            if self.is_allowed_chat(cid):
                try:
                    channel = int(msg.text.split()[1]) if len(msg.text.split()) > 1 else self.ping_channel + 1
                    channel = channel % 8
                    self.set_ping_channel(channel, cid)
                except:
                    self.bot.send_message(cid, "Использование: /pingchannel <0-7>")

        @self.bot.message_handler(func=lambda m: True)
        def all_messages(msg):
            self.on_telegram_message(msg)

    def start(self):
        try:
            self.is_running = True
            
            if not self.meshtastic.connect():
                app_logger.error("Не удалось подключиться к Meshtastic")
            self.meshtastic.start_reconnect_loop()
            self.meshtastic.add_message_callback(self.on_meshtastic_packet)
            
            self.setup_commands()
            
            self.telegram_polling_active = True
            self.telegram_polling_thread = threading.Thread(target=self._telegram_polling_worker, daemon=True)
            self.telegram_polling_thread.start()
            
            self.battery_monitor_active = True
            self.battery_monitor_thread = threading.Thread(target=self._battery_monitor_worker, daemon=True)
            self.battery_monitor_thread.start()
            
            while self.is_running:
                try:
                    self.process_reaction_queue()
                    
                    if not self.telegram_polling_thread.is_alive():
                        self.telegram_polling_thread = threading.Thread(target=self._telegram_polling_worker, daemon=True)
                        self.telegram_polling_thread.start()
                    
                    if not self.battery_monitor_thread.is_alive():
                        self.battery_monitor_thread = threading.Thread(target=self._battery_monitor_worker, daemon=True)
                        self.battery_monitor_thread.start()
                    
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    app_logger.error(f"Ошибка в основном цикле: {e}")
                    time.sleep(1)
            
            return True
            
        except Exception as e:
            app_logger.error(f"Ошибка запуска: {e}")
            return False
        finally:
            self.stop()

    def stop(self):
        self.is_running = False
        self.telegram_polling_active = False
        self.battery_monitor_active = False
        
        self.meshtastic.stop_reconnect_loop()
        self.meshtastic.disconnect()
        
        for t in self.timeout_timers.values():
            t.cancel()
        if self.tracker.cleanup_timer:
            self.tracker.cleanup_timer.cancel()
        
        try:
            self.bot.stop_polling()
        except:
            pass


def create_config():
    """Создание конфигурационного файла с запросом данных"""
    print("\n" + "="*60)
    print("Meshtastic Telegram Gateway - Первоначальная настройка")
    print("="*60)
    print("\nДля работы шлюза необходимо ввести следующие параметры:\n")
    
    config = {}
    
    # Telegram Bot Token
    print("1. Telegram Bot Token")
    print("   Получите токен у @BotFather в Telegram")
    token = input("   Токен: ").strip()
    while not token:
        print("   ❌ Токен обязателен!")
        token = input("   Токен: ").strip()
    config["telegram_token"] = token
    
    # Meshtastic host
    print("\n2. Meshtastic устройство")
    print("   IP адрес или hostname устройства (порт 4403)")
    host = input("   IP адрес [192.168.1.100]: ").strip()
    config["meshtastic_host"] = host if host else "192.168.1.100"
    
    # Allowed chat IDs (опционально)
    print("\n3. Разрешенные Telegram чаты (опционально)")
    print("   Оставьте пустым, чтобы бот был доступен всем")
    print("   ID можно узнать у бота @userinfobot")
    allowed_ids = []
    while True:
        cid = input(f"   Chat ID #{len(allowed_ids)+1} (Enter для завершения): ").strip()
        if not cid:
            break
        allowed_ids.append(cid)
    config["allowed_chat_ids"] = allowed_ids
    
    if not allowed_ids:
        print("   ✅ Бот будет доступен для всех пользователей")
    
    # Target node (опционально)
    print("\n4. Целевая нода Meshtastic (опционально)")
    print("   ID целевой ноды в формате !xxxxxxxx (например: !12345678)")
    print("   Если не указать, будет работать только режим радио")
    target = input("   ID ноды (Enter для пропуска): ").strip()
    config["target_node"] = target if target else ""
    
    if not target:
        print("   ⚠️ Режим моста будет недоступен")
    
    # Ping channel
    print("\n5. Канал для ping")
    print("   Номер канала (0-7), на котором будут обрабатываться ping запросы")
    ping_channel = input("   Канал для ping [1]: ").strip()
    config["ping_channel"] = int(ping_channel) if ping_channel else 1
    
    # Optional settings
    print("\n6. Дополнительные настройки (Enter для значений по умолчанию)")
    
    max_size = input("   Максимальный размер сообщения (байт) [200]: ").strip()
    config["max_message_size"] = int(max_size) if max_size else 200
    
    max_hops = input("   Максимальное количество хопов [7]: ").strip()
    config["max_hops"] = int(max_hops) if max_hops else 7
    
    restart = input("   Автоматический перезапуск при ошибке (true/false) [true]: ").strip()
    config["restart_on_crash"] = restart.lower() == 'true' if restart else True
    
    # Advanced settings (with defaults)
    config["mesh_reconnect_delay"] = 5
    config["mesh_heartbeat_interval"] = 30
    config["mesh_heartbeat_timeout"] = 60
    config["telegram_polling_restart_delay"] = 5
    config["battery_check_interval"] = 300
    config["battery_low_threshold"] = 3.5
    config["battery_critical_threshold"] = 3.3
    
    # Save config
    print("\n" + "="*60)
    print("Сохранение конфигурации...")
    
    try:
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print("✅ Конфигурация сохранена в config.json")
        print("="*60 + "\n")
        return config
    except Exception as e:
        print(f"❌ Ошибка сохранения конфигурации: {e}")
        sys.exit(1)


def edit_config(config):
    """Редактирование существующей конфигурации"""
    print("\n" + "="*60)
    print("Редактирование конфигурации")
    print("="*60)
    print("\nТекущие настройки:")
    print(f"  Telegram Bot Token: {config['telegram_token'][:10]}...")
    print(f"  Meshtastic Host: {config['meshtastic_host']}")
    print(f"  Разрешенные чаты: {config.get('allowed_chat_ids', []) or 'все'}")
    print(f"  Целевая нода: {config.get('target_node', 'не указана')}")
    print(f"  Ping канал: {config.get('ping_channel', 1)}")
    print(f"  Макс. размер: {config.get('max_message_size', 200)}")
    print(f"  Макс. хопов: {config.get('max_hops', 7)}")
    print("\nВведите новые значения (Enter для сохранения текущего):\n")
    
    # Telegram Bot Token
    token = input(f"Telegram Bot Token [{config['telegram_token'][:10]}...]: ").strip()
    if token:
        config["telegram_token"] = token
    
    # Meshtastic host
    host = input(f"Meshtastic IP адрес [{config['meshtastic_host']}]: ").strip()
    if host:
        config["meshtastic_host"] = host
    
    # Allowed chat IDs
    print(f"\nРазрешенные чаты (сейчас: {config.get('allowed_chat_ids', []) or 'все'}):")
    change_chats = input("Изменить список чатов? (y/n) [n]: ").strip().lower()
    if change_chats == 'y':
        allowed_ids = []
        print("Введите ID чатов (пустая строка для завершения):")
        while True:
            cid = input(f"  Chat ID #{len(allowed_ids)+1}: ").strip()
            if not cid:
                break
            allowed_ids.append(cid)
        config["allowed_chat_ids"] = allowed_ids
    
    # Target node
    current_target = config.get('target_node', 'не указана')
    target = input(f"Целевая нода (формат: !xxxxxxxx) [{current_target}]: ").strip()
    if target:
        config["target_node"] = target if target else ""
    
    # Ping channel
    current_channel = config.get('ping_channel', 1)
    ping_channel = input(f"Канал для ping (0-7) [{current_channel}]: ").strip()
    if ping_channel:
        config["ping_channel"] = int(ping_channel)
    
    # Max message size
    current_size = config.get('max_message_size', 200)
    max_size = input(f"Макс. размер сообщения (байт) [{current_size}]: ").strip()
    if max_size:
        config["max_message_size"] = int(max_size)
    
    # Max hops
    current_hops = config.get('max_hops', 7)
    max_hops = input(f"Макс. количество хопов [{current_hops}]: ").strip()
    if max_hops:
        config["max_hops"] = int(max_hops)
    
    # Restart on crash
    current_restart = config.get('restart_on_crash', True)
    restart = input(f"Авто-перезапуск при ошибке (true/false) [{current_restart}]: ").strip()
    if restart:
        config["restart_on_crash"] = restart.lower() == 'true'
    
    # Save config
    print("\nСохранение конфигурации...")
    try:
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print("✅ Конфигурация обновлена")
        print("="*60 + "\n")
        return config
    except Exception as e:
        print(f"❌ Ошибка сохранения: {e}")
        return config


def load_config():
    """Загрузка конфигурации из файла или создание нового"""
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        print("✅ Конфигурация загружена из config.json")
        
        # Спрашиваем, нужно ли изменить конфигурацию
        print("\n" + "="*60)
        edit = input("Изменить конфигурацию? (y/n) [n]: ").strip().lower()
        if edit == 'y':
            config = edit_config(config)
        
        return config
        
    except FileNotFoundError:
        print("❌ Файл config.json не найден")
        print("🔄 Запущена интерактивная настройка...")
        return create_config()
    except json.JSONDecodeError:
        print("❌ Ошибка в формате config.json")
        print("🔄 Запущена интерактивная настройка...")
        return create_config()


def main():
    """Главная функция"""
    try:
        config = load_config()
        
        print("\n" + "="*60)
        print("Запуск Meshtastic Telegram Gateway")
        print("="*60)
        bot_username = config['telegram_token'].split(':')[0] if ':' in config['telegram_token'] else '...'
        print(f"Telegram Bot: @{bot_username}")
        print(f"Meshtastic Host: {config['meshtastic_host']}")
        
        allowed = config.get('allowed_chat_ids', [])
        if allowed:
            print(f"Разрешенные чаты: {', '.join(allowed)}")
        else:
            print("Разрешенные чаты: ВСЕ (бот открыт)")
        
        target = config.get('target_node', '')
        if target:
            print(f"Целевая нода: {target}")
        else:
            print("Целевая нода: не указана (только режим радио)")
        
        print(f"Ping канал: {config.get('ping_channel', 1)}")
        print("="*60 + "\n")
        
        gw = MultiUserGateway(config)
        
        while True:
            try:
                result = gw.start()
                if result == 0:
                    break
            except KeyboardInterrupt:
                print("\n\n⏹️  Остановка шлюза...")
                break
            except Exception as e:
                app_logger.error(f"Критическая ошибка: {e}")
                traceback.print_exc()
                
                if config.get('restart_on_crash', True):
                    print(f"\n⚠️  Ошибка: {e}")
                    print("🔄 Перезапуск через 10 секунд...")
                    time.sleep(10)
                else:
                    break
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())