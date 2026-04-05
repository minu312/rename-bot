import os
import telebot

TOKEN = os.environ.get('BOT_TOKEN')
ALLOWED_USERS_STR = os.environ.get('ALLOWED_USERS', '')

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

@bot.message_handler(commands=['start'])
def send_welcome(message):
    print(f"[/start] Message received from User ID: {message.from_user.id}")
    if message.from_user.id not in ALLOWED_USERS:
        print(f"User {message.from_user.id} is NOT ALLOWED!")
        bot.reply_to(message, f"Sorry, you are not authorized to use this bot. Your ID is {message.from_user.id}")
        return
    print(f"User {message.from_user.id} is allowed. Sending welcome message.")
    bot.reply_to(message, "Send me a PDF file to rename.")

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

    user_states[user_id] = {'file_id': message.document.file_id}
    print(f"PDF accepted. Waiting for new name from {user_id}")
    bot.reply_to(message, "Please send the new name for this PDF file (English).")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    user_id = message.from_user.id
    print(f"[Text] Received name '{message.text}' from User ID: {user_id}")
    
    if user_id not in ALLOWED_USERS:
        return
        
    if user_id in user_states and 'file_id' in user_states[user_id]:
        new_name = message.text
        if not new_name.lower().endswith('.pdf'):
            new_name += '.pdf'
            
        bot.reply_to(message, "Renaming and uploading...")
        print(f"Renaming file to: {new_name}")
        
        try:
            file_info = bot.get_file(user_states[user_id]['file_id'])
            downloaded_file = bot.download_file(file_info.file_path)
            
            with open(new_name, 'wb') as new_file:
                new_file.write(downloaded_file)
                
            with open(new_name, 'rb') as final_file:
                bot.send_document(user_id, final_file)
                
            os.remove(new_name)
            del user_states[user_id]
            print("File renamed and sent successfully!")
            
        except Exception as e:
            print(f"Error during rename/upload: {e}")
            bot.reply_to(message, f"An error occurred: {str(e)}")
            if user_id in user_states:
                del user_states[user_id]
    else:
        bot.reply_to(message, "Please send a PDF file first.")

print("Starting bot polling...")
bot.polling(none_stop=True)