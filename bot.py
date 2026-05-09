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


def _is_pdf_whitespace(byte):
    return byte in b"\x00\t\n\x0c\r "


def _is_pdf_delimiter(byte):
    return byte in b"()<>[]{}/%"


def _parse_literal_string_token(data, start):
    i = start + 1
    depth = 1
    n = len(data)
    while i < n:
        byte = data[i]
        if byte == 0x5C:
            i += 2
            continue
        if byte == 0x28:
            depth += 1
        elif byte == 0x29:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _parse_hex_string_token(data, start):
    i = start + 1
    n = len(data)
    while i < n and data[i] != 0x3E:
        i += 1
    return min(i + 1, n)


def _parse_array_token(data, start):
    i = start + 1
    depth = 1
    n = len(data)
    while i < n:
        byte = data[i]
        if byte == 0x28:
            i = _parse_literal_string_token(data, i)
            continue
        if byte == 0x3C and i + 1 < n and data[i + 1] != 0x3C:
            i = _parse_hex_string_token(data, i)
            continue
        if byte == 0x5B:
            depth += 1
        elif byte == 0x5D:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _decode_pdf_literal_string(token):
    if len(token) < 2:
        return None
    data = token[1:-1]
    result = bytearray()
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        if byte != 0x5C:
            result.append(byte)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        esc = data[i]
        if esc in b"nrtbf":
            result.extend({
                ord("n"): b"\n",
                ord("r"): b"\r",
                ord("t"): b"\t",
                ord("b"): b"\b",
                ord("f"): b"\f",
            }[esc])
            i += 1
            continue
        if esc in b"()\\":
            result.append(esc)
            i += 1
            continue
        if esc in b"\n\r":
            i += 1
            if esc == ord("\r") and i < n and data[i] == ord("\n"):
                i += 1
            continue
        if 48 <= esc <= 55:
            octal = bytes([esc])
            i += 1
            count = 1
            while i < n and count < 3 and 48 <= data[i] <= 55:
                octal += bytes([data[i]])
                i += 1
                count += 1
            result.append(int(octal, 8))
            continue
        result.append(esc)
        i += 1
    try:
        return result.decode("utf-8")
    except UnicodeDecodeError:
        return result.decode("latin-1", errors="ignore")


def _decode_pdf_hex_string(token):
    if len(token) < 2:
        return None
    hex_data = re.sub(rb"\s+", b"", token[1:-1])
    if len(hex_data) % 2 == 1:
        hex_data += b"0"
    try:
        decoded = bytes.fromhex(hex_data.decode("ascii"))
    except Exception:
        return None
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return decoded.decode("latin-1", errors="ignore")


def _decode_pdf_string_token(token, token_type):
    if token_type == "literal":
        return _decode_pdf_literal_string(token)
    if token_type == "hex":
        return _decode_pdf_hex_string(token)
    return None


def _replacement_if_watermark(token, token_type, watermark_text):
    decoded = _decode_pdf_string_token(token, token_type)
    if decoded == watermark_text:
        return True, b"()"
    return False, token


def _scan_next_pdf_token(data, start):
    i = start
    n = len(data)
    while i < n:
        byte = data[i]
        if _is_pdf_whitespace(byte):
            i += 1
            continue
        if byte == 0x25:
            while i < n and data[i] not in (0x0A, 0x0D):
                i += 1
            continue
        break
    if i >= n:
        return None
    token_start = i
    byte = data[i]
    if byte == 0x28:
        end = _parse_literal_string_token(data, i)
        return token_start, end, "literal", data[token_start:end]
    if byte == 0x3C and i + 1 < n and data[i + 1] != 0x3C:
        end = _parse_hex_string_token(data, i)
        return token_start, end, "hex", data[token_start:end]
    if byte == 0x5B:
        end = _parse_array_token(data, i)
        return token_start, end, "array", data[token_start:end]
    i += 1
    while i < n and (not _is_pdf_whitespace(data[i])) and (not _is_pdf_delimiter(data[i])):
        i += 1
    return token_start, i, "token", data[token_start:i]


def _replace_text_in_tj_array(array_token, watermark_text):
    inner = array_token[1:-1]
    i = 0
    n = len(inner)
    changed = False
    removed = 0
    parts = []
    cursor = 0
    while i < n:
        byte = inner[i]
        if byte == 0x28:
            end = _parse_literal_string_token(inner, i)
            token = inner[i:end]
            should_replace, replacement = _replacement_if_watermark(token, "literal", watermark_text)
            if should_replace:
                parts.append(inner[cursor:i])
                parts.append(replacement)
                cursor = end
                changed = True
                removed += 1
            i = end
            continue
        if byte == 0x3C and i + 1 < n and inner[i + 1] != 0x3C:
            end = _parse_hex_string_token(inner, i)
            token = inner[i:end]
            should_replace, replacement = _replacement_if_watermark(token, "hex", watermark_text)
            if should_replace:
                parts.append(inner[cursor:i])
                parts.append(replacement)
                cursor = end
                changed = True
                removed += 1
            i = end
            continue
        i += 1
    if not changed:
        return array_token, 0
    parts.append(inner[cursor:])
    return b"[" + b"".join(parts) + b"]", removed


def remove_watermark_from_content_stream(stream_bytes, watermark_text):
    i = 0
    prev_token = None
    edits = []
    removed = 0
    token = _scan_next_pdf_token(stream_bytes, i)
    while token is not None:
        start, end, token_type, raw = token
        if token_type == "token":
            if raw == b"Tj" and prev_token and prev_token[2] in ("literal", "hex"):
                prev_start, prev_end, prev_type, prev_raw = prev_token
                should_replace, replacement = _replacement_if_watermark(prev_raw, prev_type, watermark_text)
                if should_replace:
                    edits.append((prev_start, prev_end, replacement))
                    removed += 1
            elif raw == b"TJ" and prev_token and prev_token[2] == "array":
                prev_start, prev_end, _, prev_raw = prev_token
                updated_array, array_removed = _replace_text_in_tj_array(prev_raw, watermark_text)
                if array_removed > 0:
                    edits.append((prev_start, prev_end, updated_array))
                    removed += array_removed
        prev_token = token
        i = end
        token = _scan_next_pdf_token(stream_bytes, i)
    if not edits:
        return stream_bytes, 0
    output = bytearray()
    cursor = 0
    for start, end, replacement in sorted(edits, key=lambda x: x[0]):
        if start < cursor:
            print("Skipping watermark edit due to overlapping stream modifications.")
            return stream_bytes, 0
        output.extend(stream_bytes[cursor:start])
        output.extend(replacement)
        cursor = end
    output.extend(stream_bytes[cursor:])
    return bytes(output), removed


def remove_watermark_by_stream_edit(doc, watermark_text):
    total_removed = 0
    for page in doc:
        page.clean_contents()
        content_refs = page.get_contents()
        if isinstance(content_refs, int):
            content_refs = [content_refs]
        for xref in content_refs or []:
            stream_bytes = doc.xref_stream(xref)
            if not stream_bytes:
                continue
            updated_stream, removed = remove_watermark_from_content_stream(stream_bytes, watermark_text)
            if removed > 0 and updated_stream != stream_bytes:
                doc.update_stream(xref, updated_stream)
                total_removed += removed
    return total_removed


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

        replaced = False
        try:
            os.replace(source_path, output_path)
            replaced = True
            send_processed_pdf(user_id, output_path, output_name)
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
            send_processed_pdf(user_id, output_path, build_output_name(state.get('original_name'), "unlocked"))
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

            matches = remove_watermark_by_stream_edit(doc, watermark_text)

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
            bot.reply_to(message, "Failed to remove watermark. Please try again.")
            if output_path:
                delete_file(output_path)
            return

    bot.reply_to(message, "Please choose 'Rename PDF', 'Remove Watermark', or 'Unlock PDF' after sending a PDF.")

print("Starting bot polling...")
bot.polling(none_stop=True)
