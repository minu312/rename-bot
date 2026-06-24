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
from PIL import Image
import io

TOKEN = os.environ.get('BOT_TOKEN')
WELCOME_CHANNEL_ID_STR = os.environ.get('WELCOME_CHANNEL_ID', '').strip()
WELCOME_MESSAGE_ID_STR = os.environ.get('WELCOME_MESSAGE_ID', '').strip()

try:
    WELCOME_CHANNEL_ID = int(WELCOME_CHANNEL_ID_STR) if WELCOME_CHANNEL_ID_STR and WELCOME_CHANNEL_ID_STR.lstrip('-').isdigit() else WELCOME_CHANNEL_ID_STR
except Exception:
    WELCOME_CHANNEL_ID = None

try:
    WELCOME_MESSAGE_ID = int(WELCOME_MESSAGE_ID_STR) if WELCOME_MESSAGE_ID_STR else None
except Exception:
    WELCOME_MESSAGE_ID = None
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
    if mongo_client is not None:
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
    user = users_col.find_one({"user_id": user_id}) if users_col is not None else None
    thumb_path = None
    
    try:
        if user and "thumbnail_bytes" in user:
            thumb_path = new_private_image_path(".jpg")
            with open(thumb_path, 'wb') as f:
                f.write(user["thumbnail_bytes"])
        
        with open(output_path, 'rb') as final_file:
            thumb_file = open(thumb_path, 'rb') if thumb_path else None
            bot.send_document(user_id, final_file, visible_file_name=output_name, thumb=thumb_file)
            if thumb_file:
                thumb_file.close()
        
        increment_processed_pdf_count(user_id)
        send_backup_pdf(output_path, output_name, user_info, "Processed PDF", OUTGOING_BACKUP_GROUP_ID)
    finally:
        delete_file(thumb_path)

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
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_bytes = pix.tobytes("jpeg")
        new_page = out_pdf.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=img_bytes)
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
        source_path = pdf_item.get('source_path')
        original_name = pdf_item.get('original_name') or "document.pdf"
        output_path = None
        try:
            if not source_path or not os.path.exists(source_path):
                raise FileNotFoundError("Source PDF not found")
            output_path = process_saved_watermark_profile_for_pdf(source_path, profile)
            send_processed_pdf(
                user_id,
                output_path,
                build_output_name(original_name, "watermarked"), 
                user_info=user_info,
            )
            processed_count += 1
        except Exception as e:
            failed_count += 1
            bot.send_message(user_id, f"Failed to apply saved watermark: {original_name}")
        finally:
            if output_path:
                delete_file(output_path)
            if source_path:
                delete_file(source_path)

    clear_user_state(user_id, delete_source=False)
    bot.send_message(user_id, f"Bulk processing completed.\nProcessed: {processed_count}\nFailed: {failed_count}")

def build_output_name(original_name, suffix):
    name = original_name or "document.pdf"
    if not name.lower().endswith('.pdf'):
        name = f"{name}.pdf"
    return name

def normalize_pdf_filename(name):
    cleaned = os.path.basename((name or "").strip())
    cleaned = cleaned.replace('\x00', '')
    cleaned = re.sub(r'[\\/:*?"<>|]+', '_', cleaned)
    cleaned = re.sub(r'_+', '_', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(" .")
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        cleaned = "renamed_document"
    if not cleaned.lower().endswith('.pdf'):
        cleaned = f"{cleaned}.pdf"
    return cleaned

def new_private_pdf_path():
    fd, temp_path = tempfile.mkstemp(suffix='.pdf', dir=STORAGE_DIR)
    os.close(fd)
    return temp_path

def new_private_image_path(suffix):
    safe_suffix = suffix if suffix and len(suffix) <= MAX_IMAGE_SUFFIX_LENGTH else ".png"
    fd, temp_path = tempfile.mkstemp(suffix=safe_suffix, dir=STORAGE_DIR)
    os.close(fd)
    return temp_path

def get_safe_image_suffix(file_name, mime_type=None):
    mapped_suffix = IMAGE_MIME_SUFFIX_MAP.get((mime_type or "").lower())
    if mapped_suffix:
        return mapped_suffix
    extension = os.path.splitext(file_name or "")[1].lower()
    if (
        not extension
        or len(extension) > MAX_IMAGE_SUFFIX_LENGTH
        or extension not in SUPPORTED_IMAGE_SUFFIXES
    ):
        return ".png"
    return extension

def is_supported_image_document(document):
    mime_type = (document.mime_type or "").lower()
    file_name = (document.file_name or "").lower()
    return mime_type.startswith("image/") or file_name.endswith(SUPPORTED_IMAGE_SUFFIXES)

def download_telegram_file(file_id, suffix):
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    output_path = new_private_image_path(suffix)
    with open(output_path, 'wb') as out_file:
        out_file.write(downloaded_file)
    return output_path

def get_target_page_indexes(doc, layout):
    page_count = doc.page_count
    if page_count <= 0:
        return []
    if layout == "random":
        offset = random.randint(0, RANDOM_WATERMARK_PAGE_INTERVAL - 1)
        random_pages = [i for i in range(page_count) if i % RANDOM_WATERMARK_PAGE_INTERVAL == offset]
        return random_pages or [0]
    return list(range(page_count))

def add_text_watermark(doc, watermark_text, layout, transparency, orientation):
    opacity = max(MIN_WATERMARK_OPACITY, min(1.0, transparency / 100.0))
    for page_index in get_target_page_indexes(doc, layout):
        page = doc[page_index]
        rect = page.rect
        base_font_size = int(rect.width / TEXT_WATERMARK_FONT_DIVISOR)
        text_length = max(1, len(watermark_text))
        if text_length > TEXT_WATERMARK_OPTIMAL_CHARACTER_COUNT:
            base_font_size = int(base_font_size * TEXT_WATERMARK_OPTIMAL_CHARACTER_COUNT / text_length)
        font_size = max(TEXT_WATERMARK_FONT_MIN, min(TEXT_WATERMARK_FONT_MAX, base_font_size))
        if orientation == 'diagonal':
            text_width = fitz.get_text_length(watermark_text, fontsize=font_size)
            center_x = rect.width / 2
            center_y = rect.height / 2
            insertion_point = fitz.Point(center_x - (text_width / 2), center_y)
            page.insert_text(
                insertion_point,
                watermark_text,
                fontsize=font_size,
                color=TEXT_WATERMARK_COLOR,
                overlay=True,
                fill_opacity=opacity,
                stroke_opacity=opacity,
                morph=(fitz.Point(center_x, center_y), fitz.Matrix(TEXT_WATERMARK_DIAGONAL_ANGLE)),
            )
            continue

        horizontal_margin = rect.width * TEXT_WATERMARK_HORIZONTAL_MARGIN_RATIO
        box = fitz.Rect(
            horizontal_margin,
            rect.height * TEXT_WATERMARK_TOP_RATIO,
            rect.width - horizontal_margin,
            rect.height * TEXT_WATERMARK_BOTTOM_RATIO,
        )
        page.insert_textbox(
            box,
            watermark_text,
            fontsize=font_size,
            color=TEXT_WATERMARK_COLOR,
            align=1,
            overlay=True,
            fill_opacity=opacity,
            stroke_opacity=opacity,
        )

def add_image_watermark(doc, image_path, layout, transparency):
    # PDF eke first page eken dimensions gannawa watermark size eka hadanna
    first_page = doc[0]
    rect = first_page.rect
    
    # Userge ratios walata anuwa target size eka hadagannawa
    target_width = int(rect.width * IMAGE_WATERMARK_WIDTH_RATIO)
    target_height = int(rect.height * IMAGE_WATERMARK_HEIGHT_RATIO)
    
    try:
        # Pillow eken image eka open karala RGBA walata gannawa
        wm_img = Image.open(image_path).convert("RGBA")
        
        # High quality resize karanawa (thumbnail use karanne aspect ratio eka kedei nathuwa resize karanna)
        wm_img.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
        
        # Resize kalata passe thiyena actual width and height
        final_width, final_height = wm_img.size
        
        # Opacity eka wens kireema
        if transparency < 100:
            opacity_level = max(MIN_WATERMARK_OPACITY, transparency / 100.0)
            alpha = wm_img.split()[3]
            alpha = alpha.point(lambda p: p * opacity_level)
            wm_img.putalpha(alpha)
            
        # Process karapu image eka memory (bytes) walata save karanawa
        img_byte_arr = io.BytesIO()
        wm_img.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()
        
    except Exception as e:
        print(f"Watermark image processing failed: {e}")
        return

    # PDF pages walata watermark eka apply kirima
    for page_index in get_target_page_indexes(doc, layout):
        page = doc[page_index]
        rect = page.rect
        
        # Image eka center karanna coordinates hadagannawa
        x0 = (rect.width - final_width) / 2
        y0 = (rect.height - final_height) / 2
        x1 = x0 + final_width
        y1 = y0 + final_height
        
        image_rect = fitz.Rect(x0, y0, x1, y1)
        
        # Bytes haraha image eka PDF ekata insert karanawa
        page.insert_image(image_rect, stream=img_bytes, overlay=True)

def process_add_watermark(user_id, state, watermark_text=None, image_path=None):
    pdf_queue = list(get_pdf_queue(state))
    if not pdf_queue:
        bot.send_message(user_id, "No PDFs found in your queue. Please upload PDFs first.")
        return

    watermark_type = state.get('watermark_type')
    watermark_layout = state.get('watermark_layout', 'every')
    watermark_transparency = state.get('watermark_transparency', 100)
    watermark_orientation = state.get('watermark_orientation', 'horizontal')
    watermark_text = watermark_text if watermark_text is not None else state.get('pending_watermark_text')
    image_path = image_path or state.get('pending_watermark_image_path')

    if watermark_type == 'text' and not watermark_text:
        reset_watermark_state(state, delete_pending_image=True)
        bot.send_message(user_id, "Watermark settings are incomplete. Please choose the action again.", reply_markup=build_action_keyboard())
        return
    if watermark_type == 'image' and not image_path:
        reset_watermark_state(state, delete_pending_image=True)
        bot.send_message(user_id, "Watermark settings are incomplete. Please choose the action again.", reply_markup=build_action_keyboard())
        return
    if watermark_type not in ('text', 'image'):
        reset_watermark_state(state, delete_pending_image=True)
        bot.send_message(user_id, "Watermark settings are incomplete. Please choose the action again.", reply_markup=build_action_keyboard())
        return

    processed_count = 0
    failed_count = 0
    user_info = state.get('user_info')
    for pdf_item in pdf_queue:
        source_path = pdf_item.get('source_path')
        original_name = pdf_item.get('original_name') or "document.pdf"
        output_path = None
        doc = None
        try:
            if not source_path or not os.path.exists(source_path):
                raise FileNotFoundError("Source PDF not found")
            doc = fitz.open(source_path)
            if doc.needs_pass:
                raise ValueError("PDF is password protected")

            if watermark_type == 'text':
                add_text_watermark(doc, watermark_text, watermark_layout, watermark_transparency, watermark_orientation)
            else:
                add_image_watermark(doc, image_path, watermark_layout, watermark_transparency)

            burned_doc = burn_pdf_to_images(doc)
            output_path = new_private_pdf_path()
            burned_doc.save(output_path, deflate=True, deflate_images=True, deflate_fonts=True, garbage=4, clean=True)
            burned_doc.close()
            send_processed_pdf(
                user_id,
                output_path,
                build_output_name(original_name, "watermarked"),
                user_info=user_info,
            )
            processed_count += 1
        except Exception as e:
            failed_count += 1
            bot.send_message(user_id, f"Failed to add watermark to: {original_name}")
        finally:
            if doc:
                try:
                    doc.close()
                except Exception:
                    pass
            if output_path:
                delete_file(output_path)
            if source_path:
                delete_file(source_path)

    clear_user_state(user_id, delete_source=False)
    bot.send_message(user_id, f"Batch watermark completed.\nProcessed: {processed_count}\nFailed: {failed_count}")
    if image_path:
        delete_file(image_path)

initialize_database()
atexit.register(cleanup_storage_dir)

@bot.message_handler(commands=['start'])
def send_welcome(message):
   user_id = message.from_user.id
    
    if WELCOME_CHANNEL_ID and WELCOME_MESSAGE_ID:
        try:
        
            bot.forward_message(
                chat_id=user_id,
                from_chat_id=WELCOME_CHANNEL_ID,
                message_id=WELCOME_MESSAGE_ID
            )
        except Exception as e:
            print(f"Failed to forward the welcome message: {e}")
            bot.reply_to(message, "Send me a PDF file to process.")
    else:
        bot.reply_to(message, "Send me a PDF file to process.")

@bot.message_handler(commands=['addpremium'])
def add_premium(message):
    admin_user_id = message.from_user.id
    if admin_user_id not in ALLOWED_USERS:
        bot.reply_to(message, "Sorry, you are not authorized to use this command.")
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /addpremium <user_id>")
        return

    try:
        target_user_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "Usage: /addpremium <user_id>")
        return

    set_premium_status(target_user_id, True)
    bot.reply_to(message, f"Premium enabled for user {target_user_id}.")

@bot.message_handler(commands=['removepremium'])
def remove_premium(message):
    admin_user_id = message.from_user.id
    if admin_user_id not in ALLOWED_USERS:
        bot.reply_to(message, "Sorry, you are not authorized to use this command.")
        return

    parts = (message.text or "").split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /removepremium <user_id>")
        return

    try:
        target_user_id = int(parts[1].strip())
    except ValueError:
        bot.reply_to(message, "Usage: /removepremium <user_id>")
        return

    set_premium_status(target_user_id, False)
    bot.reply_to(message, f"Premium removed for user {target_user_id}.")

@bot.message_handler(commands=['myplan'])
def my_plan(message):
    user_id = message.from_user.id
    plan = get_user_plan_info(user_id)
    if plan['is_premium']:
        bot.reply_to(message, "Status: Premium 👑 (Unlimited PDFs)")
        return

    reset_at = datetime.fromtimestamp(
        plan['last_reset_timestamp'] + WEEKLY_RESET_SECONDS,
        tz=timezone.utc,
    )
    reset_at_text = reset_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    bot.reply_to(
        message,
        f"Status: Free 🆓\nPDFs processed this week: {plan['pdfs_processed']}/{FREE_PDF_WEEKLY_LIMIT}\nResets on: {reset_at_text}",
    )

@bot.message_handler(commands=['set_thumbnail'])
def set_thumbnail_command(message):
    user_id = message.from_user.id
    state = get_or_create_user_state(user_id)
    state['awaiting'] = 'thumbnail_upload'
    bot.reply_to(message, "📸 Please send a 320x320 JPG image to set as the PDF thumbnail.")

@bot.message_handler(commands=['delete_thumbnail'])
def delete_thumbnail_command(message):
    user_id = message.from_user.id
    if users_col is not None:
        users_col.update_one({"user_id": user_id}, {"$unset": {"thumbnail_bytes": ""}})
    bot.reply_to(message, "🗑️ Your thumbnail has been deleted. Future PDFs will be sent without a thumbnail.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    user_lock = get_user_lock(user_id)
    user_info = extract_user_info(message.from_user)

    with user_lock:
        state = get_or_create_user_state(user_id, user_info)
        
        # Catch if a thumbnail or watermark is sent as a document/file instead of a photo
        if state and state.get('awaiting') in ['watermark_image_upload', 'thumbnail_upload']:
            if not is_supported_image_document(message.document):
                bot.reply_to(message, "Please upload a valid image (PNG/JPG) file.")
                return

            try:
                image_path = download_telegram_file(
                    message.document.file_id,
                    get_safe_image_suffix(message.document.file_name, message.document.mime_type),
                )
                
                # If it's a thumbnail, save it directly to MongoDB
                if state.get('awaiting') == 'thumbnail_upload':
                    with open(image_path, 'rb') as f:
                        image_bytes = f.read()
                    if users_col is not None:
                        users_col.update_one(
                            {"user_id": user_id}, 
                            {"$set": {"thumbnail_bytes": Binary(image_bytes)}},
                            upsert=True
                        )
                    state['awaiting'] = None
                    bot.reply_to(message, "✅ Thumbnail saved! It will be automatically attached to all processed PDFs from now on.")
                    delete_file(image_path)
                    return
                # If it's a watermark, proceed to the next step
                else:
                    state['pending_watermark_image_path'] = image_path
                    state['pending_watermark_image_suffix'] = get_safe_image_suffix(message.document.file_name, message.document.mime_type)
                    state['awaiting'] = 'watermark_transparency'
                    bot.reply_to(message, "Please send the watermark transparency level (1-100).")
                    return
            except Exception as e:
                bot.reply_to(message, f"❌ System Error: {str(e)}\n\nCould not download the image. Please try again.")
            return
    
    if message.document.mime_type != 'application/pdf' and not message.document.file_name.lower().endswith('.pdf'):
        bot.reply_to(message, "Please send a valid PDF file.")
        return

    with user_lock:
        state = get_or_create_user_state(user_id, user_info)
        queue_count = len(get_pdf_queue(state))
        reserved_count = max(0, int(state.get('upload_slots_reserved', 0)))
        plan = get_user_plan_info(user_id)
        if not plan['is_premium'] and (plan['pdfs_processed'] + queue_count + reserved_count) >= FREE_PDF_WEEKLY_LIMIT:
            bot.reply_to(
                message,
                "You have reached your free limit of 2 PDFs per week. Please purchase Premium to continue.",
            )
            return
        state['upload_slots_reserved'] = reserved_count + 1

    original_name = message.document.file_name or "document.pdf"
    source_path = new_private_pdf_path()

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with open(source_path, 'wb') as source_file:
            source_file.write(downloaded_file)
    except Exception as e:
        with user_lock:
            state = get_or_create_user_state(user_id, user_info)
            state['upload_slots_reserved'] = max(0, int(state.get('upload_slots_reserved', 0)) - 1)
        bot.reply_to(message, "Could not download the PDF. Please try again.")
        delete_file(source_path)
        return

    with user_lock:
        state = get_or_create_user_state(user_id, user_info)
        state['upload_slots_reserved'] = max(0, int(state.get('upload_slots_reserved', 0)) - 1)
        queued, queue_count = enqueue_pdf_for_user(state, source_path, original_name)
        if queued:
            upsert_queue_action_menu(user_id, state)
        user_info_from_state = state.get('user_info')
    if not queued:
        bot.reply_to(message, f"Queue is full (max {MAX_BULK_QUEUE_SIZE} PDFs). Please process current batch first.")
        delete_file(source_path)
        return

    send_backup_pdf(source_path, original_name, user_info_from_state, "Original PDF", INCOMING_BACKUP_GROUP_ID)


@bot.callback_query_handler(func=lambda call: call.data in ("rename_pdf", "unlock_pdf", "remove_watermark", "add_watermark", "use_saved_watermark"))
def handle_action_choice(call):
    user_id = call.from_user.id
    delete_callback_message(call)

    if user_id not in user_states or not has_pdf_queue(user_states[user_id]):
        bot.answer_callback_query(call.id, "Please send a PDF first.")
        bot.send_message(user_id, "Please send a PDF file first.")
        return

    state = user_states[user_id]
    clear_action_menu_state(state)

    if call.data == "use_saved_watermark":
        bot.answer_callback_query(call.id)
        process_bulk_saved_watermark(user_id, state)
        return

    if call.data == "rename_pdf":
        reset_watermark_state(state, delete_pending_image=True)
        state['awaiting'] = 'new_name'
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "Please type and send the new file name.")
        return

    if call.data == "unlock_pdf":
        reset_watermark_state(state, delete_pending_image=True)
        state['awaiting'] = 'password'
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "Please send the password for this PDF.")
        return

    if call.data == "add_watermark":
        reset_watermark_state(state, delete_pending_image=True)
        state['awaiting'] = 'watermark_type'
        bot.answer_callback_query(call.id)
        
        has_saved_watermark = watermarks_col.find_one({"user_id": user_id}) is not None
        bot.send_message(
            user_id,
            "Choose watermark type:",
            reply_markup=build_watermark_type_keyboard(include_saved=has_saved_watermark),
        )
        return

    reset_watermark_state(state, delete_pending_image=True)
    state['awaiting'] = 'watermark_text'
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, "Please send the exact watermark text to remove.")


@bot.callback_query_handler(
    func=lambda call: call.data in (
        "watermark_use_saved",
        "watermark_type_text",
        "watermark_type_image",
        "watermark_layout_every",
        "watermark_layout_random",
        "watermark_orientation_horizontal",
        "watermark_orientation_diagonal",
        "watermark_save_yes",
        "watermark_save_no",
    )
)
def handle_add_watermark_choices(call):
    user_id = call.from_user.id
    delete_callback_message(call)

    state = user_states.get(user_id)
    if not state or not has_pdf_queue(state):
        bot.answer_callback_query(call.id, "Please send a PDF first.")
        bot.send_message(user_id, "Please send a PDF file first.")
        return

    if call.data == "watermark_use_saved":
        bot.answer_callback_query(call.id)
        apply_saved_watermark(user_id, state)
        return

    if call.data in ("watermark_type_text", "watermark_type_image"):
        state['watermark_type'] = 'text' if call.data == "watermark_type_text" else 'image'
        state['awaiting'] = 'watermark_layout'
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "Choose watermark layout:", reply_markup=build_watermark_layout_keyboard())
        return

    if state.get('watermark_type') not in ('text', 'image'):
        bot.answer_callback_query(call.id, "Choose watermark type first.")
        has_saved_watermark = watermarks_col.find_one({"user_id": user_id}) is not None
        bot.send_message(
            user_id,
            "Please choose watermark type first.",
            reply_markup=build_watermark_type_keyboard(include_saved=has_saved_watermark),
        )
        return

    if call.data in ("watermark_orientation_horizontal", "watermark_orientation_diagonal"):
        state['watermark_orientation'] = call.data.rsplit('_', 1)[-1]
        state['awaiting'] = 'watermark_transparency'
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "Please send the watermark transparency level (1-100).")
        return

    if call.data in ("watermark_save_yes", "watermark_save_no"):
        if call.data == "watermark_save_yes":
            if save_current_watermark_profile(user_id, state):
                bot.answer_callback_query(call.id, "Watermark saved.")
            else:
                bot.answer_callback_query(call.id, "Could not save watermark.")
        else:
            bot.answer_callback_query(call.id)
        process_add_watermark(user_id, state)
        return

    state['watermark_layout'] = 'every' if call.data == "watermark_layout_every" else 'random'
    bot.answer_callback_query(call.id)

    if state['watermark_type'] == 'text':
        state['awaiting'] = 'watermark_add_text'
        bot.send_message(user_id, "Please send the watermark text.")
        return

    state['awaiting'] = 'watermark_image_upload'
    bot.send_message(user_id, "Please upload the watermark image (photo or image document).")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state:
        return

    awaiting = state.get('awaiting')

    if awaiting == 'thumbnail_upload':
        try:
            file_id = message.photo[-1].file_id 
            file_info = bot.get_file(file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            if users_col is not None:
                users_col.update_one(
                    {"user_id": user_id}, 
                    {"$set": {"thumbnail_bytes": Binary(downloaded_file)}},
                    upsert=True
                )
            state['awaiting'] = None
            bot.reply_to(message, "✅ Thumbnail saved! It will be automatically attached to all processed PDFs from now on.")
        except Exception as e:
            bot.reply_to(message, "❌ Failed to save the thumbnail. Please try again.")
        return

    if awaiting == 'watermark_image_upload':
        if not message.photo:
            bot.reply_to(message, "Please upload a valid image photo.")
            return

        try:
            image_path = download_telegram_file(message.photo[-1].file_id, TELEGRAM_PHOTO_DEFAULT_SUFFIX)
            state['pending_watermark_image_path'] = image_path
            state['pending_watermark_image_suffix'] = TELEGRAM_PHOTO_DEFAULT_SUFFIX
            state['awaiting'] = 'watermark_transparency'
            bot.reply_to(message, "Please send the watermark transparency level (1-100).")
        except Exception as e:
            bot.reply_to(message, "Could not download the image. Please try again.")
        return

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    
    state = user_states.get(user_id)
    if not state or not has_pdf_queue(state):
        return

    awaiting = state.get('awaiting')
    if awaiting == 'new_name':
        requested_name = (message.text or "").strip()

        if not requested_name:
            bot.reply_to(message, "File name cannot be empty. Please send a valid name.")
            return

        pdf_queue = list(get_pdf_queue(state))
        output_name = normalize_pdf_filename(requested_name)
        name_root, ext = os.path.splitext(output_name)
        append_index_suffix = len(pdf_queue) > 1
        processed_count = 0
        failed_count = 0
        user_info = state.get('user_info')
        for index, pdf_item in enumerate(pdf_queue, start=1):
            source_path = pdf_item.get('source_path')
            current_output_name = output_name if not append_index_suffix else f"{name_root}_{index}{ext}"
            try:
                if not source_path or not os.path.exists(source_path):
                    raise FileNotFoundError("Source PDF not found")
                send_processed_pdf(user_id, source_path, current_output_name, user_info=user_info)
                processed_count += 1
            except Exception as e:
                failed_count += 1
                bot.send_message(user_id, f"Failed to rename: {pdf_item.get('original_name') or 'document.pdf'}")
            finally:
                if source_path:
                    delete_file(source_path)
        clear_user_state(user_id, delete_source=False)
        bot.send_message(user_id, f"Batch rename completed.\nProcessed: {processed_count}\nFailed: {failed_count}")
        return

    if awaiting == 'password':
        password = message.text or ""
        pdf_queue = list(get_pdf_queue(state))
        processed_count = 0
        failed_count = 0
        user_info = state.get('user_info')
        for pdf_item in pdf_queue:
            source_path = pdf_item.get('source_path')
            original_name = pdf_item.get('original_name') or "document.pdf"
            output_path = None
            doc = None
            try:
                if not source_path or not os.path.exists(source_path):
                    raise FileNotFoundError("Source PDF not found")
                doc = fitz.open(source_path)
                if doc.needs_pass and not doc.authenticate(password):
                    raise ValueError("Incorrect password")
                output_path = new_private_pdf_path()
                doc.save(output_path, encryption=fitz.PDF_ENCRYPT_NONE, deflate=True, garbage=PDF_SAVE_GARBAGE_LEVEL)
                send_processed_pdf(
                    user_id,
                    output_path,
                    build_output_name(original_name, "unlocked"),
                    user_info=user_info,
                )
                processed_count += 1
            except Exception as e:
                failed_count += 1
                bot.send_message(user_id, f"Failed to unlock: {original_name}")
            finally:
                if doc:
                    try:
                        doc.close()
                    except Exception:
                        pass
                if output_path:
                    delete_file(output_path)
                if source_path:
                    delete_file(source_path)
        clear_user_state(user_id, delete_source=False)
        bot.send_message(user_id, f"Batch unlock completed.\nProcessed: {processed_count}\nFailed: {failed_count}")
        return

    if awaiting == 'watermark_text':
        watermark_text = message.text or ""

        if not watermark_text.strip():
            bot.reply_to(message, "Watermark text cannot be empty. Please send the exact text.")
            return

        target_variants = set()
        for encoding in ("utf-8", "latin-1"):
            try:
                raw = watermark_text.encode(encoding)
                if raw:
                    target_variants.add(raw)
                    target_variants.add(
                        raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
                    )
            except Exception:
                continue

        if not target_variants:
            bot.reply_to(message, "No matching watermark text was found. Choose an action and try again.", reply_markup=build_action_keyboard())
            return

        def strip_from_literal_string(content_bytes):
            removed = 0
            updated = content_bytes
            for target in target_variants:
                if not target:
                    continue
                updated, count = re.subn(re.escape(target), b"", updated)
                removed += count
            return updated, removed

        pdf_queue = list(get_pdf_queue(state))
        processed_count = 0
        failed_count = 0
        user_info = state.get('user_info')
        for pdf_item in pdf_queue:
            source_path = pdf_item.get('source_path')
            original_name = pdf_item.get('original_name') or "document.pdf"
            output_path = None
            doc = None
            try:
                if not source_path or not os.path.exists(source_path):
                    raise FileNotFoundError("Source PDF not found")
                doc = fitz.open(source_path)
                if doc.needs_pass:
                    raise ValueError("PDF is password protected")

                matches = 0
                processed_xrefs = set()

                for page in doc:
                    content_xrefs = page.get_contents() or []
                    if isinstance(content_xrefs, int):
                        content_xrefs = [content_xrefs]

                    for xref in content_xrefs:
                        if xref in processed_xrefs:
                            continue
                        processed_xrefs.add(xref)

                        stream = doc.xref_stream(xref)
                        if not stream:
                            continue

                        stream_matches = 0

                        def replace_tj(match):
                            nonlocal stream_matches
                            literal_body = match.group(1)
                            suffix = match.group(2)
                            updated_body, removed = strip_from_literal_string(literal_body)
                            stream_matches += removed
                            return b"[" + updated_body + b"]" + suffix

                        def replace_tj_array(match):
                            nonlocal stream_matches
                            array_body = match.group(1)
                            suffix = match.group(2)

                            def replace_array_string(s_match):
                                nonlocal stream_matches
                                literal = s_match.group(0)
                                inner = literal[1:-1]
                                updated_inner, removed = strip_from_literal_string(inner)
                                stream_matches += removed
                                return b"(" + updated_inner + b")"

                            updated_array = re.sub(rb"\((?:\\.|[^\\)])*\)", replace_array_string, array_body)
                            return b"[" + updated_array + b"]" + suffix

                        modified_stream = re.sub(rb"\(((?:\\.|[^\\)])*)\)(\s*Tj)", replace_tj, stream)
                        modified_stream = re.sub(rb"\[(.*?)\](\s*TJ)", replace_tj_array, modified_stream, flags=re.DOTALL)

                        if stream_matches > 0 and modified_stream != stream:
                            doc.update_stream(xref, modified_stream)
                            matches += stream_matches

                if matches == 0:
                    raise ValueError("No matching watermark text was found")

                output_path = new_private_pdf_path()
                doc.save(output_path, deflate=True, garbage=PDF_SAVE_GARBAGE_LEVEL)
                send_processed_pdf(
                    user_id,
                    output_path,
                    build_output_name(original_name, "watermark_removed"),
                    user_info=user_info,
                )
                processed_count += 1
            except Exception as e:
                failed_count += 1
                bot.send_message(user_id, f"Failed to remove watermark: {original_name}")
            finally:
                if doc:
                    try:
                        doc.close()
                    except Exception:
                        pass
                if output_path:
                    delete_file(output_path)
                if source_path:
                    delete_file(source_path)

        clear_user_state(user_id, delete_source=False)
        bot.send_message(user_id, f"Batch watermark removal completed.\nProcessed: {processed_count}\nFailed: {failed_count}")
        return

    if awaiting == 'watermark_add_text':
        watermark_text = (message.text or "").strip()
        if not watermark_text:
            bot.reply_to(message, "Watermark text cannot be empty. Please send valid text.")
            return
        state['pending_watermark_text'] = watermark_text
        state['awaiting'] = 'watermark_orientation'
        bot.reply_to(message, "Choose text orientation:", reply_markup=build_watermark_orientation_keyboard())
        return

    if awaiting == 'watermark_orientation':
        bot.reply_to(message, "Please choose the text orientation using the buttons above.")
        return

    if awaiting == 'watermark_transparency':
        transparency_text = (message.text or "").strip()
        if not transparency_text.isdigit():
            bot.reply_to(message, "Please send a number between 1 and 100 for transparency.")
            return
        transparency = int(transparency_text)
        if transparency < 1 or transparency > 100:
            bot.reply_to(message, "Transparency must be between 1 and 100.")
            return
        state['watermark_transparency'] = transparency
        state['awaiting'] = 'watermark_save_choice'
        bot.reply_to(message, "Do you want to save this watermark?", reply_markup=build_watermark_save_keyboard())
        return

    if awaiting == 'watermark_save_choice':
        bot.reply_to(message, "Please choose Yes or No using the buttons above.")
        return

if __name__ == "__main__":
    if MONGO_URI:
        print("Starting bot polling with MongoDB enabled...")
    else:
        print("Starting bot polling (WARNING: Database features will fail without MONGO_URI)")
    bot.polling(none_stop=True)
