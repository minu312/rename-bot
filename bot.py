import os
import telebot
import fitz
from telebot import types
from uuid import uuid4

TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')
STORAGE_DIR = '/tmp/pdf-processor-files'
WHITE_FILL = (1, 1, 1)

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


def ensure_storage_dir():
    os.makedirs(STORAGE_DIR, exist_ok=True)


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
    keyboard.row(
        types.InlineKeyboardButton("Unlock PDF", callback_data="unlock_pdf"),
        types.InlineKeyboardButton("Remove Watermark", callback_data="remove_watermark"),
    )
    return keyboard


def send_processed_pdf(user_id, output_path, output_name):
    with open(output_path, 'rb') as final_file:
        bot.send_document(user_id, final_file, visible_file_name=output_name)


def build_output_name(original_name, suffix):
    base_name = original_name or "document.pdf"
    if base_name.lower().endswith('.pdf'):
        base_name = base_name[:-4]
    return f"{base_name}_{suffix}.pdf"

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

    ensure_storage_dir()
    clear_user_state(user_id, delete_source=True)

    original_name = message.document.file_name or "document.pdf"
    source_path = os.path.join(STORAGE_DIR, f"{user_id}_{uuid4().hex}.pdf")

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


@bot.callback_query_handler(func=lambda call: call.data in ("unlock_pdf", "remove_watermark"))
def handle_action_choice(call):
    user_id = call.from_user.id

    if user_id not in ALLOWED_USERS:
        bot.answer_callback_query(call.id, "Not authorized.")
        return

    if user_id not in user_states or not user_states[user_id].get('source_path'):
        bot.answer_callback_query(call.id, "Please send a PDF first.")
        bot.send_message(user_id, "Please send a PDF file first.")
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
    print(f"[Text] Received '{message.text}' from User ID: {user_id}")
    
    if user_id not in ALLOWED_USERS:
        return

    state = user_states.get(user_id)
    if not state or not state.get('source_path'):
        bot.reply_to(message, "Please send a PDF file first.")
        return

    awaiting = state.get('awaiting')
    if awaiting == 'password':
        password = message.text or ""
        source_path = state['source_path']
        output_path = os.path.join(STORAGE_DIR, f"{user_id}_{uuid4().hex}_unlocked.pdf")

        try:
            doc = fitz.open(source_path)
            if not doc.needs_pass:
                doc.close()
                bot.reply_to(message, "This PDF is not password protected.")
                return

            if not doc.authenticate(password):
                doc.close()
                bot.reply_to(message, "Incorrect password. Please try again.")
                return

            doc.save(output_path, encryption=fitz.PDF_ENCRYPT_NONE)
            doc.close()
            send_processed_pdf(user_id, output_path, build_output_name(state.get('original_name'), "unlocked"))
            delete_file(output_path)
            clear_user_state(user_id, delete_source=True)
            print("Unlocked PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error unlocking PDF: {e}")
            bot.reply_to(message, f"Failed to unlock PDF: {str(e)}")
            delete_file(output_path)
            return

    if awaiting == 'watermark_text':
        watermark_text = message.text or ""
        source_path = state['source_path']
        output_path = os.path.join(STORAGE_DIR, f"{user_id}_{uuid4().hex}_watermark_removed.pdf")

        if not watermark_text.strip():
            bot.reply_to(message, "Watermark text cannot be empty. Please send the exact text.")
            return

        try:
            doc = fitz.open(source_path)
            if doc.needs_pass:
                doc.close()
                bot.reply_to(message, "This PDF is password protected. Please use 'Unlock PDF' first.")
                return

            matches = 0
            for page in doc:
                rects = page.search_for(watermark_text)
                for rect in rects:
                    page.add_redact_annot(rect, fill=WHITE_FILL)
                if rects:
                    page.apply_redactions()
                    matches += len(rects)

            if matches == 0:
                doc.close()
                bot.reply_to(message, "No matching watermark text was found. Please send exact text and try again.")
                return

            doc.save(output_path)
            doc.close()
            send_processed_pdf(user_id, output_path, build_output_name(state.get('original_name'), "watermark_removed"))
            delete_file(output_path)
            clear_user_state(user_id, delete_source=True)
            print("Watermark-removed PDF sent successfully.")
            return
        except Exception as e:
            print(f"Error removing watermark: {e}")
            bot.reply_to(message, f"Failed to remove watermark: {str(e)}")
            delete_file(output_path)
            return

    bot.reply_to(message, "Please choose 'Unlock PDF' or 'Remove Watermark' after sending a PDF.")

print("Starting bot polling...")
bot.polling(none_stop=True)
