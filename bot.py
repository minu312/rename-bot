import os
import telebot
import fitz
from telebot import types
import tempfile
import shutil
import atexit
import re

TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')
STORAGE_DIR = tempfile.mkdtemp(prefix='pdf-processor-')
PDF_SAVE_GARBAGE_LEVEL = 3

print(f"Token loaded: {bool(TOKEN)}")
print(f"Allowed users string: {ALLOWED_USERS_STR}")

try:
    ALLOWED_USERS = [int(uid.strip()) for uid in ALLOWED_USERS_STR.split(',') if uid.strip()]
    print(f"Allowed Users List: {ALLOWED_USERS}")
except Exception as e:
    print(f"Error parsing user IDs: {e}")
    ALLOWED_USERS = []

bot = telebot.TeleBot(TOKEN)
user_states = {}


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


def clear_user_state(user_id, delete_source=False):
    state = user_states.get(user_id)
    if not state:
        return
    if delete_source:
        delete_file(state.get('source_path'))
    user_states.pop(user_id, None)


def build_action_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("Rename PDF", callback_data="rename_pdf"))
    keyboard.row(types.InlineKeyboardButton("Remove Watermark", callback_data="remove_watermark"))
    keyboard.row(types.InlineKeyboardButton("Unlock PDF", callback_data="unlock_pdf"))
    return keyboard


def send_processed_pdf(user_id, output_path, output_name):
    with open(output_path, 'rb') as final_file:
        bot.send_document(user_id, final_file, visible_file_name=output_name)


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
    if not cleaned:
        cleaned = "renamed_document"
    if not cleaned.lower().endswith('.pdf'):
        cleaned = f"{cleaned}.pdf"
    return cleaned


def new_private_pdf_path():
    fd, temp_path = tempfile.mkstemp(suffix='.pdf', dir=STORAGE_DIR)
    os.close(fd)
    return temp_path


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
    }
    print(f"PDF accepted from {user_id}. Waiting for action choice.")
    bot.reply_to(message, "Choose an action for this PDF:", reply_markup=build_action_keyboard())


@bot.callback_query_handler(func=lambda call: call.data in ("rename_pdf", "unlock_pdf", "remove_watermark"))
def handle_action_choice(call):
    user_id = call.from_user.id

    if user_id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    if user_id not in user_states or not user_states[user_id].get('source_path'):
        bot.answer_callback_query(call.id, "Please send a PDF first.")
        bot.send_message(user_id, "Please send a PDF file first.")
        return

    if call.data == "rename_pdf":
        user_states[user_id]['awaiting'] = 'new_name'
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "Please type and send the new file name.")
        return

    if call.data == "unlock_pdf":
        user_states[user_id]['awaiting'] = 'password'
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "Please send the password for this PDF.")
        return

    user_states[user_id]['awaiting'] = 'watermark_text'
    bot.answer_callback_query(call.id)
    bot.send_message(user_id, "Please send the exact watermark text to remove.")

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

        try:
            os.replace(source_path, output_path)
            send_processed_pdf(user_id, output_path, output_name)
            delete_file(output_path)
            clear_user_state(user_id, delete_source=False)
            print("Renamed PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error renaming PDF: {e}")
            if os.path.exists(output_path) and not os.path.exists(source_path):
                try:
                    os.replace(output_path, source_path)
                except Exception as restore_error:
                    print(f"Failed to restore source PDF after rename error: {restore_error}")
                    clear_user_state(user_id, delete_source=False)
            bot.reply_to(message, f"Failed to rename PDF: {str(e)}")
            if os.path.exists(output_path) and os.path.exists(source_path):
                delete_file(output_path)
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
            send_processed_pdf(user_id, output_path, build_output_name(state.get('original_name'), "unlocked"))
            delete_file(output_path)
            clear_user_state(user_id, delete_source=True)
            print("Unlocked PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error unlocking PDF: {e}")
            bot.reply_to(message, f"Failed to unlock PDF: {str(e)}")
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

            matches = 0
            for page in doc:
                rects = page.search_for(watermark_text)
                for rect in rects:
                    page.add_redact_annot(rect, fill=None)
                if rects:
                    page.apply_redactions()
                    matches += len(rects)

            if matches == 0:
                doc.close()
                state['awaiting'] = None
                bot.reply_to(message, "No matching watermark text was found. Choose an action and try again.", reply_markup=build_action_keyboard())
                return

            output_path = new_private_pdf_path()
            doc.save(output_path, deflate=True, garbage=PDF_SAVE_GARBAGE_LEVEL)
            doc.close()
            send_processed_pdf(user_id, output_path, build_output_name(state.get('original_name'), "watermark_removed"))
            delete_file(output_path)
            clear_user_state(user_id, delete_source=True)
            print("Watermark-removed PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error removing watermark: {e}")
            bot.reply_to(message, f"Failed to remove watermark: {str(e)}")
            if output_path:
                delete_file(output_path)
            return

    bot.reply_to(message, "Please choose 'Rename PDF', 'Remove Watermark', or 'Unlock PDF' after sending a PDF.")

print("Starting bot polling...")
bot.polling(none_stop=True)
