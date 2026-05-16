import os
import telebot
import fitz
from telebot import types
import tempfile
import shutil
import atexit
import re
import random
import threading
import pymongo
from bson.binary import Binary
import time
from datetime import datetime, timezone

TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')
MONGO_URI = os.environ.get('MONGO_URI')

if not MONGO_URI:
    print("CRITICAL WARNING: MONGO_URI environment variable is not set! Database functions will fail.")

INCOMING_BACKUP_GROUP_ID_STR = os.environ.get('INCOMING_BACKUP_GROUP_ID', '').strip()
OUTGOING_BACKUP_GROUP_ID_STR = os.environ.get('OUTGOING_BACKUP_GROUP_ID', '').strip()
STORAGE_DIR = tempfile.mkdtemp(prefix='pdf-processor-')
PDF_SAVE_GARBAGE_LEVEL = 3
MAX_BULK_QUEUE_SIZE = 50
FREE_PDF_WEEKLY_LIMIT = 2
WEEKLY_RESET_SECONDS = 604800
RANDOM_WATERMARK_PAGE_INTERVAL = 3
MIN_WATERMARK_TRANSPARENCY_PERCENT = 1
MIN_WATERMARK_OPACITY = MIN_WATERMARK_TRANSPARENCY_PERCENT / 100.0
TEXT_WATERMARK_HORIZONTAL_MARGIN_RATIO = 0.12
TEXT_WATERMARK_TOP_RATIO = 0.42
TEXT_WATERMARK_BOTTOM_RATIO = 0.58
TEXT_WATERMARK_DIAGONAL_ANGLE = 45
TEXT_WATERMARK_FONT_MIN = 18
TEXT_WATERMARK_FONT_MAX = 48
TEXT_WATERMARK_FONT_DIVISOR = 12
IMAGE_WATERMARK_WIDTH_RATIO = 0.4
IMAGE_WATERMARK_HEIGHT_RATIO = 0.22
MAX_IMAGE_SUFFIX_LENGTH = 10
TELEGRAM_PHOTO_DEFAULT_SUFFIX = ".jpg"
TEXT_WATERMARK_OPTIMAL_CHARACTER_COUNT = 40
TEXT_WATERMARK_COLOR = (0.75, 0.75, 0.75)
SUPPORTED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
IMAGE_MIME_SUFFIX_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}

print(f"Token loaded: {bool(TOKEN)}")

try:
    ALLOWED_USERS = [int(uid.strip()) for uid in ALLOWED_USERS_STR.split(',') if uid.strip()]
except Exception as e:
    ALLOWED_USERS = []

try:
    INCOMING_BACKUP_GROUP_ID = int(INCOMING_BACKUP_GROUP_ID_STR) if INCOMING_BACKUP_GROUP_ID_STR else None
except Exception:
    INCOMING_BACKUP_GROUP_ID = None

try:
    OUTGOING_BACKUP_GROUP_ID = int(OUTGOING_BACKUP_GROUP_ID_STR) if OUTGOING_BACKUP_GROUP_ID_STR else None
except Exception:
    OUTGOING_BACKUP_GROUP_ID = None

bot = telebot.TeleBot(TOKEN)
user_states = {}
state_lock = threading.Lock()
user_locks = {}

# MongoDB Setup
if MONGO_URI:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client['pdf_bot_database']
    users_col = db['users']
    watermarks_col = db['watermarks']
else:
    mongo_client = None
    db = None
    users_col = None
    watermarks_col = None

def get_user_lock(user_id):
    with state_lock:
        if user_id not in user_locks:
            user_locks[user_id] = threading.Lock()
        return user_locks[user_id]

def initialize_database():
    if mongo_client:
        print("MongoDB Atlas configured.")

def get_user_plan_info(user_id):
    now = time.time()
    user = users_col.find_one({"user_id": user_id})

    if not user:
        user = {
            "user_id": user_id,
            "is_premium": False,
            "pdfs_processed": 0,
            "last_reset_timestamp": now,
        }
        users_col.insert_one(user)

    is_premium = user.get("is_premium", False)
    pdfs_processed = user.get("pdfs_processed", 0)
    last_reset_timestamp = user.get("last_reset_timestamp", now)

    if not is_premium and now - last_reset_timestamp >= WEEKLY_RESET_SECONDS:
        pdfs_processed = 0
        last_reset_timestamp = now
        users_col.update_one(
            {"user_id": user_id},
            {"$set": {"pdfs_processed": pdfs_processed, "last_reset_timestamp": last_reset_timestamp}},
        )

    return {
        'is_premium': is_premium,
        'pdfs_processed': pdfs_processed,
        'last_reset_timestamp': last_reset_timestamp,
    }

def increment_processed_pdf_count(user_id):
    now = time.time()
    user = users_col.find_one({"user_id": user_id})

    if not user:
        get_user_plan_info(user_id)
        user = users_col.find_one({"user_id": user_id})

    if user and user.get("is_premium", False):
        return

    pdfs_processed = user.get("pdfs_processed", 0)
    last_reset_timestamp = user.get("last_reset_timestamp", now)

    if now - last_reset_timestamp >= WEEKLY_RESET_SECONDS:
        pdfs_processed = 0
        last_reset_timestamp = now

    pdfs_processed += 1
    users_col.update_one(
        {"user_id": user_id},
        {"$set": {"pdfs_processed": pdfs_processed, "last_reset_timestamp": last_reset_timestamp}},
    )

def set_premium_status(user_id, is_premium):
    now = time.time()
    if is_premium:
        users_col.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": True}},
            upsert=True,
        )
    else:
        users_col.update_one(
            {"user_id": user_id},
            {"$set": {"is_premium": False, "pdfs_processed": 0, "last_reset_timestamp": now}},
            upsert=True,
        )

def cleanup_storage_dir():
    try:
        shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    except Exception as e:
        print(f"Storage dir cleanup failed: {e}")

def delete_file(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            print(f"Cleanup failed for {path}: {e}")

def reset_watermark_state(state, delete_pending_image=True):
    if not state:
        return
    if delete_pending_image:
        delete_file(state.get('pending_watermark_image_path'))
    for key in (
        'watermark_type',
        'watermark_layout',
        'watermark_orientation',
        'watermark_transparency',
        'pending_watermark_text',
        'pending_watermark_image_path',
        'pending_watermark_image_suffix',
    ):
        state.pop(key, None)
    awaiting = state.get('awaiting') or ''
    if awaiting.startswith('watermark_'):
        state['awaiting'] = None

def clear_user_state(user_id, delete_source=False):
    state = user_states.get(user_id)
    if not state:
        return
    queued_pdfs = state.get('pdf_queue') or []
    reset_watermark_state(state, delete_pending_image=True)
    if delete_source:
        unique_paths = set()
        for pdf_item in queued_pdfs:
            source_path = pdf_item.get('source_path')
            if source_path:
                unique_paths.add(source_path)
        active_source_path = state.get('source_path')
        if active_source_path:
            unique_paths.add(active_source_path)
        for source_path in unique_paths:
            delete_file(source_path)
    user_states.pop(user_id, None)

def get_pdf_queue(state):
    return state.get('pdf_queue') or []

def has_pdf_queue(state):
    return bool(get_pdf_queue(state))

def build_queue_status_text(queue_count):
    return f"Received {queue_count} PDF(s). Send more if you want, then choose an action below:"

def upsert_queue_action_menu(user_id, state):
    queue_count = len(get_pdf_queue(state))
    if queue_count <= 0:
        return

    menu_text = build_queue_status_text(queue_count)
    has_saved_watermark = watermarks_col.find_one({"user_id": user_id}) is not None
    reply_markup = build_action_keyboard(include_bulk_saved=has_saved_watermark)
    
    action_menu_message_id = state.get('action_menu_message_id')
    if action_menu_message_id:
        try:
            bot.delete_message(user_id, action_menu_message_id)
        except Exception as e:
            pass

    try:
        sent = bot.send_message(user_id, menu_text, reply_markup=reply_markup)
        state['action_menu_message_id'] = sent.message_id
    except Exception as e:
        print(f"Failed to send action menu: {e}")

def clear_action_menu_state(state):
    if not state:
        return
    state.pop('action_menu_message_id', None)

def build_action_keyboard(include_bulk_saved=False):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("Rename PDF", callback_data="rename_pdf"))
    keyboard.row(types.InlineKeyboardButton("Remove Watermark", callback_data="remove_watermark"))
    keyboard.row(types.InlineKeyboardButton("Add Watermark", callback_data="add_watermark"))
    if include_bulk_saved:
        keyboard.row(types.InlineKeyboardButton("Use Saved Watermark", callback_data="use_saved_watermark"))
    keyboard.row(types.InlineKeyboardButton("Unlock PDF", callback_data="unlock_pdf"))
    return keyboard

def build_watermark_type_keyboard(include_saved=False):
    keyboard = types.InlineKeyboardMarkup()
    if include_saved:
        keyboard.row(types.InlineKeyboardButton("Use Last Saved Watermark", callback_data="watermark_use_saved"))
    keyboard.row(types.InlineKeyboardButton("Text Watermark", callback_data="watermark_type_text"))
    keyboard.row(types.InlineKeyboardButton("Image Watermark", callback_data="watermark_type_image"))
    return keyboard

def build_watermark_layout_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("Every page", callback_data="watermark_layout_every"))
    keyboard.row(types.InlineKeyboardButton("Random", callback_data="watermark_layout_random"))
    return keyboard

def build_watermark_orientation_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("Horizontal", callback_data="watermark_orientation_horizontal"))
    keyboard.row(types.InlineKeyboardButton("Diagonal", callback_data="watermark_orientation_diagonal"))
    return keyboard

def build_watermark_save_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("Yes", callback_data="watermark_save_yes"))
    keyboard.row(types.InlineKeyboardButton("No", callback_data="watermark_save_no"))
    return keyboard

def extract_user_info(user):
    return {
        'id': user.id,
        'first_name': user.first_name or "N/A",
        'username': user.username or "",
    }

def build_backup_caption(user_info, label):
    username = f"@{user_info.get('username')}" if user_info and user_info.get('username') else "N/A"
    first_name = user_info.get('first_name') if user_info else "N/A"
    telegram_id = user_info.get('id') if user_info else "N/A"
    return f"{label}\nFirst Name: {first_name}\nUsername: {username}\nTelegram ID: {telegram_id}"

def send_backup_pdf(file_path, file_name, user_info, label, backup_group_id):
    if not backup_group_id:
        return
    try:
        with open(file_path, 'rb') as backup_file:
            bot.send_document(
                backup_group_id,
                backup_file,
                visible_file_name=file_name,
                caption=build_backup_caption(user_info, label),
            )
    except Exception as e:
        print(f"Failed to send backup PDF: {e}")

def send_processed_pdf(user_id, output_path, output_name, user_info=None):
    with open(output_path, 'rb') as final_file:
        bot.send_document(user_id, final_file, visible_file_name=output_name)
    increment_processed_pdf_count(user_id)
    send_backup_pdf(output_path, output_name, user_info, "Processed PDF", OUTGOING_BACKUP_GROUP_ID)

def delete_callback_message(call):
    message = getattr(call, 'message', None)
    if not message:
        return
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        print(f"Failed to delete inline keyboard message: {e}")

def save_current_watermark_profile(user_id, state):
    watermark_type = state.get('watermark_type')
    profile = {
        'type': watermark_type,
        'layout': state.get('watermark_layout', 'every'),
        'transparency': state.get('watermark_transparency', 100),
    }
    if watermark_type == 'text':
        profile['orientation'] = state.get('watermark_orientation', 'horizontal')
        profile['text'] = state.get('pending_watermark_text', '')
    elif watermark_type == 'image':
        image_path = state.get('pending_watermark_image_path')
        if not image_path or not os.path.exists(image_path):
            return False
        with open(image_path, 'rb') as image_file:
            profile['image_bytes'] = Binary(image_file.read())
        profile['image_suffix'] = state.get('pending_watermark_image_suffix') or os.path.splitext(image_path)[1] or ".png"
    else:
        return False
        
    watermarks_col.update_one({"user_id": user_id}, {"$set": profile}, upsert=True)
    return True

def build_saved_watermark_image_path(profile):
    image_suffix = profile.get('image_suffix') or ".png"
    image_path = new_private_image_path(image_suffix)
    with open(image_path, 'wb') as image_file:
        image_file.write(profile.get('image_bytes', b''))
    return image_path

def is_valid_saved_watermark_profile(profile):
    if not profile or profile.get('type') not in ('text', 'image'):
        return False
    if profile.get('type') == 'text':
        return bool((profile.get('text') or '').strip())
    if profile.get('type') == 'image':
        return bool(profile.get('image_bytes'))
    return False

def get_or_create_user_state(user_id, user_info=None):
    state = user_states.get(user_id)
    if not state:
        state = {
            'awaiting': None,
            'pdf_queue': [],
            'upload_slots_reserved': 0,
        }
        user_states[user_id] = state
    if 'pdf_queue' not in state:
        state['pdf_queue'] = []
    if 'upload_slots_reserved' not in state:
        state['upload_slots_reserved'] = 0
    if user_info:
        state['user_info'] = user_info
    return state

def enqueue_pdf_for_user(state, source_path, original_name):
    pdf_queue = state.setdefault('pdf_queue', [])
    if len(pdf_queue) >= MAX_BULK_QUEUE_SIZE:
        return False, len(pdf_queue)
    pdf_item = {
        'source_path': source_path,
        'original_name': original_name,
    }
    pdf_queue.append(pdf_item)
    state['source_path'] = source_path
    state['original_name'] = original_name
    return True, len(pdf_queue)

def apply_saved_watermark(user_id, state):
    profile = watermarks_col.find_one({"user_id": user_id})
    if not profile:
        bot.send_message(user_id, "No saved watermark was found. Please create one first.")
        return

    reset_watermark_state(state, delete_pending_image=True)
    state['watermark_type'] = profile.get('type')
    state['watermark_layout'] = profile.get('layout', 'every')
    state['watermark_transparency'] = profile.get('transparency', 100)
    state['watermark_orientation'] = profile.get('orientation', 'horizontal')
    state['awaiting'] = None

    if profile.get('type') == 'text':
        state['pending_watermark_text'] = profile.get('text', '')
        process_add_watermark(user_id, state, watermark_text=state['pending_watermark_text'])
        return

    if profile.get('type') == 'image':
        image_path = build_saved_watermark_image_path(profile)
        state['pending_watermark_image_path'] = image_path
        state['pending_watermark_image_suffix'] = profile.get('image_suffix') or ".png"
        process_add_watermark(user_id, state, image_path=image_path)
        return

    bot.send_message(user_id, "Saved watermark data is incomplete. Please create it again.")

def burn_pdf_to_images(doc):
    out_pdf = fitz.open()
    for page in doc:
        # 1. Page eka image ekak (pixmap) karanawa
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        
        # 2. Aluth PDF eke his page ekak hadanawa (original page size ekatama)
        new_page = out_pdf.new_page(width=page.rect.width, height=page.rect.height)
        
        # 3. Ara image eka aluth page ekata insert karanawa
        new_page.insert_image(new_page.rect, pixmap=pix)
        
    return out_pdf

def process_saved_watermark_profile_for_pdf(source_path, profile):
    output_path = None
    doc = None
    image_path = None
    try:
        doc = fitz.open(source_path)
        if doc.needs_pass:
            raise ValueError("PDF is password protected")

        watermark_type = profile.get('type')
        watermark_layout = profile.get('layout', 'every')
        watermark_transparency = profile.get('transparency', 100)
        watermark_orientation = profile.get('orientation', 'horizontal')

        if watermark_type == 'text':
            watermark_text = profile.get('text', '')
            if not watermark_text:
                raise ValueError("Saved text watermark is empty")
            add_text_watermark(doc, watermark_text, watermark_layout, watermark_transparency, watermark_orientation)
        elif watermark_type == 'image':
            image_path = build_saved_watermark_image_path(profile)
            add_image_watermark(doc, image_path, watermark_layout, watermark_transparency)
        else:
            raise ValueError("Saved watermark type is invalid")

        burned_doc = burn_pdf_to_images(doc)
        output_path = new_private_pdf_path()
        burned_doc.save(output_path, deflate=True, deflate_images=True, deflate_fonts=True, garbage=4, clean=True)
        burned_doc.close()
        return output_path
    finally:
        if doc:
            try:
                doc.close()
            except Exception:
                pass
        if image_path:
            delete_file(image_path)

def process_bulk_saved_watermark(user_id, state):
    profile = watermarks_col.find_one({"user_id": user_id})
    if not is_valid_saved_watermark_profile(profile):
        bot.send_message(user_id, "Saved watermark data is incomplete. Please create it again.")
        return

    pdf_queue = list(state.get('pdf_queue') or [])
    if not pdf_queue:
        bot.send_message(user_id, "No PDFs found in your queue. Please upload PDFs first.")
        return

    processed_count = 0
    failed_count = 0
    user_info = state.get('user_info')
    for pdf_item in pdf_queue:
        source_path = pdf_item
