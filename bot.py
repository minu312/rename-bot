import os
import telebot
import fitz
from telebot import types
import tempfile
import shutil
import atexit
import re
import random

TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')
BACKUP_GROUP_ID_STR = os.environ.get('BACKUP_GROUP_ID', '').strip()
STORAGE_DIR = tempfile.mkdtemp(prefix='pdf-processor-')
PDF_SAVE_GARBAGE_LEVEL = 3
RANDOM_WATERMARK_PAGE_INTERVAL = 3
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
print(f"Allowed users string: {ALLOWED_USERS_STR}")

try:
    ALLOWED_USERS = [int(uid.strip()) for uid in ALLOWED_USERS_STR.split(',') if uid.strip()]
    print(f"Allowed Users List: {ALLOWED_USERS}")
except Exception as e:
    print(f"Error parsing user IDs: {e}")
    ALLOWED_USERS = []

try:
    BACKUP_GROUP_ID = int(BACKUP_GROUP_ID_STR) if BACKUP_GROUP_ID_STR else None
    print(f"Backup group configured: {bool(BACKUP_GROUP_ID)}")
except Exception as e:
    print(f"Error parsing backup group ID: {e}")
    BACKUP_GROUP_ID = None

bot = telebot.TeleBot(TOKEN)
user_states = {}
saved_watermarks = {}


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
    reset_watermark_state(state, delete_pending_image=True)
    if delete_source:
        delete_file(state.get('source_path'))
    user_states.pop(user_id, None)


def build_action_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("Rename PDF", callback_data="rename_pdf"))
    keyboard.row(types.InlineKeyboardButton("Remove Watermark", callback_data="remove_watermark"))
    keyboard.row(types.InlineKeyboardButton("Add Watermark", callback_data="add_watermark"))
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


def send_backup_pdf(file_path, file_name, user_info, label):
    if not BACKUP_GROUP_ID:
        return
    try:
        with open(file_path, 'rb') as backup_file:
            bot.send_document(
                BACKUP_GROUP_ID,
                backup_file,
                visible_file_name=file_name,
                caption=build_backup_caption(user_info, label),
            )
    except Exception as e:
        print(f"Failed to send backup PDF: {e}")


def send_processed_pdf(user_id, output_path, output_name, user_info=None):
    with open(output_path, 'rb') as final_file:
        bot.send_document(user_id, final_file, visible_file_name=output_name)
    send_backup_pdf(output_path, output_name, user_info, "Processed PDF")


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
            profile['image_bytes'] = image_file.read()
        profile['image_suffix'] = state.get('pending_watermark_image_suffix') or os.path.splitext(image_path)[1] or ".png"
    else:
        return False
    saved_watermarks[user_id] = profile
    return True


def build_saved_watermark_image_path(profile):
    image_suffix = profile.get('image_suffix') or ".png"
    image_path = new_private_image_path(image_suffix)
    with open(image_path, 'wb') as image_file:
        image_file.write(profile.get('image_bytes', b''))
    return image_path


def apply_saved_watermark(user_id, state):
    profile = saved_watermarks.get(user_id)
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


def build_output_name(original_name, suffix):
    base_name = original_name or "document.pdf"
    if base_name.lower().endswith('.pdf'):
        base_name = base_name[:-4]
    return f"{base_name}_{suffix}.pdf"


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
    opacity = max(0.01, min(1.0, transparency / 100.0))
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
    watermark_pixmap = None
    if transparency < 100:
        alpha_value = max(1, min(255, round(255 * transparency / 100.0)))
        base_pixmap = fitz.Pixmap(image_path)
        watermark_pixmap = fitz.Pixmap(base_pixmap, 1)
        watermark_pixmap.set_alpha(bytes([alpha_value]) * (watermark_pixmap.width * watermark_pixmap.height), premultiply=0)

    for page_index in get_target_page_indexes(doc, layout):
        page = doc[page_index]
        rect = page.rect
        watermark_width = rect.width * IMAGE_WATERMARK_WIDTH_RATIO
        watermark_height = rect.height * IMAGE_WATERMARK_HEIGHT_RATIO
        image_rect = fitz.Rect(
            (rect.width - watermark_width) / 2,
            (rect.height - watermark_height) / 2,
            (rect.width + watermark_width) / 2,
            (rect.height + watermark_height) / 2,
        )
        if watermark_pixmap is not None:
            page.insert_image(image_rect, pixmap=watermark_pixmap, overlay=True, keep_proportion=True)
        else:
            page.insert_image(image_rect, filename=image_path, overlay=True, keep_proportion=True)


def process_add_watermark(user_id, state, watermark_text=None, image_path=None):
    source_path = state['source_path']
    output_path = None
    doc = None

    try:
        doc = fitz.open(source_path)
        if doc.needs_pass:
            reset_watermark_state(state, delete_pending_image=True)
            bot.send_message(user_id, "This PDF is password protected. Please use 'Unlock PDF' first.", reply_markup=build_action_keyboard())
            return

        watermark_type = state.get('watermark_type')
        watermark_layout = state.get('watermark_layout', 'every')
        watermark_transparency = state.get('watermark_transparency', 100)
        watermark_orientation = state.get('watermark_orientation', 'horizontal')
        watermark_text = watermark_text if watermark_text is not None else state.get('pending_watermark_text')
        image_path = image_path or state.get('pending_watermark_image_path')

        if watermark_type == 'text':
            if not watermark_text:
                reset_watermark_state(state, delete_pending_image=True)
                bot.send_message(user_id, "Watermark settings are incomplete. Please choose the action again.", reply_markup=build_action_keyboard())
                return
            add_text_watermark(doc, watermark_text, watermark_layout, watermark_transparency, watermark_orientation)
        elif watermark_type == 'image':
            if not image_path:
                reset_watermark_state(state, delete_pending_image=True)
                bot.send_message(user_id, "Watermark settings are incomplete. Please choose the action again.", reply_markup=build_action_keyboard())
                return
            add_image_watermark(doc, image_path, watermark_layout, watermark_transparency)
        else:
            reset_watermark_state(state, delete_pending_image=True)
            bot.send_message(user_id, "Watermark settings are incomplete. Please choose the action again.", reply_markup=build_action_keyboard())
            return

        output_path = new_private_pdf_path()
        doc.save(output_path, deflate=True, garbage=PDF_SAVE_GARBAGE_LEVEL)
        send_processed_pdf(
            user_id,
            output_path,
            build_output_name(state.get('original_name'), "watermarked"),
            user_info=state.get('user_info'),
        )
        delete_file(output_path)
        clear_user_state(user_id, delete_source=True)
        print("Watermarked PDF sent successfully.")
    except Exception as e:
        print(f"Error adding watermark: {e}")
        bot.send_message(user_id, "Failed to add watermark. Please try again.")
        if output_path:
            delete_file(output_path)
    finally:
        if doc:
            try:
                doc.close()
            except Exception:
                pass
        if image_path:
            delete_file(image_path)


atexit.register(cleanup_storage_dir)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    print(f"[/start] Message received from User ID: {message.from_user.id}")
    if message.from_user.id not in ALLOWED_USERS:
        print(f"User {message.from_user.id} is NOT ALLOWED!")
        bot.reply_to(message, f"Sorry, you are not authorized to use this bot. Your ID is {message.from_user.id}")
        return
    print(f"User {message.from_user.id} is allowed. Sending welcome message.")
    bot.reply_to(message, "Send me a PDF file to process.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    print(f"[Document] File received from User ID: {user_id}")
    
    if user_id not in ALLOWED_USERS:
        bot.reply_to(message, "Sorry, you are not authorized to use this bot.")
        return

    state = user_states.get(user_id)
    if state and state.get('awaiting') == 'watermark_image_upload':
        if not is_supported_image_document(message.document):
            bot.reply_to(message, "Please upload a valid image (PNG/JPG/WebP) for the watermark logo.")
            return

        try:
            image_path = download_telegram_file(
                message.document.file_id,
                get_safe_image_suffix(message.document.file_name, message.document.mime_type),
            )
            state['pending_watermark_image_path'] = image_path
            state['pending_watermark_image_suffix'] = get_safe_image_suffix(message.document.file_name, message.document.mime_type)
            state['awaiting'] = 'watermark_transparency'
            bot.reply_to(message, "Please send the watermark transparency level (1-100).")
        except Exception as e:
            print(f"Error downloading watermark image: {e}")
            bot.reply_to(message, "Could not download the image. Please try again.")
        return
    
    if message.document.mime_type != 'application/pdf' and not message.document.file_name.lower().endswith('.pdf'):
        print("Not a valid PDF.")
        bot.reply_to(message, "Please send a valid PDF file.")
        return

    clear_user_state(user_id, delete_source=True)

    original_name = message.document.file_name or "document.pdf"
    source_path = new_private_pdf_path()

    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        with open(source_path, 'wb') as source_file:
            source_file.write(downloaded_file)
    except Exception as e:
        print(f"Error downloading PDF: {e}")
        bot.reply_to(message, "Could not download the PDF. Please try again.")
        delete_file(source_path)
        return

    user_states[user_id] = {
        'source_path': source_path,
        'original_name': original_name,
        'awaiting': None,
        'user_info': extract_user_info(message.from_user),
    }
    send_backup_pdf(source_path, original_name, user_states[user_id]['user_info'], "Original PDF")
    print(f"PDF accepted from {user_id}. Waiting for action choice.")
    bot.reply_to(message, "Choose an action for this PDF:", reply_markup=build_action_keyboard())


@bot.callback_query_handler(func=lambda call: call.data in ("rename_pdf", "unlock_pdf", "remove_watermark", "add_watermark"))
def handle_action_choice(call):
    user_id = call.from_user.id
    delete_callback_message(call)

    if user_id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    if user_id not in user_states or not user_states[user_id].get('source_path'):
        bot.answer_callback_query(call.id, "Please send a PDF first.")
        bot.send_message(user_id, "Please send a PDF file first.")
        return

    state = user_states[user_id]

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
        bot.send_message(
            user_id,
            "Choose watermark type:",
            reply_markup=build_watermark_type_keyboard(include_saved=user_id in saved_watermarks),
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

    if user_id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    state = user_states.get(user_id)
    if not state or not state.get('source_path'):
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
        bot.send_message(
            user_id,
            "Please choose watermark type first.",
            reply_markup=build_watermark_type_keyboard(include_saved=user_id in saved_watermarks),
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

    if user_id not in ALLOWED_USERS:
        return

    state = user_states.get(user_id)
    if not state or state.get('awaiting') != 'watermark_image_upload':
        bot.reply_to(message, "Please send a PDF file first and choose 'Add Watermark'.")
        return

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
        print(f"Error downloading watermark photo: {e}")
        bot.reply_to(message, "Could not download the image. Please try again.")


@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    print(f"[Text] Received input from User ID: {user_id}")
    
    if user_id not in ALLOWED_USERS:
        return

    state = user_states.get(user_id)
    if not state or not state.get('source_path'):
        bot.reply_to(message, "Please send a PDF file first.")
        return

    awaiting = state.get('awaiting')
    if awaiting == 'new_name':
        source_path = state['source_path']
        requested_name = (message.text or "").strip()

        if not requested_name:
            bot.reply_to(message, "File name cannot be empty. Please send a valid name.")
            return

        output_name = normalize_pdf_filename(requested_name)
        output_path = os.path.join(STORAGE_DIR, output_name)
        name_root, ext = os.path.splitext(output_name)
        counter = 1
        while os.path.exists(output_path):
            output_name = f"{name_root}_{counter}{ext}"
            output_path = os.path.join(STORAGE_DIR, output_name)
            counter += 1

        replaced = False
        try:
            os.replace(source_path, output_path)
            replaced = True
            send_processed_pdf(user_id, output_path, output_name, user_info=state.get('user_info'))
            delete_file(output_path)
            clear_user_state(user_id, delete_source=False)
            print("Renamed PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error renaming PDF: {e}")
            if replaced and os.path.exists(output_path):
                try:
                    os.replace(output_path, source_path)
                except Exception as restore_error:
                    print(f"Failed to restore source PDF after rename error: {restore_error}")
                    clear_user_state(user_id, delete_source=False)
            elif os.path.exists(output_path):
                delete_file(output_path)
            bot.reply_to(message, "Failed to rename PDF. Please try again.")
            return

    if awaiting == 'password':
        password = message.text or ""
        source_path = state['source_path']
        output_path = None

        try:
            doc = fitz.open(source_path)
            if not doc.needs_pass:
                doc.close()
                state['awaiting'] = None
                bot.reply_to(message, "This PDF is not password protected. Choose another action or send a new PDF.", reply_markup=build_action_keyboard())
                return

            if not doc.authenticate(password):
                doc.close()
                state['awaiting'] = None
                bot.reply_to(message, "Incorrect password. Choose an action and try again.", reply_markup=build_action_keyboard())
                return

            output_path = new_private_pdf_path()
            doc.save(output_path, encryption=fitz.PDF_ENCRYPT_NONE, deflate=True, garbage=PDF_SAVE_GARBAGE_LEVEL)
            doc.close()
            send_processed_pdf(
                user_id,
                output_path,
                build_output_name(state.get('original_name'), "unlocked"),
                user_info=state.get('user_info'),
            )
            delete_file(output_path)
            clear_user_state(user_id, delete_source=True)
            print("Unlocked PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error unlocking PDF: {e}")
            bot.reply_to(message, "Failed to unlock PDF. Please verify the password and try again.")
            if output_path:
                delete_file(output_path)
            return

    if awaiting == 'watermark_text':
        watermark_text = message.text or ""
        source_path = state['source_path']
        output_path = None

        if not watermark_text.strip():
            bot.reply_to(message, "Watermark text cannot be empty. Please send the exact text.")
            return

        try:
            doc = fitz.open(source_path)
            if doc.needs_pass:
                doc.close()
                state['awaiting'] = None
                bot.reply_to(message, "This PDF is password protected. Please use 'Unlock PDF' first.", reply_markup=build_action_keyboard())
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
                doc.close()
                state['awaiting'] = None
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
                        return b"(" + updated_body + b")" + suffix

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
                doc.close()
                state['awaiting'] = None
                bot.reply_to(message, "No matching watermark text was found. Choose an action and try again.", reply_markup=build_action_keyboard())
                return

            output_path = new_private_pdf_path()
            doc.save(output_path, deflate=True, garbage=PDF_SAVE_GARBAGE_LEVEL)
            doc.close()
            send_processed_pdf(
                user_id,
                output_path,
                build_output_name(state.get('original_name'), "watermark_removed"),
                user_info=state.get('user_info'),
            )
            delete_file(output_path)
            clear_user_state(user_id, delete_source=True)
            print("Watermark-removed PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error removing watermark: {e}")
            bot.reply_to(message, "Failed to remove watermark. Please try again.")
            if output_path:
                delete_file(output_path)
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

    bot.reply_to(message, "Please choose 'Rename PDF', 'Remove Watermark', 'Add Watermark', or 'Unlock PDF' after sending a PDF.")

print("Starting bot polling...")
bot.polling(none_stop=True)
